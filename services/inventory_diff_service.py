"""
services/inventory_diff_service.py
────────────────────────────────────
Servicio de comparación triple: Excel × Portal × Cluster.

Cruza las tres fuentes de datos y determina el estado real de cada certificado:
  - Activo:         fecha cluster == portal/excel, notAfter > hoy
  - Vencido:        fecha cluster == portal/excel, notAfter < hoy
  - Renovado:       fecha cluster > portal/excel (cert fue renovado en el cluster)
  - No encontrado:  existe en excel/portal pero no en cluster
  - Nuevo:          existe en cluster pero no en excel/portal
"""

from datetime import datetime, timezone
from typing import Optional

from models.inventory_cert import (
    InventoryCert,
    normalize_cn,
    STATUS_ACTIVO,
    STATUS_VENCIDO,
    STATUS_RENOVADO,
    STATUS_NO_ENCONTRADO,
    STATUS_NUEVO,
)
from models.k8s_cert import K8sCert, PortalK8sCert


def _utc(dt: Optional[datetime]) -> Optional[datetime]:
    """Asegura que un datetime sea UTC-aware."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _portal_to_match_key(cert: PortalK8sCert) -> str:
    """Genera match_key para un cert del portal."""
    cn = normalize_cn(cert.cert_name)
    return (
        f"{cert.cluster.strip().upper()}|"
        f"{cert.namespace.strip().lower()}|"
        f"{cert.secret_name.strip().lower()}|"
        f"{cn}"
    )


def _k8s_to_match_key(cert: K8sCert) -> str:
    """Genera match_key para un cert del cluster."""
    cn = normalize_cn(cert.common_name)
    return (
        f"{cert.cluster.strip().upper()}|"
        f"{cert.namespace.strip().lower()}|"
        f"{cert.secret_name.strip().lower()}|"
        f"{cn}"
    )


class InventoryDiffService:
    """
    Comparación triple: Excel × Portal × Cluster.

    Recibe las 3 fuentes normalizadas y produce el inventario final
    con estados recalculados.
    """

    def compare_triple(
        self,
        excel_certs: list[InventoryCert],
        portal_certs: list[PortalK8sCert],
        cluster_certs: list[K8sCert],
    ) -> list[InventoryCert]:
        """
        Cruza las 3 fuentes y genera el inventario final.

        Args:
            excel_certs:   Certificados del Excel de entrada
            portal_certs:  Certificados del portal DevOps
            cluster_certs: Certificados del cluster (expandidos por alias en JKS)

        Returns:
            Lista de InventoryCert con estados recalculados.
        """
        now = datetime.now(timezone.utc)

        print(f"\n[Diff] Comparación triple:")
        print(f"  Excel:   {len(excel_certs)} registros")
        print(f"  Portal:  {len(portal_certs)} registros")
        print(f"  Cluster: {len(cluster_certs)} registros")

        # ── Indexar por match_key ─────────────────────────────────────────
        excel_index: dict[str, InventoryCert] = {}
        for cert in excel_certs:
            excel_index[cert.match_key] = cert

        portal_index: dict[str, PortalK8sCert] = {}
        for cert in portal_certs:
            key = _portal_to_match_key(cert)
            portal_index[key] = cert

        cluster_index: dict[str, K8sCert] = {}
        for cert in cluster_certs:
            key = _k8s_to_match_key(cert)
            cluster_index[key] = cert

        # ── Union de todas las keys ───────────────────────────────────────
        all_keys = set(excel_index.keys()) | set(portal_index.keys()) | set(cluster_index.keys())

        print(f"  Keys únicas: {len(all_keys)}")

        # ── Comparar cada key ─────────────────────────────────────────────
        results: list[InventoryCert] = []
        counters = {s: 0 for s in [STATUS_ACTIVO, STATUS_VENCIDO, STATUS_RENOVADO, STATUS_NO_ENCONTRADO, STATUS_NUEVO]}

        for key in sorted(all_keys):
            in_excel = key in excel_index
            in_portal = key in portal_index
            in_cluster = key in cluster_index

            result = self._resolve_one(
                key=key,
                excel_cert=excel_index.get(key),
                portal_cert=portal_index.get(key),
                cluster_cert=cluster_index.get(key),
                now=now,
            )
            results.append(result)
            counters[result.status] = counters.get(result.status, 0) + 1

        # ── Resumen ───────────────────────────────────────────────────────
        print(f"\n[Diff] Resultado de la comparación triple:")
        for status, count in counters.items():
            if count > 0:
                print(f"  {status}: {count}")
        print(f"  Total: {len(results)}")

        return results

    def _resolve_one(
        self,
        key: str,
        excel_cert: Optional[InventoryCert],
        portal_cert: Optional[PortalK8sCert],
        cluster_cert: Optional[K8sCert],
        now: datetime,
    ) -> InventoryCert:
        """Resuelve el estado de un certificado individual."""

        # ── Caso: existe en el cluster ────────────────────────────────────
        if cluster_cert is not None:
            cluster_exp = _utc(cluster_cert.not_after)
            cluster_bef = _utc(cluster_cert.not_before)

            # Datos base del cluster (fuente de verdad)
            base = InventoryCert(
                cluster=cluster_cert.cluster,
                ambiente=self._resolve_ambiente(cluster_cert.namespace, excel_cert),
                namespace=cluster_cert.namespace,
                secret_name=cluster_cert.secret_name,
                cert_name=cluster_cert.common_name,
                created_at=cluster_bef,
                expires_at=cluster_exp,
                secret_type=cluster_cert.secret_type,
                responsable=excel_cert.responsable if excel_cert else "",
                source="merged",
            )

            in_excel_or_portal = excel_cert is not None or portal_cert is not None

            if not in_excel_or_portal:
                # Nuevo: existe en cluster pero no en excel/portal
                base.status = STATUS_NUEVO
                return base

            # Comparar con la fecha más reciente conocida
            known_exp = self._best_known_expiry(excel_cert, portal_cert)

            if cluster_exp and known_exp and cluster_exp > known_exp:
                base.status = STATUS_RENOVADO
            elif cluster_exp and cluster_exp <= now:
                base.status = STATUS_VENCIDO
            else:
                base.status = STATUS_ACTIVO

            return base

        # ── Caso: NO existe en el cluster ─────────────────────────────────
        # Priorizar datos del Excel, luego del Portal
        if excel_cert is not None:
            result = InventoryCert(
                cluster=excel_cert.cluster,
                ambiente=excel_cert.ambiente,
                namespace=excel_cert.namespace,
                secret_name=excel_cert.secret_name,
                cert_name=excel_cert.cert_name,
                created_at=excel_cert.created_at,
                expires_at=excel_cert.expires_at,
                secret_type=excel_cert.secret_type,
                status=STATUS_NO_ENCONTRADO,
                responsable=excel_cert.responsable,
                source="excel",
            )
            return result

        if portal_cert is not None:
            result = InventoryCert(
                cluster=portal_cert.cluster,
                ambiente=portal_cert.ambiente,
                namespace=portal_cert.namespace,
                secret_name=portal_cert.secret_name,
                cert_name=portal_cert.cert_name,
                created_at=portal_cert.created_at,
                expires_at=portal_cert.expires_at,
                secret_type=portal_cert.secret_type,
                status=STATUS_NO_ENCONTRADO,
                responsable="",
                source="portal",
            )
            return result

        # No debería llegar aquí, pero por seguridad
        return InventoryCert(status=STATUS_NO_ENCONTRADO, source="unknown")

    def _resolve_ambiente(
        self,
        namespace: str,
        excel_cert: Optional[InventoryCert],
    ) -> str:
        """
        Resuelve el ambiente. Prioriza el del Excel si existe.
        Si no, intenta deducir del namespace.
        """
        if excel_cert and excel_cert.ambiente:
            return excel_cert.ambiente

        # Deducir del namespace: rollout-sqa → NoProd, mb-pty-prod → Prod
        ns_lower = namespace.lower()
        if "prod" in ns_lower and "noprod" not in ns_lower and "ppd" not in ns_lower:
            return "Prod"
        return "NoProd"

    def _best_known_expiry(
        self,
        excel_cert: Optional[InventoryCert],
        portal_cert: Optional[PortalK8sCert],
    ) -> Optional[datetime]:
        """Retorna la fecha de expiración más reciente entre Excel y Portal."""
        dates = []
        if excel_cert and excel_cert.expires_at:
            dates.append(_utc(excel_cert.expires_at))
        if portal_cert and portal_cert.expires_at:
            dates.append(_utc(portal_cert.expires_at))

        return max(dates) if dates else None
