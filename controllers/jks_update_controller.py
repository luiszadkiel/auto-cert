"""
controllers/jks_update_controller.py
──────────────────────────────────────
Controller del modo jks-update.

Recorre los mismos clusters/namespaces que JksDiscoveryController, pero en
vez de solo generar un reporte, detecta certificados JKS vencidos, descarga
el cert fresco del host, reconstruye el keystore y lo sube al cluster.

Flujo:
  1. Auth Azure + descubrimiento de clusters AKS (igual que jks-discovery)
  2. Por cluster → namespaces → secrets con JKS
  3. Agrupar por (secret_name, data_key) → procesar cada keystore una sola vez
  4. Llamar process_jks_secret() por keystore
  5. Guardar JSON + Excel con todos los registros y estados

Variables de entorno:
  JKS_UPDATE_APPLY=true   → escribir en el cluster (default: dry-run)
  JKS_UPDATE_PRUNE=true   → borrar aliases duplicados vencidos (default: false)
  FILTER_NAME=<name>      → procesar solo secrets que coincidan con ese nombre
"""

import asyncio
import os
import re
import traceback
from datetime import datetime, timezone
from collections import Counter

from config.settings import (
    OUTPUT_DIR,
    JKS_UPDATE_APPLY,
    JKS_UPDATE_PRUNE,
    JKS_REFETCH_PORT,
    JKS_REFETCH_TIMEOUT,
)
from services.azure_auth_service import AzureAuthService
from services.k8s_service import K8sService
from services.jks_updater_service import process_jks_secret
from models.k8s_cert import K8sCert
from views.reporter import save_json, save_excel, build_output_paths, print_banner


