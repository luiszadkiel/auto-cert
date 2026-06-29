"""
controllers/inventory_controller.py
─────────────────────────────────────
Controller: inventario completo de certificados con comparación triple.

Flujo de 5 fases:
  1. Cargar fuentes (Excel + Portal)
  2. Leer cluster (kubectl + keytool para JKS)
  3. Comparación triple (Excel × Portal × Cluster)
  4. Sincronización al portal (certs renovados)
  5. Generar Excel final con estados actualizados

Uso: py main.py inventory
"""

import os
from config.settings import TARGET_URL, OUTPUT_DIR, INVENTORY_INPUT_PATH
from services.browser_service import BrowserService
from services.auth_service import AuthService
from services.portal_k8s_scraper_service import PortalK8sScraperService
from services.cluster_discovery_service import ClusterDiscoveryService
from services.k8s_service import K8sService
from services.excel_loader_service import ExcelLoaderService
from services.inventory_diff_service import InventoryDiffService
from services.report_exporter_service import ReportExporterService
from services.portal_uploader_service import PortalUploaderService
from services.audit_service import AuditService
from models.audit_log import EventType
from models.k8s_cert import K8sCert, PortalK8sCert, CertDiff
from models.inventory_cert import (
    InventoryCert,
    STATUS_RENOVADO,
)
from views.reporter import print_banner, save_json, build_output_paths


