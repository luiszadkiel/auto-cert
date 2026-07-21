"""
models/k8s_cert.py
â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
Modelos de datos para certificados Kubernetes.

- K8sCert       : certificado leído directamente del cluster (via kubectl)
- PortalK8sCert : [DEPRECADO] registro del portal â€” ya no se usa en el flujo
                  de exploración masiva (jks_discovery_controller), que no
                  depende del portal DevOps. Se deja por compatibilidad con
                  código que aún no haya sido migrado.
- CertDiff      : [DEPRECADO] resultado de comparar K8sCert vs PortalK8sCert.
"""

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional


# â”€â”€â”€ Fechas â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

PORTAL_DATE_FMT = "%m/%d/%Y %H:%M:%S"   # formato que usa el portal: 01/19/2022 12:16:37


def parse_portal_date(raw: str) -> Optional[datetime]:
    """Parsea una fecha en el formato del portal â†’ datetime UTC-aware."""
    try:
        return datetime.strptime(raw.strip(), PORTAL_DATE_FMT).replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None


# â”€â”€â”€ Modelos â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

@dataclass
class K8sCert:
    """
    Certificado extraído directamente del cluster Kubernetes (via kubectl).
    Representa la verdad actual en el cluster.

    Para un secret CRT: 1 instancia.
    Para un secret JKS: 1 instancia POR CADA alias dentro del keystore.
    """
    cluster: str
    namespace: str
    secret_name: str
    common_name: str                    # CN del Subject del certificado
    not_before: Optional[datetime]      # fecha de emisión (UTC)
    not_after: Optional[datetime]       # fecha de vencimiento (UTC)
    organization: str = ""              # Organización (O)
    organizational_unit: str = ""       # Unidad Organizacional (OU)
    secret_type: str                    # CRT | Opaque | JKS
    san: list = field(default_factory=list)  # Subject Alternative Names (DNS)
    cert_pem: bytes = field(default_factory=bytes, repr=False)  # raw PEM o JKS
    data_key: str = ""                  # key dentro de .data: "tls.crt", "brokerKeystore.jks"
    alias: str = ""                     # alias dentro del JKS (vacío para CRT)
    password: str = ""                  # password del keystore JKS (vacío para CRT)

    def to_dict(self) -> dict:
        return {
            "cluster": self.cluster,
            "namespace": self.namespace,
            "secret_name": self.secret_name,
            "common_name": self.common_name,
            "organization": self.organization,
            "organizational_unit": self.organizational_unit,
            "not_before": self.not_before.isoformat() if self.not_before else None,
            "not_after": self.not_after.isoformat() if self.not_after else None,
            "secret_type": self.secret_type,
            "san": self.san,
            "alias": self.alias,
            "password": self.password,
        }


@dataclass
class PortalK8sCert:
    """
    [DEPRECADO] Registro de certificado tal como aparece en la tabla
    Kubernetes del portal. Ya no se usa en el flujo de exploración masiva.
    """
    cluster: str
    ambiente: str
    namespace: str
    secret_name: str
    cert_name: str                       # columna "Nombre del certificado" (CN)
    created_at: Optional[datetime]
    expires_at: Optional[datetime]
    secret_type: str
    status: str                          # "Activo" | "Vencido"

    @classmethod
    def from_row(cls, row: dict) -> "PortalK8sCert":
        """Construye desde una fila cruda del scraper."""
        return cls(
            cluster=row.get("Nombre del Cluster", "").strip(),
            ambiente=row.get("Ambiente", "").strip(),
            namespace=row.get("Namespace", "").strip(),
            secret_name=row.get("Nombre del secreto en k8s", "").strip(),
            cert_name=row.get("Nombre del certificado", "").strip(),
            created_at=parse_portal_date(row.get("Fecha de Creacion", "")),
            expires_at=parse_portal_date(row.get("Fecha de Vencimiento", "")),
            secret_type=row.get("Tipo de secreto", "").strip(),
            status=row.get("Estado", "").strip(),
        )

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
        }


@dataclass
class CertDiff:
    """
    [DEPRECADO] Resultado de comparar un K8sCert (cluster) con un
    PortalK8sCert (portal). Ya no se usa en el flujo de exploración masiva.
    """
    portal_cert: PortalK8sCert
    cluster_cert: Optional[K8sCert]
    needs_update: bool
    reason: str                          # descripción legible de por qué

    def to_dict(self) -> dict:
        return {
            "namespace": self.portal_cert.namespace,
            "secret_name": self.portal_cert.secret_name,
            "needs_update": self.needs_update,
            "reason": self.reason,
            "portal_expires_at": self.portal_cert.expires_at.isoformat()
                if self.portal_cert.expires_at else None,
            "cluster_expires_at": self.cluster_cert.not_after.isoformat()
                if self.cluster_cert and self.cluster_cert.not_after else None,
        }
