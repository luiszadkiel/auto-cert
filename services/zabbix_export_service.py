"""
services/zabbix_export_service.py
──────────────────────────────────
Transforma los CertDiff del sync en un payload plano que Zabbix puede consumir.

Cada certificado incluye:
  - Identificación (cluster, namespace, secret_name, nombre_certificado, san)
  - Fechas (fecha_inicio, fecha_fin, dias_restantes)
  - Estado calculado (ACTIVO / POR_VENCER / CRITICO / VENCIDO / SIN_DATO)
  - Metadata (tipo_secreto, needs_update, actualizado_en_portal, razon)
"""

from datetime import datetime, timezone
from typing import Optional

from config.settings import CERT_DIAS_WARNING, CERT_DIAS_CRITICAL
from models.k8s_cert import CertDiff


def _clasificar_estado(dias: Optional[int]) -> str:
    """Clasifica el estado del certificado según días restantes."""
    if dias is None:
        return "SIN_DATO"
    if dias < 0:
        return "VENCIDO"
    if dias <= CERT_DIAS_CRITICAL:
        return "CRITICO"
    if dias <= CERT_DIAS_WARNING:
        return "POR_VENCER"
    return "ACTIVO"


def build_zabbix_payload(
    diffs: list[CertDiff],
    upload_results: Optional[dict[str, bool]] = None,
) -> list[dict]:
    """
    Construye el payload de certificados para Zabbix.

    Por cada CertDiff:
      - Calcula dias_restantes (puede ser negativo si ya venció)
      - Clasifica en estado: VENCIDO / CRITICO / POR_VENCER / ACTIVO / SIN_DATO
      - Prioriza fechas y nombre del cluster real sobre lo del portal

    Args:
        diffs: lista de CertDiff del sync
        upload_results: dict "namespace/secret" → True/False (resultado de subida)

    Returns:
        Lista de dicts listos para serializar a JSON.
    """
    if upload_results is None:
        upload_results = {}

    now_utc = datetime.now(timezone.utc)
    payload: list[dict] = []

    for diff in diffs:
        pc = diff.portal_cert
        cc = diff.cluster_cert

        # ── Fechas: priorizar cluster real, fallback al portal ────────────
        if cc and cc.not_after:
            not_after = cc.not_after
            not_before = cc.not_before
        elif pc.expires_at:
            not_after = pc.expires_at
            not_before = pc.created_at
        else:
            not_after = None
            not_before = None

        # ── Días restantes ────────────────────────────────────────────────
        if not_after:
            # Asegurar timezone-aware
            if not_after.tzinfo is None:
                not_after = not_after.replace(tzinfo=timezone.utc)
            dias_restantes = (not_after - now_utc).days
        else:
            dias_restantes = None

        estado = _clasificar_estado(dias_restantes)

        # ── Nombre del certificado ────────────────────────────────────────
        nombre_certificado = ""
        if cc and cc.common_name:
            nombre_certificado = cc.common_name
        elif pc.cert_name:
            nombre_certificado = pc.cert_name
        else:
            nombre_certificado = pc.secret_name

        # ── Resultado de subida ───────────────────────────────────────────
        short_name = f"{pc.namespace}/{pc.secret_name}"
        actualizado = upload_results.get(short_name)  # True/False/None

        payload.append({
            "cluster": cc.cluster if cc else pc.cluster,
            "ambiente": pc.ambiente,
            "namespace": cc.namespace if cc else pc.namespace,
            "secret_name": pc.secret_name,
            "nombre_certificado": nombre_certificado,
            "san": cc.san if cc else [],
            "tipo_secreto": cc.secret_type if cc else pc.secret_type,
            "fecha_inicio": not_before.isoformat() if not_before else None,
            "fecha_fin": not_after.isoformat() if not_after else None,
            "dias_restantes": dias_restantes,
            "estado": estado,
            "needs_update": diff.needs_update,
            "actualizado_en_portal": actualizado,
            "encontrado_en_cluster": cc is not None,
            "razon": diff.reason,
        })

    return payload
