"""
services/cert_diff_service.py
──────────────────────────────
Servicio de comparación: compara los certs del cluster con los del portal
y determina cuáles necesitan ser actualizados.

Lógica:
  Para cada PortalK8sCert:
    1. Buscar su secret en el cluster (via K8sService)
    2. Si el cluster tiene NOT_AFTER > portal EXPIRES_AT → cert renovado → needs_update=True
    3. Si el secret no existe en el cluster → marcar como no encontrado
    4. Si las fechas son iguales → sin cambio
"""

from datetime import timezone
from typing import Optional

from models.k8s_cert import PortalK8sCert, K8sCert, CertDiff
from services.k8s_service import K8sService


def _utc(dt):
    """Asegura que un datetime sea UTC-aware."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


class CertDiffService:
    def __init__(self):
        self.k8s = K8sService()

    def compare(self, portal_certs: list[PortalK8sCert]) -> list[CertDiff]:
        """
        Compara cada registro del portal contra el cluster.
        Retorna lista de CertDiff con el campo needs_update=True para los que
        tienen un cert más reciente en el cluster.
        """
        print(f"\n[Diff] Comparando {len(portal_certs)} registros del portal contra el cluster...")
        diffs: list[CertDiff] = []

        for portal_cert in portal_certs:
            diff = self._compare_one(portal_cert)
            diffs.append(diff)

        needs = sum(1 for d in diffs if d.needs_update)
        print(f"  [Diff] ✓ {needs} certificados necesitan actualización en el portal.")
        return diffs

    def _compare_one(self, portal_cert: PortalK8sCert) -> CertDiff:
        """Compara un PortalK8sCert individual contra el cluster."""
        cluster_cert: Optional[K8sCert] = self.k8s.get_secret(
            cluster_name=portal_cert.cluster,
            namespace=portal_cert.namespace,
            secret_name=portal_cert.secret_name,
            ambiente=portal_cert.ambiente,
        )

        # Secret no encontrado en el cluster
        if cluster_cert is None:
            return CertDiff(
                portal_cert=portal_cert,
                cluster_cert=None,
                needs_update=False,
                reason="Secret no encontrado en el cluster — sin acción.",
            )

        # Sin fechas para comparar
        portal_exp = _utc(portal_cert.expires_at)
        cluster_exp = _utc(cluster_cert.not_after)

        if portal_exp is None or cluster_exp is None:
            return CertDiff(
                portal_cert=portal_cert,
                cluster_cert=cluster_cert,
                needs_update=False,
                reason="No se pueden comparar fechas (alguna es None).",
            )

        # Comparar fechas
        if cluster_exp > portal_exp:
            delta_days = (cluster_exp - portal_exp).days
            return CertDiff(
                portal_cert=portal_cert,
                cluster_cert=cluster_cert,
                needs_update=True,
                reason=(
                    f"Cluster tiene cert más reciente: "
                    f"portal={portal_exp.date()} < cluster={cluster_exp.date()} "
                    f"(+{delta_days} días)"
                ),
            )

        return CertDiff(
            portal_cert=portal_cert,
            cluster_cert=cluster_cert,
            needs_update=False,
            reason=f"Fechas iguales o portal más reciente: {portal_exp.date()} >= {cluster_exp.date()}",
        )
