"""
controllers/jks_discovery_controller.py
â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
Controller: exploración masiva de TODOS los certificados (CRT y JKS) en
TODOS los clusters configurados (K8S_CLUSTERS), TODOS los namespaces,
TODOS los secrets.

Reemplaza a K8sSyncController para el nuevo flujo: NO hay login al portal,
NO hay scraping, NO hay diff contra el portal, NO hay upload. La única
responsabilidad es: descubrir â†’ extraer â†’ estructurar.

Flujo:
  1. Para cada cluster en K8S_CLUSTERS (o el subconjunto filtrado):
       a. Listar TODOS los namespaces (kubectl get namespaces)
       b. Por cada namespace: listar TODOS los secrets y extraer
          TODOS los certificados que contengan (1 por CRT, 1 por cada
          alias de cada JKS, con su password)
  2. Aplanar todo a un payload estructurado (jks_export_service)
  3. Guardar JSON + Excel
  4. Retornar el payload para que la API lo sirva (ej. a Zabbix)
"""

import os
import asyncio
import traceback
from datetime import datetime, timezone

from config.settings import OUTPUT_DIR
from services.azure_auth_service import AzureAuthService
from services.k8s_service import K8sService
from services.jks_export_service import build_discovery_payload
from services.legacy_discovery_service import LegacyDiscoveryService
from models.k8s_cert import K8sCert
from views.reporter import save_json, save_excel, build_output_paths, print_banner


