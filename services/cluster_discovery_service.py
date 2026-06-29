"""
services/cluster_discovery_service.py
───────────────────────────────────────
Descubre y valida todos los clusters kubernetes disponibles.

Flujo:
  1. Extraer clusters únicos de la lista de PortalK8sCert
  2. Verificar cuáles tienen un contexto kubectl accesible
  3. Reportar cuáles están disponibles y cuáles no

Esto garantiza que el sync procese TODOS los clusters del portal,
no solo los que están hardcodeados en settings.py.

Nota: Si un cluster retorna Forbidden al listar namespaces, se marca
como "acceso parcial" (reachable=True, partial_access=True). Esto
permite que _deep_search_secret intente acceder directamente a secrets
individuales, ya que el usuario puede tener permisos a nivel de secret
aunque no pueda listar namespaces globalmente.
"""

import subprocess
from dataclasses import dataclass
from typing import Optional

from config.settings import K8S_CLUSTERS, KUBECTL_TIMEOUT
from models.k8s_cert import PortalK8sCert


@dataclass
class ClusterStatus:
    cluster_name: str       # nombre tal como aparece en el portal
    kubectl_context: str    # contexto kubectl configurado
    reachable: bool         # True si kubectl puede conectarse (total o parcial)
    error: str = ""         # mensaje de error si no es accesible
    namespace_count: int = 0  # número de namespaces detectados
    partial_access: bool = False  # True = Forbidden en namespace listing, pero puede tener acceso a secrets


class ClusterDiscoveryService:
    """
    Descubre todos los clusters únicos a partir de los certs del portal
    y valida su accesibilidad via kubectl.
    """

    def discover_from_portal(
        self, portal_certs: list[PortalK8sCert]
    ) -> dict[str, ClusterStatus]:
        """
        Extrae clusters únicos de la lista de certs del portal,
        resuelve su contexto kubectl y verifica accesibilidad.

        Retorna: {cluster_name: ClusterStatus}
        """
        unique_clusters = sorted({c.cluster for c in portal_certs if c.cluster})
        print(f"\n[Discovery] {len(unique_clusters)} cluster(s) encontrados en el portal:")

        statuses: dict[str, ClusterStatus] = {}

        for cluster_name in unique_clusters:
            status = self._check_cluster(cluster_name)
            statuses[cluster_name] = status

            if status.reachable and not status.partial_access:
                icon = "OK"
                detail = f" -> {status.namespace_count} namespaces"
            elif status.partial_access:
                icon = "PARCIAL"
                detail = " -> Forbidden en namespaces, pero intentará acceso directo a secrets"
            else:
                icon = "FAIL"
                detail = f" -> {status.error}" if status.error else ""

            print(f"  [{icon}] {cluster_name}{detail}")

        reachable_full = sum(1 for s in statuses.values() if s.reachable and not s.partial_access)
        reachable_partial = sum(1 for s in statuses.values() if s.partial_access)
        unreachable = sum(1 for s in statuses.values() if not s.reachable)

        parts = []
        if reachable_full:
            parts.append(f"{reachable_full} OK")
        if reachable_partial:
            parts.append(f"{reachable_partial} parcial")
        if unreachable:
            parts.append(f"{unreachable} inaccesible")

        print(
            f"\n  [Discovery] Clusters: {' | '.join(parts)}"
            f" (total: {len(unique_clusters)})"
        )
        return statuses

    def _check_cluster(self, cluster_name: str) -> ClusterStatus:
        """
        Verifica si un cluster es accesible via kubectl.

        Si kubectl get namespaces retorna Forbidden, se marca como
        acceso parcial (reachable=True, partial_access=True) en vez
        de descartarlo completamente.
        """
        # Resolver el contexto: buscar en K8S_CLUSTERS primero, luego usar el nombre directo
        context = K8S_CLUSTERS.get(cluster_name, cluster_name)

        try:
            result = subprocess.run(
                ["kubectl", "--context", context, "get", "namespaces",
                 "--no-headers", "-o", "custom-columns=NAME:.metadata.name"],
                capture_output=True,
                text=True,
                timeout=KUBECTL_TIMEOUT,
            )
            if result.returncode == 0:
                namespaces = [
                    line.strip()
                    for line in result.stdout.splitlines()
                    if line.strip()
                ]
                return ClusterStatus(
                    cluster_name=cluster_name,
                    kubectl_context=context,
                    reachable=True,
                    namespace_count=len(namespaces),
                )
            else:
                stderr = result.stderr.strip()
                # ── Forbidden = acceso parcial (no descartar) ─────────
                if any(kw in stderr for kw in [
                    "Forbidden", "not allowed", "disable local accounts",
                    "cannot list resource"
                ]):
                    return ClusterStatus(
                        cluster_name=cluster_name,
                        kubectl_context=context,
                        reachable=True,           # ← clave: no descartar
                        partial_access=True,
                        error="Forbidden en listado de namespaces",
                    )
                # ── Otros errores = cluster no accesible ──────────────
                return ClusterStatus(
                    cluster_name=cluster_name,
                    kubectl_context=context,
                    reachable=False,
                    error=stderr[:120],
                )
        except subprocess.TimeoutExpired:
            return ClusterStatus(
                cluster_name=cluster_name,
                kubectl_context=context,
                reachable=False,
                error=f"Timeout ({KUBECTL_TIMEOUT}s)",
            )
        except FileNotFoundError:
            return ClusterStatus(
                cluster_name=cluster_name,
                kubectl_context=context,
                reachable=False,
                error="kubectl no encontrado en PATH",
            )
        except Exception as exc:
            return ClusterStatus(
                cluster_name=cluster_name,
                kubectl_context=context,
                reachable=False,
                error=str(exc)[:120],
            )

    def filter_reachable(
        self,
        portal_certs: list[PortalK8sCert],
        statuses: dict[str, ClusterStatus],
    ) -> tuple[list[PortalK8sCert], list[PortalK8sCert]]:
        """
        Separa la lista de certs del portal en:
          - processable: certs cuyos clusters son accesibles (total o parcial)
          - skipped:     certs cuyos clusters no son accesibles

        Retorna: (processable, skipped)
        """
        processable = [
            c for c in portal_certs
            if statuses.get(c.cluster, ClusterStatus(c.cluster, c.cluster, False)).reachable
        ]
        skipped = [
            c for c in portal_certs
            if not statuses.get(c.cluster, ClusterStatus(c.cluster, c.cluster, False)).reachable
        ]
        return processable, skipped
