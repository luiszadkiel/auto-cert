"""
models/inventory_cert.py
─────────────────────────
Modelo unificado del inventario de certificados.

Una instancia = una fila del Excel final.
Sirve como puente entre las 3 fuentes de datos (Excel, Portal, Cluster)
con una match_key normalizada para cruce.

Columnas del Excel de salida (mismo formato que el inventario original):
  Cluster | Ambiente | Namespace | Nombre del Secreto |
  Nombre del certificado | Fecha de Creación | Fecha de Vencimiento |
  Tipo de Secreto | Estado | Responsable
"""

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


# ─── Estados posibles ─────────────────────────────────────────────────────────

STATUS_ACTIVO = "Activo"
STATUS_VENCIDO = "Vencido"
STATUS_RENOVADO = "Renovado"
STATUS_NO_ENCONTRADO = "No encontrado"
STATUS_NUEVO = "Nuevo"

ALL_STATUSES = [
    STATUS_ACTIVO,
    STATUS_VENCIDO,
    STATUS_RENOVADO,
    STATUS_NO_ENCONTRADO,
    STATUS_NUEVO,
]

# ─── Columnas del Excel (orden exacto) ───────────────────────────────────────

EXCEL_COLUMNS = [
    "Cluster",
    "Ambiente",
    "Namespace",
    "Nombre del Secreto",
    "Nombre del certificado",
    "Fecha de Creación",
    "Fecha de Vencimiento",
    "Tipo de Secreto",
    "Estado",
    "Responsable",
]


# ─── Normalización de CN ─────────────────────────────────────────────────────

def normalize_cn(raw: str) -> str:
    """
    Normaliza un CN para matching entre fuentes.

    El CN puede venir en formatos distintos entre Excel, Portal y keytool:
      - 'CN=DigiCert Global Root G2, OU=www.digicert.com, O=DigiCert Inc, C=US'
      - 'DigiCert Global Root G2'
      - 'cn=digicert global root g2'

    Normalización:
      1. Extraer solo el valor del CN= si viene como parte de un Subject
      2. Lowercase
      3. Quitar espacios duplicados
    """
    if not raw:
        return ""

    s = raw.strip()

    # Si contiene "CN=", extraer solo el valor del CN
    match = re.search(r"CN=([^,]+)", s, re.IGNORECASE)
    if match:
        s = match.group(1).strip()

    # Normalizar
    s = s.lower()
    s = re.sub(r"\s+", " ", s)

    return s


# ─── Fechas ───────────────────────────────────────────────────────────────────

# Formatos que aparecen en el CSV (mezcla de formatos)
_DATE_FORMATS = [
    "%m/%d/%Y %H:%M:%S",    # 01/15/2038 08:00:00
    "%m/%d/%Y %H:%M",       # 1/3/2023 16:37
    "%Y-%m-%dT%H:%M:%S",    # 2023-01-03T16:37:00 (ISO)
]


def parse_flexible_date(raw: str) -> Optional[datetime]:
    """Parsea una fecha probando múltiples formatos. Retorna UTC-aware."""
    if not raw or not raw.strip():
        return None

    clean = raw.strip()
    for fmt in _DATE_FORMATS:
        try:
            dt = datetime.strptime(clean, fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue

    return None


# ─── Modelo principal ─────────────────────────────────────────────────────────

@dataclass
class InventoryCert:
    """
    Certificado del inventario unificado.

    Representa una fila del Excel final con datos cruzados de las 3 fuentes.
    """
    cluster: str = ""
    ambiente: str = ""
    namespace: str = ""
    secret_name: str = ""
    cert_name: str = ""                  # CN del certificado individual
    created_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    secret_type: str = ""                # "JKS" | "CRT"
    status: str = ""                     # Activo | Vencido | Renovado | No encontrado | Nuevo
    responsable: str = ""
    # ── Campos de control (no van al Excel de salida) ─────────────────────────
    source: str = ""                     # "excel" | "portal" | "cluster" | "merged"

    @property
    def match_key(self) -> str:
        """Key normalizada para cruce entre fuentes."""
        cn = normalize_cn(self.cert_name)
        return (
            f"{self.cluster.strip().upper()}|"
            f"{self.namespace.strip().lower()}|"
            f"{self.secret_name.strip().lower()}|"
            f"{cn}"
        )

    def to_excel_row(self) -> list:
        """Retorna los valores en el orden de EXCEL_COLUMNS."""
        return [
            self.cluster,
            self.ambiente,
            self.namespace,
            self.secret_name,
            self.cert_name,
            self.created_at.strftime("%m/%d/%Y %H:%M:%S") if self.created_at else "",
            self.expires_at.strftime("%m/%d/%Y %H:%M:%S") if self.expires_at else "",
            self.secret_type,
            self.status,
            self.responsable,
        ]

    def to_dict(self) -> dict:
        return {
            "cluster": self.cluster,
            "ambiente": self.ambiente,
            "namespace": self.namespace,
            "secret_name": self.secret_name,
            "cert_name": self.cert_name,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "secret_type": self.secret_type,
            "status": self.status,
            "responsable": self.responsable,
            "source": self.source,
            "match_key": self.match_key,
        }

    @classmethod
    def from_excel_row(cls, row: dict) -> "InventoryCert":
        """Construye desde un dict con las columnas del CSV/XLSX de entrada."""
        return cls(
            cluster=str(row.get("Cluster", "")).strip(),
            ambiente=str(row.get("Ambiente", "")).strip(),
            namespace=str(row.get("Namespace", "")).strip(),
            secret_name=str(row.get("Nombre del Secreto", "")).strip(),
            cert_name=str(row.get("Nombre del certificado", "")).strip(),
            created_at=parse_flexible_date(
                str(row.get("Fecha de Creación", row.get("Fecha de Creacion", "")))
            ),
            expires_at=parse_flexible_date(
                str(row.get("Fecha de Vencimiento", ""))
            ),
            secret_type=str(row.get("Tipo de Secreto", "")).strip(),
            status=str(row.get("Estado", "")).strip(),
            responsable=str(row.get("Responsable", "")).strip(),
            source="excel",
        )