class JksDiscoveryController:
    """
    Recorre todos los clusters configurados y hace un escaneo masivo de
    namespaces â†’ secrets â†’ certificados (CRT y cada alias de cada JKS).

    No depende del portal DevOps en ningún punto del flujo.
    
    RESILIENCIA: Ningún error individual detiene el flujo completo.
    Los errores se registran y el escaneo continúa con los demás clusters/namespaces.
    """

    async def run(self, run_filter: dict | None = None) -> dict:
        if not run_filter:
            run_filter = {"mode": "all"}

        filter_mode = run_filter.get("mode", "all")
        svc = K8sService()

        all_certs: list[K8sCert] = []
        cluster_summaries: list[dict] = []
        errors_log: list[str] = []

        # â”€â”€ Autenticación Azure â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        auth_svc = AzureAuthService()
        if not auth_svc.ensure_az_session():
            msg = "[ERROR] No se pudo asegurar la sesión de Azure. Abortando proceso."
            print(msg)
            return {"error": "Azure session failed", "errors": [msg], "payload": [], "total_certs": 0}

        # â”€â”€ Extensión Resource Graph (no-fatal si falla) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        try:
            if not auth_svc.ensure_resource_graph_extension():
                msg = "[WARN] No se pudo instalar resource-graph. Intentando continuar de todas formas..."
                print(msg)
                errors_log.append(msg)
        except Exception as e:
            msg = f"[WARN] Excepción en resource-graph extension: {e}. Continuando..."
            print(msg)
            errors_log.append(msg)

        # â”€â”€ Descubrimiento Dinámico de Clusters â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
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

        # â”€â”€ Filtrar clusters si aplica â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        cluster_list = []
        for c in discovered_clusters:
            name = c.get("name", "").lower()
            
            # 1. Filtro por nombres específicos (si aplica)
            if filter_mode == "names":
                requested = [n.lower() for n in run_filter.get("names", [])]
                if name not in requested:
                    continue
            
            # 2. Omitir clusters de produccion por defecto
            if "prd" in name or "prod" in name:
                print(f"  [INFO] Omitiendo cluster de produccion: {name}")
                continue
                
            cluster_list.append(c)

        print(f"\n[*] Iniciando exploracion masiva de certificados (CRT + JKS)")
        print(f"    {len(cluster_list)} cluster(s) a procesar (de {len(discovered_clusters)} descubiertos en el tenant)\n")

        # â”€â”€ Iniciar Escaneo de Servidores Legacy en paralelo â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        legacy_task = None
        try:
            legacy_service = LegacyDiscoveryService()
            legacy_task = asyncio.create_task(legacy_service.scan_all())
        except Exception as e:
            msg = f"[WARN] No se pudo iniciar escaneo Legacy: {e}"
            print(msg)
            errors_log.append(msg)

        # â”€â”€ Recorrer cada cluster (AKS) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        for cluster_meta in cluster_list:
            cluster_name = cluster_meta.get("name", "desconocido")
            try:
                # Permitir que el event loop respire
                await asyncio.sleep(0.1)

                sub_id = cluster_meta.get("subscriptionId")
                rg = cluster_meta.get("resourceGroup")
                
                if not sub_id or not rg:
                    print(f"  [!] Ignorando {cluster_name}: Faltan metadatos de subscripción o resource group.")
                    cluster_summaries.append({"cluster": cluster_name, "error": "Faltan metadatos", "certs": 0})
                    continue
                    
                ctx = cluster_name

                print(f"\n[->] Preparando clúster: {cluster_name}")

                # 1. Pre-flight RBAC Check
                if not auth_svc.preflight_rbac_check(sub_id, rg, cluster_name):
                    print(f"  [!] Ignorando {cluster_name}: No hay roles asignados o error RBAC.")
                    cluster_summaries.append({"cluster": cluster_name, "error": "No RBAC", "certs": 0})
                    continue

                # 2. Set Subscription
                if not auth_svc.set_subscription(sub_id):
                    print(f"  [!] Ignorando {cluster_name}: No se pudo setear la subscripción.")
                    cluster_summaries.append({"cluster": cluster_name, "error": "Subscription Error", "certs": 0})
                    continue

                # 3. Configure Context (az aks get-credentials + kubelogin)
                if not auth_svc.configure_cluster_context(rg, cluster_name):
                    print(f"  [!] Ignorando {cluster_name}: Falló la descarga de credenciales.")
                    cluster_summaries.append({"cluster": cluster_name, "error": "Kubeconfig Error", "certs": 0})
                    continue

                # 4. Final auth verification
                if not auth_svc.auth_cani_check(ctx):
                    print(f"  [!] Ignorando {cluster_name}: No se puede listar pods en kube-system.")
                    cluster_summaries.append({"cluster": cluster_name, "error": "Auth Denied", "certs": 0})
                    continue

                # 5. Extract K8s Certs
                print(f"  [{cluster_name}] Iniciando escaneo de namespaces (JKS y TLS)...")
                cluster_cert_count = 0
                k8s_svc = K8sService()
                namespaces = k8s_svc.list_namespaces(cluster_name)
                if not namespaces:
                    print(f"  [{cluster_name}] âš  inaccesible o sin namespaces â€” se omite")
                    cluster_summaries.append({
                        "cluster": cluster_name, "namespaces": 0,
                        "certs": 0, "error": "inaccesible",
                    })
                    continue

                print(f"  [{cluster_name}] {len(namespaces)} namespace(s) detectados")

                for ns in namespaces:
                    try:
                        # Ceder control al event loop
                        await asyncio.sleep(0.05)

                        certs = k8s_svc.list_secrets_with_all_certs(cluster_name, ns)
                        if certs:
                            print(f"    [{cluster_name}/{ns}] {len(certs)} certificado(s)/alias encontrados")
                            for cert in certs:
                                cert.cluster = cluster_name
                                all_certs.append(cert)
                                cluster_cert_count += 1
                    except Exception as ns_err:
                        msg = f"  [WARN] Error en namespace {cluster_name}/{ns}: {ns_err}"
                        print(msg)
                        errors_log.append(msg)
                        continue
                    
                    # Check limits
                    if filter_mode == "limit" and len(all_certs) >= run_filter["limit"]:
                        break

                cluster_summaries.append({
                    "cluster": cluster_name,
                    "namespaces": len(namespaces),
                    "certs": cluster_cert_count,
                })

                if filter_mode == "limit" and len(all_certs) >= run_filter["limit"]:
                    break

            except Exception as cluster_err:
                msg = f"[ERROR] Excepción procesando cluster {cluster_name}: {cluster_err}"
                print(msg)
                print(traceback.format_exc())
                errors_log.append(msg)
                cluster_summaries.append({"cluster": cluster_name, "error": str(cluster_err), "certs": 0})
                continue

        if filter_mode == "limit":
            all_certs = all_certs[: run_filter["limit"]]

        # â”€â”€ Estructurar resultado AKS â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        try:
            payload = build_discovery_payload(all_certs)
        except Exception as e:
            msg = f"[ERROR] Error construyendo payload: {e}"
            print(msg)
            errors_log.append(msg)
            payload = []
        
        jks_count = len(payload)
        
        # Opcional: contar vencidos comparando fechas si es necesario
        now_utc = datetime.now(timezone.utc)
        vencidos = 0
        for p in payload:
            if p.get("fecha_vencimiento_certificado"):
                try:
                    fv = datetime.fromisoformat(p["fecha_vencimiento_certificado"])
                    if fv < now_utc:
                        vencidos += 1
                except:
                    pass

        # â”€â”€ Esperar resultado de Servidores Legacy â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        legacy_payload = []
        if legacy_task:
            try:
                print("\n[INFO] Esperando a que termine el escaneo de Servidores Legacy...")
                legacy_payload = await asyncio.wait_for(legacy_task, timeout=300)
                if legacy_payload is None:
                    legacy_payload = []
            except asyncio.TimeoutError:
                msg = "[WARN] Timeout esperando escaneo Legacy (5 min). Continuando sin datos Legacy."
                print(msg)
                errors_log.append(msg)
            except Exception as e:
                msg = f"[WARN] Error en escaneo Legacy: {e}. Continuando sin datos Legacy."
                print(msg)
                errors_log.append(msg)

        # â”€â”€ Guardar reportes â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        try:
            paths = build_output_paths(OUTPUT_DIR, "jks_discovery")
            # Generar JSON de Legacy
            legacy_json_path = paths["json"].replace("jks_discovery", "legacy_servers")
            
            save_json(payload, paths["json"])
            save_json(legacy_payload, legacy_json_path)
            
            # Generar Excel unificado (con ambas pestañas)
            unified_data = {
                "AKS": payload,
                "Servidores": legacy_payload
            }
            save_excel(unified_data, paths["excel"])

            print_banner(
                f"Exploracion masiva completada\n"
                f"  Clusters: {len(cluster_list)} | JKS encontrados: {len(payload)} "
                f"(Vencidos: {vencidos})\n"
                f"  Servidores Legacy: {len(legacy_payload)}\n"
                f"  Reporte JSON AKS : {os.path.abspath(paths['json'])}\n"
                f"  Reporte JSON Leg : {os.path.abspath(legacy_json_path)}\n"
                f"  Reporte EXCEL    : {os.path.abspath(paths['excel'])}"
            )
        except Exception as e:
            msg = f"[ERROR] Error guardando reportes: {e}"
            print(msg)
            errors_log.append(msg)

        if errors_log:
            print(f"\n[RESUMEN] Se registraron {len(errors_log)} advertencia(s)/error(es) durante el escaneo:")
            for err in errors_log:
                print(f"  â€¢ {err}")

        return {
            "payload": payload,
            "cluster_summaries": cluster_summaries,
            "total_certs": len(payload),
            "jks_count": jks_count,
            "vencidos": vencidos,
            "errors": errors_log,
        }
