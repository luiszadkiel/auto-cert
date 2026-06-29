"""
models/audit_log.py
────────────────────
Modelos de datos para el registro de auditoría.

- AuditEvent   : una acción individual dentro de una sesión
- AuditSession : una ejecución completa del script (inicio → fin)
"""

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional, Any
from enum import Enum


class EventType(str, Enum):
    """Tipos de evento registrables."""
    # Sesión
    SESSION_START    = "SESSION_START"
    SESSION_END      = "SESSION_END"

    # Autenticación
    AUTH_AUTO        = "AUTH_AUTO"          # login automático
    AUTH_MANUAL      = "AUTH_MANUAL"        # login manual (esperando usuario)
    AUTH_OK          = "AUTH_OK"            # login completado
    AUTH_FAIL        = "AUTH_FAIL"          # login falló/timeout

    # Scraping del portal
    SCRAPE_START     = "SCRAPE_START"
    SCRAPE_OK        = "SCRAPE_OK"
    SCRAPE_EMPTY     = "SCRAPE_EMPTY"

    # Comparación K8s vs portal
    DIFF_ANALYZED    = "DIFF_ANALYZED"      # resultado del diff
    DIFF_NEEDS_UPDATE = "DIFF_NEEDS_UPDATE" # cert con fecha más reciente en cluster

    # Upload al portal
    UPLOAD_START     = "UPLOAD_START"
    UPLOAD_OK        = "UPLOAD_OK"
    UPLOAD_FAIL      = "UPLOAD_FAIL"

    # Exportación de archivos
    FILE_SAVED       = "FILE_SAVED"

    # Errores generales
    ERROR            = "ERROR"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class AuditEvent:
    """Una acción individual dentro de una sesión de automatización."""
    event_type: str                              # valor de EventType
    timestamp: str = field(default_factory=_now_iso)
    message: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class AuditSession:
    """
    Una ejecución completa del script.
    Se crea al iniciar y se cierra al terminar (con resumen).
    """
    session_id: str
    mode: str                                    # "certs" | "k8s-sync"
    user: str                                    # BHD_USERNAME del .env
    started_at: str = field(default_factory=_now_iso)
    ended_at: Optional[str] = None
    status: str = "RUNNING"                      # RUNNING | OK | ERROR
    events: list[AuditEvent] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)

    def add(self, event_type: EventType, message: str = "", **details) -> None:
        """Agrega un evento a la sesión."""
        self.events.append(AuditEvent(
            event_type=event_type.value,
            message=message,
            details=details,
        ))

    def close(self, status: str = "OK", **summary_fields) -> None:
        """Cierra la sesión con estado y resumen."""
        self.ended_at = _now_iso()
        self.status = status
        self.summary = summary_fields
        self.add(EventType.SESSION_END, f"Sesión finalizada con estado: {status}", **summary_fields)

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "mode": self.mode,
            "user": self.user,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "status": self.status,
            "summary": self.summary,
            "events": [e.to_dict() for e in self.events],
        }