class JksUpdateController:
    """
    Recorre todos los clusters no-prod, detecta keystores JKS vencidos,
    y los actualiza (o reporta qué actualizaría en dry-run).

    Resiliencia: ningún error individual detiene el recorrido completo.
    """

    async def run(self, run_filter: dict | None = None) -> dict:
        if not run_filter:
            run_filter = {"mode": "all"}

        filter_mode = run_filter.get("mode", "all")
        apply_changes = JKS_UPDATE_APPLY
        prune = JKS_UPDATE_PRUNE

        now = datetime.now(timezone.utc)

        # ── Banner de modo ────────────────────────────────────────────────────
        mode_label = "🟢 APPLY (escribirá en el cluster)" if apply_changes else "🔵 DRY-RUN (solo reporte)"
        prune_label = "✂ prune activado" if prune else "sin prune"
        print(f"\n{'='*60}")
        print(f"  [jks-update] Modo: {mode_label}")
        print(f"  [jks-update] {prune_label}")
        print(f"{'='*60}\n")

        all_records: list[dict] = []
        cluster_summaries: list[dict] = []
        errors_log: list[str] = []

        # ── Autenticación Azure ───────────────────────────────────────────────
        auth_svc = AzureAuthService()
        if not auth_svc.ensure_az_session():
            msg = "[ERROR] No se pudo asegurar la sesión de Azure. Abortando."
            print(msg)
            return {"error": "Azure session failed", "errors": [msg], "records": [], "total": 0}

        # ── Resource Graph extension (no-fatal) ───────────────────────────────
        try:
            if not auth_svc.ensure_resource_graph_extension():
                msg = "[WARN] No se pudo instalar resource-graph. Continuando..."
                print(msg)
                errors_log.append(msg)
        except Exception as e:
            msg = f"[WARN] Excepción en resource-graph extension: {e}. Continuando..."
            print(msg)
            errors_log.append(msg)

        # ── Descubrimiento de clusters ────────────────────────────────────────
        try:
            discovered_clusters = auth_svc.discover_all_aks_clusters()
        except Exception as e:
            msg = f"[ERROR] Excepción descubriendo clusters: {e}"
            print(msg)
            discovered_clusters = []
            errors_log.append(msg)

        if not discovered_clusters:
            msg = "[WARN] No se descubrieron clústeres AKS en Azure."
            print(msg)
            errors_log.append(msg)

        # ── Filtrar clusters ──────────────────────────────────────────────────
        cluster_list = []
        for c in discovered_clusters:
            name = c.get("name", "").lower()

            # Omitir producción real — sin capturar "noprod"
            # (BUG heredado de jks_discovery: "noprod" contiene "prod")
            is_noprod = "noprod" in name
            is_prod = bool(re.search(r"(?<![a-z])prod(?![a-z])|(?<![a-z])prd(?![a-z])", name))
            if is_prod and not is_noprod:
                print(f"  [INFO] Omitiendo cluster de produccion: {name}")
                continue

            cluster_list.append(c)

        print(f"[*] {len(cluster_list)} cluster(s) a procesar "
              f"(de {len(discovered_clusters)} descubiertos)\n")

        # ── Recorrer clusters ─────────────────────────────────────────────────
        for cluster_meta in cluster_list:
            cluster_name = cluster_meta.get("name", "desconocido")
            cluster_records: list[dict] = []
            try:
                await asyncio.sleep(0.1)

                sub_id = cluster_meta.get("subscriptionId")
                rg = cluster_meta.get("resourceGroup")

                if not sub_id or not rg:
                    print(f"  [!] Ignorando {cluster_name}: faltan metadatos sub/rg.")
                    cluster_summaries.append({"cluster": cluster_name, "error": "Faltan metadatos"})
                    continue

                ctx = cluster_name
                print(f"\n[->] Preparando clúster: {cluster_name}")

                if not auth_svc.preflight_rbac_check(sub_id, rg, cluster_name):
                    print(f"  [!] Ignorando {cluster_name}: sin roles RBAC.")
                    cluster_summaries.append({"cluster": cluster_name, "error": "No RBAC"})
                    continue

                if not auth_svc.set_subscription(sub_id):
                    print(f"  [!] Ignorando {cluster_name}: error al setear subscripción.")
                    cluster_summaries.append({"cluster": cluster_name, "error": "Subscription Error"})
                    continue

                if not auth_svc.configure_cluster_context(rg, cluster_name):
                    print(f"  [!] Ignorando {cluster_name}: falló descarga de credenciales.")
                    cluster_summaries.append({"cluster": cluster_name, "error": "Kubeconfig Error"})
                    continue

                if not auth_svc.auth_cani_check(ctx):
                    print(f"  [!] Ignorando {cluster_name}: acceso denegado.")
                    cluster_summaries.append({"cluster": cluster_name, "error": "Auth Denied"})
                    continue

                k8s_svc = K8sService()
                namespaces = k8s_svc.list_namespaces(cluster_name)
                if not namespaces:
                    print(f"  [{cluster_name}] ⚠ inaccesible o sin namespaces — se omite")
                    cluster_summaries.append({"cluster": cluster_name, "error": "inaccesible", "namespaces": 0})
                    continue

                print(f"  [{cluster_name}] {len(namespaces)} namespace(s)")

                for ns in namespaces:
                    try:
                        await asyncio.sleep(0.05)

                        # Obtener todos los certs del namespace (CRT + alias JKS)
                        certs: list[K8sCert] = k8s_svc.list_secrets_with_all_certs(cluster_name, ns)

                        # Solo JKS con bytes reales
                        jks_certs = [
                            c for c in certs
                            if c.secret_type == "JKS" and c.cert_pem
                        ]
                        if not jks_certs:
                            continue

                        # Agrupar por (secret_name, data_key) — un keystore, una llamada
                        seen_keystores: dict[tuple[str, str], K8sCert] = {}
                        for c in jks_certs:
                            key = (c.secret_name, c.data_key)
                            if key not in seen_keystores:
                                seen_keystores[key] = c  # primer alias → tiene jks_bytes y password

                        for (secret_name, jks_key), representative in seen_keystores.items():
                            # Aplicar filtro FILTER_NAME si está activo
                            if filter_mode == "names":
                                names_lower = [n.lower() for n in run_filter.get("names", [])]
                                if secret_name.lower() not in names_lower:
                                    continue

                            try:
                                records = process_jks_secret(
                                    cluster=cluster_name,
                                    namespace=ns,
                                    secret_name=secret_name,
                                    jks_key=jks_key,
                                    jks_bytes=representative.cert_pem,
                                    password=representative.password,
                                    context=ctx,
                                    now=now,
                                    apply_changes=apply_changes,
                                    prune=prune,
                                    port=JKS_REFETCH_PORT,
                                    timeout=JKS_REFETCH_TIMEOUT,
                                )
                                cluster_records.extend(records)
                            except Exception as ks_err:
                                msg = f"  [WARN] Error procesando keystore {ns}/{secret_name}#{jks_key}: {ks_err}"
                                print(msg)
                                errors_log.append(msg)

                    except Exception as ns_err:
                        msg = f"  [WARN] Error en namespace {cluster_name}/{ns}: {ns_err}"
                        print(msg)
                        errors_log.append(msg)

                # Resumen por cluster
                estado_counts = Counter(r["estado"] for r in cluster_records)
                all_records.extend(cluster_records)
                cluster_summaries.append({
                    "cluster":      cluster_name,
                    "namespaces":   len(namespaces),
                    "keystores":    sum(1 for r in cluster_records if r.get("estado") != "DUPLICADO_VENCIDO"),
                    "actualizados": estado_counts.get("ACTUALIZADO", 0),
                    "actualizaria": estado_counts.get("ACTUALIZARIA", 0),
                    "duplicados":   estado_counts.get("DUPLICADO_VENCIDO", 0) + estado_counts.get("DUPLICADO_VIGENTE", 0),
                    "omitidos":     estado_counts.get("OMITIDO", 0),
                })
                print(f"  [{cluster_name}] Resumen: {dict(estado_counts)}")

            except Exception as cluster_err:
                msg = f"[ERROR] Excepción procesando cluster {cluster_name}: {cluster_err}"
                print(msg)
                print(traceback.format_exc())
                errors_log.append(msg)
                cluster_summaries.append({"cluster": cluster_name, "error": str(cluster_err)})

        # ── Guardar reportes ──────────────────────────────────────────────────
        global_counts = Counter(r["estado"] for r in all_records)

        try:
            paths = build_output_paths(OUTPUT_DIR, "jks_update")
            save_json(all_records, paths["json"])
            save_excel(all_records, paths["excel"])

            mode_str = "APPLY" if apply_changes else "DRY-RUN"
            print_banner(
                f"jks-update ({mode_str}) completado\n"
                f"  Clusters : {len(cluster_list)}\n"
                f"  ACTUALIZADO      : {global_counts.get('ACTUALIZADO', 0)}\n"
                f"  ACTUALIZARIA     : {global_counts.get('ACTUALIZARIA', 0)}\n"
                f"  DUPLICADO_VIGENTE: {global_counts.get('DUPLICADO_VIGENTE', 0)}\n"
                f"  DUPLICADO_VENCIDO: {global_counts.get('DUPLICADO_VENCIDO', 0)}\n"
                f"  OMITIDO          : {global_counts.get('OMITIDO', 0)}\n"
                f"  JSON  → {os.path.abspath(paths['json'])}\n"
                f"  EXCEL → {os.path.abspath(paths['excel'])}"
            )
        except Exception as e:
            msg = f"[ERROR] Error guardando reportes: {e}"
            print(msg)
            errors_log.append(msg)

        if errors_log:
            print(f"\n[RESUMEN] {len(errors_log)} advertencia(s)/error(es) durante el proceso:")
            for err in errors_log:
                print(f"  • {err}")

        return {
            "records":          all_records,
            "cluster_summaries": cluster_summaries,
            "total":            len(all_records),
            "counts":           dict(global_counts),
            "errors":           errors_log,
            "apply":            apply_changes,
        }
