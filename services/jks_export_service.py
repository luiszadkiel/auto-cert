"""
services/jks_export_service.py
â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
Construye el payload estructurado y plano de TODOS los certificados (CRT y
cada alias de cada JKS) descubiertos en el escaneo masivo.

Reemplaza a zabbix_export_service.build_zabbix_payload para el nuevo flujo
SIN portal: ya no hay "needs_update" ni "actualizado_en_portal" porque no se
compara contra ningún registro externo ni se sube nada a ningún lado â€” esto
es pura exploración + estructuración.

Cada fila del payload incluye:
  - Identificación: cluster, ambiente, namespace, secret_name, tipo_secreto, alias
  - Certificado:    nombre_certificado, fecha_inicio, fecha_fin, dias_restantes, estado
  - Credencial:     password (solo aplica a JKS; vacío en CRT)
"""

from datetime import datetime, timezone
from typing import Optional

from config.settings import CERT_DIAS_WARNING, CERT_DIAS_CRITICAL
from models.k8s_cert import K8sCert
from services.k8s_service import _inferir_ambiente_de_namespace


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


def build_discovery_payload(all_certs: list[K8sCert]) -> list[dict]:
    """
    Aplana la lista de K8sCert (1 fila por cert CRT, 1 fila por CADA alias
    dentro de cada JKS) a dicts listos para JSON / Excel / consumo por Zabbix.

    Args:
        all_certs: certificados descubiertos en el escaneo masivo.

    Returns:
        Lista de dicts, uno por certificado/alias.
    """
    now_utc = datetime.now(timezone.utc)
    payload: list[dict] = []

    for cert in all_certs:
        # El requerimiento explícito es extraer los JKS.
        if cert.secret_type != "JKS":
            continue

        not_after = cert.not_after
        if not_after and not_after.tzinfo is None:
            not_after = not_after.replace(tzinfo=timezone.utc)

        # Calcular dias/horas vencidos o restantes
        dias_vencidos:   Optional[int] = None
        horas_vencidas:  Optional[int] = None
        dias_para_vencer: Optional[int] = None

        if not_after:
            delta = now_utc - not_after          # positivo si ya venció
            total_horas = int(delta.total_seconds() // 3600)
            if is_expired:
                dias_vencidos  = int(delta.total_seconds() // 86400)
                horas_vencidas = total_horas
            else:
                dias_para_vencer = int((-delta).total_seconds() // 86400)

        payload.append({
            "organizacion":                  getattr(cert, "organization", ""),
            "estructura_organizacional":      getattr(cert, "organizational_unit", ""),
            "nombre_certificado":             cert.common_name,
            "ambiente":                       _inferir_ambiente_de_namespace(cert.namespace) or "",
            "fecha_vencimiento_certificado":  not_after.isoformat() if not_after else None,
            "vencido":                        is_expired,
            "estado":                         estado,
            "dias_vencidos":                  dias_vencidos,
            "horas_vencidas":                 horas_vencidas,
            "dias_para_vencer":               dias_para_vencer,
            "proxima_fecha_vencimiento":      not_after.strftime("%Y-%m-%d") if (not_after and not is_expired) else None,
            "fecha_escaneo":                  now_utc.strftime("%Y-%m-%d %H:%M UTC"),
            "cluster":                        cert.cluster,
            "namespace":                      cert.namespace,
            "secreto_k8s":                    cert.secret_name,
            "archivo_jks":                    cert.data_key,
            "password":                       cert.password,
        })

    return payload
