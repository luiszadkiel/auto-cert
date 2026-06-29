"""
models/broker_cert.py
──────────────────────
Modelos de datos para el tab Brokers del portal.

- BrokerRow       : fila principal de la tabla Brokers (archivo JKS)
- BrokerCertDetail: certificado individual dentro del JKS (del modal de detalle)
- BrokerUploadJob : trabajo de carga — une un BrokerRow + un BrokerCertDetail
                    con la ruta del archivo .crt a subir
"""

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional

PORTAL_DATE_FMT = "%m/%d/%Y %H:%M:%S"


def _parse_date(raw: str) -> Optional[datetime]:
    try:
        return datetime.strptime(raw.strip(), PORTAL_DATE_FMT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


@dataclass(unsafe_hash=True)
class BrokerRow:
    """
    Una fila de la tabla Brokers.
    Representa un archivo JKS con su resumen de estados.
    """
    jks_name: str            # Nombre Archivo JKS
    ambiente: str            # DEV | SQA | PROD ...
    total_certs: int         # Cantidad De Certificados
    active: int              # Activos (parseado del badge)
    expired: int             # Vencidos (parseado del badge)

    @classmethod
    def from_row(cls, row: dict) -> "BrokerRow":
        """Construye desde una fila cruda del scraper de la tabla principal."""
        estado_raw = row.get("Estado General De Los Certificados", "")
        active, expired = _parse_estado(estado_raw)
        try:
            total = int(row.get("Cantidad De Certificados", "0").strip())
        except ValueError:
            total = 0
        return cls(
            jks_name=row.get("Nombre Archivo JKS", "").strip(),
            ambiente=row.get("Ambiente", "").strip(),
            total_certs=total,
            active=active,
            expired=expired,
        )

    def needs_attention(self) -> bool:
        """True si tiene al menos un certificado vencido."""
        return self.expired > 0

    def to_dict(self) -> dict:
        return asdict(self)


def _parse_estado(raw: str) -> tuple[int, int]:
    """
    Extrae los conteos de activos y vencidos del texto del badge.
    Ejemplo: '2 Activos | 4 Vencidos' → (2, 4)
    """
    import re
    active = expired = 0
    m = re.search(r"(\d+)\s*Activos?", raw, re.IGNORECASE)
    if m:
        active = int(m.group(1))
    m = re.search(r"(\d+)\s*Vencidos?", raw, re.IGNORECASE)
    if m:
        expired = int(m.group(1))
    return active, expired


@dataclass
class BrokerCertDetail:
    """
    Un certificado individual dentro de un JKS (visible en el modal de detalle).
    """
    issued_to: str                    # Certificado Emitido (CN)
    created_at: Optional[datetime]
    expires_at: Optional[datetime]
    status: str                       # "Activo" | "Vencido"

    @classmethod
    def from_row(cls, cells: list[str]) -> "BrokerCertDetail":
        """Construye desde las 4 celdas de la tabla del modal."""
        created = _parse_date(cells[0]) if len(cells) > 0 else None
        expires = _parse_date(cells[1]) if len(cells) > 1 else None
        issued = cells[2].strip() if len(cells) > 2 else ""
        status = cells[3].strip() if len(cells) > 3 else ""
        return cls(
            issued_to=issued,
            created_at=created,
            expires_at=expires,
            status=status,
        )

    def is_expired(self) -> bool:
        return "vencido" in self.status.lower()

    def to_dict(self) -> dict:
        return {
            "issued_to": self.issued_to,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "status": self.status,
        }


@dataclass
class BrokerUploadJob:
    """
    Trabajo de carga: representa un certificado a subir para un JKS específico.
    Se crea cuando se detecta un cert vencido y hay un .crt disponible para subir.
    """
    broker_row: BrokerRow
    cert_detail: BrokerCertDetail      # cert vencido a reemplazar
    cert_file_path: str                # ruta del .crt/.cert a subir
    alias: str                         # alias a usar en el JKS
    email: str                         # correo interesados

    def to_dict(self) -> dict:
        return {
            "jks_name": self.broker_row.jks_name,
            "ambiente": self.broker_row.ambiente,
            "cert_issued_to": self.cert_detail.issued_to,
            "cert_expires_at": self.cert_detail.expires_at.isoformat()
                if self.cert_detail.expires_at else None,
            "cert_file": self.cert_file_path,
            "alias": self.alias,
            "email": self.email,
        }
