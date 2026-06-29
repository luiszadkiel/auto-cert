"""
controllers/jks_discovery_controller.py
──────────────────────────────────────────
Controller: exploración masiva de TODOS los certificados (CRT y JKS) en
TODOS los clusters configurados (K8S_CLUSTERS), TODOS los namespaces,
TODOS los secrets.

Reemplaza a K8sSyncController para el nuevo flujo: NO hay login al portal,
NO hay scraping, NO hay diff contra el portal, NO hay upload. La única
responsabilidad es: descubrir → extraer → estructurar.

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
    namespaces → secrets → certificados (CRT y cada alias de cada JKS).

    No depende del portal DevOps en ningún punto del flujo.
    """

    async def run(self, run_filter: dict | None = None) -> dict:
        if not run_filter:
            run_filter = {"mode": "all"}

        filter_mode = run_filter["mode"]
        svc = K8sService()

        all_certs: list[K8sCert] = []
        cluster_summaries: list[dict] = []

        auth_svc = AzureAuthService()
        if not auth_svc.ensure_az_session():
            print("[ERROR] No se pudo asegurar la sesión de Azure. Abortando proceso.")
            return {"error": "Azure session failed"}

        if not auth_svc.ensure_resource_graph_extension():
            print("[ERROR] No se pudo instalar o asegurar resource-graph. Abortando proceso.")
            return {"error": "Azure Resource Graph extension missing"}

        # ── Descubrimiento Dinámico de Clusters ───────────────────────────────
        discovered_clusters = auth_svc.discover_all_aks_clusters()
        if not discovered_clusters:
            print("[ERROR] No se descubrieron clústeres en Azure. Abortando.")
            return {"error": "No clusters discovered"}

        # ── Filtrar clusters si aplica ────────────────────────────────────────
        if filter_mode == "names":
            requested = [n.lower() for n in run_filter["names"]]
            cluster_list = [c for c in discovered_clusters if c.get("name", "").lower() in requested]
        else:
            cluster_list = discovered_clusters

        print(f"\n[*] Iniciando exploracion masiva de certificados (CRT + JKS)")
        print(f"    {len(cluster_list)} cluster(s) a procesar (de {len(discovered_clusters)} descubiertos en el tenant)\n")

        # ── Recorrer cada cluster ───────────────────────────────────────────
        for cluster_meta in cluster_list:
            cluster_name = cluster_meta.get("name")
            sub_id = cluster_meta.get("subscriptionId")
            rg = cluster_meta.get("resourceGroup")
            ctx = cluster_name  # AKS credentials typically map context to cluster name

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
                print(f"  [!] Ignorando {cluster_name}: Error al configurar el contexto local.")
                cluster_summaries.append({"cluster": cluster_name, "error": "Context Config Error", "certs": 0})
                continue

            # 4. Verificación Real con kubectl auth can-i
            if not auth_svc.auth_cani_check(ctx):
                is_azure_rbac = auth_svc.check_azure_rbac_enabled(rg, cluster_name)
                if is_azure_rbac:
                    print(f"  [!] Ignorando {cluster_name}: Sin permisos. Requiere ROL DE AZURE ('Azure Kubernetes Service RBAC Reader').")
                    cluster_summaries.append({"cluster": cluster_name, "error": "Forbidden (Requires Azure RBAC)", "certs": 0})
                else:
                    print(f"  [!] Ignorando {cluster_name}: Sin permisos. Requiere ROL NATIVO (ClusterRoleBinding interno).")
                    cluster_summaries.append({"cluster": cluster_name, "error": "Forbidden (Requires Native RBAC)", "certs": 0})
                continue

            # Ahora sí procedemos al escaneo
            namespaces = svc.list_namespaces(cluster_name)

            if not namespaces:
                print(f"  [{cluster_name}] ⚠ inaccesible o sin namespaces — se omite")

                cluster_summaries.append({
                    "cluster": cluster_name, "namespaces": 0,
                    "certs": 0, "error": "inaccesible",
                })
                continue

            print(f"  [{cluster_name}] {len(namespaces)} namespace(s) detectados")
            cluster_cert_count = 0

            for ns in namespaces:
                if filter_mode == "limit" and len(all_certs) >= run_filter["limit"]:
                    break

                certs = svc.list_secrets_with_all_certs(cluster_name, ns)
                if certs:
                    print(f"    [{cluster_name}/{ns}] {len(certs)} certificado(s)/alias encontrados")
                all_certs.extend(certs)
                cluster_cert_count += len(certs)


            cluster_summaries.append({
                "cluster": cluster_name,
                "namespaces": len(namespaces),
                "certs": cluster_cert_count,
            })

            if filter_mode == "limit" and len(all_certs) >= run_filter["limit"]:
                break

        if filter_mode == "limit":
            all_certs = all_certs[: run_filter["limit"]]

        # ── Estructurar resultado ───────────────────────────────────────────
        payload = build_discovery_payload(all_certs)
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

        # ── Escanear Servidores Legacy ───────────────────────────────────────
        legacy_service = LegacyDiscoveryService()
        legacy_payload = await legacy_service.scan_all()

        # ── Guardar reportes ─────────────────────────────────────────────────
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

        return {
            "payload": payload,
            "cluster_summaries": cluster_summaries,
            "total_certs": len(payload),
            "jks_count": jks_count,
            "vencidos": vencidos,
        }