class InventoryController:
    """
    Controlador del inventario completo de certificados.

    Cruza Excel + Portal + Cluster, detecta discrepancias,
    sincroniza renovados, y genera el Excel final.
    """

    async def run(self) -> None:
        print("\n[*] Iniciando inventario de certificados — Comparación Triple")
        print("    (Excel + Portal + Cluster → Estado real → Excel final)\n")

        audit = AuditService(mode="inventory")
        excel_certs: list[InventoryCert] = []
        portal_certs: list[PortalK8sCert] = []
        cluster_certs: list[K8sCert] = []
        final_inventory: list[InventoryCert] = []
        ok_list: list[str] = []
        fail_list: list[str] = []
        xlsx_path = ""

        try:
            # ══════════════════════════════════════════════════════════════
            # FASE 1: Cargar fuentes
            # ══════════════════════════════════════════════════════════════
            print("─" * 60)
            print("  FASE 1: Cargando fuentes")
            print("─" * 60)

            # 1a. Excel de inventario
            print(f"\n  → Cargando Excel: {os.path.basename(INVENTORY_INPUT_PATH)}")
            excel_loader = ExcelLoaderService()
            excel_certs = excel_loader.load(INVENTORY_INPUT_PATH)
            audit.log(
                EventType.SCRAPE_OK,
                f"Excel cargado: {len(excel_certs)} registros",
                path=INVENTORY_INPUT_PATH,
                total=len(excel_certs),
            )

            if not excel_certs:
                print("  [!] Excel vacío o no encontrado — continuando solo con Portal + Cluster")

            # 1b. Portal DevOps (Playwright)
            print(f"\n  → Conectando al portal: {TARGET_URL}")

            async with BrowserService() as browser:
                page = browser.page

                # Login
                await browser.goto(TARGET_URL)
                auth = AuthService(page)
                if auth.needs_login():
                    if auth.has_credentials():
                        audit.log(EventType.AUTH_AUTO, "Auto-login con credenciales del .env")
                    else:
                        audit.log(EventType.AUTH_MANUAL, "Esperando login manual")
                    await auth.login()
                    audit.log(EventType.AUTH_OK, "Login completado")
                else:
                    audit.log(EventType.AUTH_OK, "Sesión ya activa")

                if "inventariodecertificados" not in page.url:
                    await browser.goto(TARGET_URL)
                await browser.wait_for_network_idle()

                # Scraping del portal
                audit.log(EventType.SCRAPE_START, "Extrayendo tabla K8s del portal")
                portal_scraper = PortalK8sScraperService(page)
                portal_certs = await portal_scraper.scrape_all()

                audit.log(
                    EventType.SCRAPE_OK,
                    f"Portal: {len(portal_certs)} registros K8s extraídos",
                    total=len(portal_certs),
                )

                print(f"\n  ✓ Fuentes cargadas: Excel={len(excel_certs)} | Portal={len(portal_certs)}")

                # ══════════════════════════════════════════════════════════
                # FASE 2: Leer cluster (fuente de verdad)
                # ══════════════════════════════════════════════════════════
                print("\n" + "─" * 60)
                print("  FASE 2: Leyendo cluster (fuente de verdad)")
                print("─" * 60)

                # Deducir secrets únicos de ambas fuentes
                unique_secrets = self._deduplicate_secrets(excel_certs, portal_certs)
                print(f"\n  → {len(unique_secrets)} secrets únicos para consultar al cluster")

                # Validar clusters accesibles
                discovery = ClusterDiscoveryService()
                # Extraer clusters únicos
                clusters = sorted({s[0] for s in unique_secrets})
                print(f"  → Clusters: {clusters}")

                # Verificar accesibilidad
                cluster_statuses = discovery.discover_from_portal(portal_certs)

                # Leer cada secret del cluster
                k8s = K8sService()
                cluster_certs = []
                processed = 0
                skipped_unreachable = 0

                for cluster_name, namespace, secret_name in unique_secrets:
                    # Verificar si el cluster es accesible
                    status = cluster_statuses.get(cluster_name)
                    if status and not status.reachable:
                        skipped_unreachable += 1
                        continue

                    certs = k8s.get_secret_all_certs(cluster_name, namespace, secret_name)
                    cluster_certs.extend(certs)
                    processed += 1

                audit.log(
                    EventType.SCRAPE_OK,
                    f"Cluster: {len(cluster_certs)} certificados leídos de {processed} secrets",
                    total_certs=len(cluster_certs),
                    secrets_processed=processed,
                    skipped_unreachable=skipped_unreachable,
                )

                print(f"\n  ✓ Cluster: {len(cluster_certs)} certs de {processed} secrets")
                if skipped_unreachable:
                    print(f"  ⚠ {skipped_unreachable} secrets omitidos (cluster no accesible)")

                # ══════════════════════════════════════════════════════════
                # FASE 3: Comparación triple
                # ══════════════════════════════════════════════════════════
                print("\n" + "─" * 60)
                print("  FASE 3: Comparación triple")
                print("─" * 60)

                diff_service = InventoryDiffService()
                final_inventory = diff_service.compare_triple(
                    excel_certs=excel_certs,
                    portal_certs=portal_certs,
                    cluster_certs=cluster_certs,
                )

                # Guardar reporte JSON intermedio
                paths = build_output_paths(OUTPUT_DIR, "inventory")
                diff_report = [c.to_dict() for c in final_inventory]
                save_json(diff_report, paths["json"])
                audit.log(EventType.FILE_SAVED, "Reporte de diff guardado", path=paths["json"])

                # ══════════════════════════════════════════════════════════
                # FASE 4: Sincronización al portal
                # ══════════════════════════════════════════════════════════
                renovados = [c for c in final_inventory if c.status == STATUS_RENOVADO]

                if renovados:
                    print("\n" + "─" * 60)
                    print(f"  FASE 4: Sincronización al portal ({len(renovados)} renovados)")
                    print("─" * 60)

                    uploader = PortalUploaderService(page)

                    for cert in renovados:
                        # Buscar el K8sCert correspondiente del cluster
                        matching_cluster_cert = self._find_cluster_cert(
                            cert, cluster_certs
                        )
                        if not matching_cluster_cert:
                            fail_list.append(f"{cert.namespace}/{cert.secret_name}")
                            continue

                        # Buscar el PortalK8sCert correspondiente
                        matching_portal_cert = self._find_portal_cert(
                            cert, portal_certs
                        )
                        if not matching_portal_cert:
                            # Crear uno sintético para el uploader
                            matching_portal_cert = PortalK8sCert(
                                cluster=cert.cluster,
                                ambiente=cert.ambiente,
                                namespace=cert.namespace,
                                secret_name=cert.secret_name,
                                cert_name=cert.cert_name,
                                created_at=cert.created_at,
                                expires_at=cert.expires_at,
                                secret_type=cert.secret_type,
                                status=cert.status,
                            )

                        diff = CertDiff(
                            portal_cert=matching_portal_cert,
                            cluster_cert=matching_cluster_cert,
                            needs_update=True,
                            reason=f"Renovado: cluster más reciente que portal/excel",
                        )

                        name = f"{cert.namespace}/{cert.secret_name}"
                        audit.log(
                            EventType.UPLOAD_START,
                            f"Subiendo {name} al portal",
                            namespace=cert.namespace,
                            secret=cert.secret_name,
                        )

                        success = await uploader.upload(diff)
                        if success:
                            ok_list.append(name)
                            audit.log(EventType.UPLOAD_OK, f"OK: {name}")
                        else:
                            fail_list.append(name)
                            audit.log(EventType.UPLOAD_FAIL, f"FAIL: {name}")
                else:
                    print("\n  → FASE 4: Sin renovados — nada que sincronizar al portal")

            # ══════════════════════════════════════════════════════════════
            # FASE 5: Excel final
            # ══════════════════════════════════════════════════════════════
            print("\n" + "─" * 60)
            print("  FASE 5: Generando Excel final")
            print("─" * 60)

            exporter = ReportExporterService()
            xlsx_path = exporter.export(final_inventory, OUTPUT_DIR)

            audit.log(EventType.FILE_SAVED, "Excel final generado", path=xlsx_path)

            # ── Cerrar auditoría ──────────────────────────────────────────
            audit.close(
                status="OK" if not fail_list else "PARCIAL",
                excel_input=len(excel_certs),
                portal_input=len(portal_certs),
                cluster_input=len(cluster_certs),
                total_output=len(final_inventory),
                renovados=len(renovados) if renovados else 0,
                uploaded_ok=len(ok_list),
                uploaded_fail=len(fail_list),
                xlsx_path=xlsx_path,
            )

        except Exception as exc:
            audit.log(EventType.ERROR, f"Error: {exc}", error=str(exc))
            audit.close(status="ERROR", error=str(exc))
            raise

        # ── Resumen final ─────────────────────────────────────────────────
        renovados_count = sum(1 for c in final_inventory if c.status == STATUS_RENOVADO)
        activos_count = sum(1 for c in final_inventory if c.status == "Activo")
        vencidos_count = sum(1 for c in final_inventory if c.status == "Vencido")
        nuevos_count = sum(1 for c in final_inventory if c.status == "Nuevo")
        no_encontrados_count = sum(1 for c in final_inventory if c.status == "No encontrado")

        print_banner(
            f"✓ Inventario completado\n"
            f"  Fuentes: Excel={len(excel_certs)} | Portal={len(portal_certs)} | "
            f"Cluster={len(cluster_certs)}\n"
            f"  Resultado: {len(final_inventory)} certificados\n"
            f"    Activos: {activos_count} | Vencidos: {vencidos_count} | "
            f"Renovados: {renovados_count}\n"
            f"    Nuevos: {nuevos_count} | No encontrados: {no_encontrados_count}\n"
            f"  Sync portal: OK={len(ok_list)} | Fail={len(fail_list)}\n"
            f"  Excel: {os.path.abspath(xlsx_path) if xlsx_path else 'N/A'}"
        )

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _deduplicate_secrets(
        self,
        excel_certs: list[InventoryCert],
        portal_certs: list[PortalK8sCert],
    ) -> list[tuple[str, str, str]]:
        """
        Extrae los secrets únicos (cluster, namespace, secret_name)
        de ambas fuentes para consultar al cluster.
        """
        seen: set[tuple[str, str, str]] = set()

        for cert in excel_certs:
            if cert.cluster and cert.namespace and cert.secret_name:
                seen.add((cert.cluster, cert.namespace, cert.secret_name))

        for cert in portal_certs:
            if cert.cluster and cert.namespace and cert.secret_name:
                seen.add((cert.cluster, cert.namespace, cert.secret_name))

        return sorted(seen)

    def _find_cluster_cert(
        self,
        inv_cert: InventoryCert,
        cluster_certs: list[K8sCert],
    ) -> K8sCert | None:
        """Busca el K8sCert del cluster que coincida con un InventoryCert."""
        for cc in cluster_certs:
            if (
                cc.namespace.lower() == inv_cert.namespace.lower()
                and cc.secret_name.lower() == inv_cert.secret_name.lower()
            ):
                return cc
        return None

    def _find_portal_cert(
        self,
        inv_cert: InventoryCert,
        portal_certs: list[PortalK8sCert],
    ) -> PortalK8sCert | None:
        """Busca el PortalK8sCert que coincida con un InventoryCert."""
        for pc in portal_certs:
            if (
                pc.namespace.lower() == inv_cert.namespace.lower()
                and pc.secret_name.lower() == inv_cert.secret_name.lower()
            ):
                return pc
        return None
