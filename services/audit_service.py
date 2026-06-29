"""
services/audit_service.py
──────────────────────────
Servicio de auditoría: registra toda la actividad de la automatización.

Genera dos archivos en output/audit/:
  1. audit_YYYY-MM.json  → log estructurado acumulativo (una sesión por entrada)
  2. audit_YYYY-MM.log   → log legible por humanos (texto plano)

El JSON puede ser importado fácilmente en Excel, Power BI o cualquier herramienta.
El .log es legible directamente con cualquier editor.

Uso típico:
    audit = AuditService(mode="k8s-sync")
    audit.session.add(EventType.SCRAPE_OK, "44 certs extraídos", count=44)
    audit.close(status="OK", uploaded=5, failed=0)
"""

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from config.settings import BHD_USERNAME, OUTPUT_DIR
from models.audit_log import AuditSession, AuditEvent, EventType

AUDIT_DIR = os.path.join(OUTPUT_DIR, "audit")


def _audit_paths(now: datetime) -> tuple[str, str]:
    """Retorna las rutas del JSON y .log del mes actual."""
    month = now.strftime("%Y-%m")
    os.makedirs(AUDIT_DIR, exist_ok=True)
    return (
        os.path.join(AUDIT_DIR, f"audit_{month}.json"),
        os.path.join(AUDIT_DIR, f"audit_{month}.log"),
    )


def _load_json_log(path: str) -> list[dict]:
    """Lee el archivo JSON del mes o retorna lista vacía."""
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _fmt_ts(iso: str) -> str:
    """Formatea un timestamp ISO a formato legible: 2025-01-15 14:30:00 UTC"""
    try:
        dt = datetime.fromisoformat(iso)
        return dt.strftime("%Y-%m-%d %H:%M:%S UTC")
    except Exception:
        return iso


def _write_log_entry(log_path: str, session: AuditSession) -> None:
    """Escribe la sesión como bloque de texto legible al .log."""
    lines = [
        "",
        "=" * 70,
        f"  SESIÓN : {session.session_id}",
        f"  MODO   : {session.mode.upper()}",
        f"  USUARIO: {session.user or '(no configurado)'}",
        f"  INICIO : {_fmt_ts(session.started_at)}",
        f"  FIN    : {_fmt_ts(session.ended_at) if session.ended_at else 'EN CURSO'}",
        f"  ESTADO : {session.status}",
    ]

    if session.summary:
        lines.append("  RESUMEN:")
        for k, v in session.summary.items():
            lines.append(f"    {k:<30}: {v}")

    lines.append("  EVENTOS:")
    for ev in session.events:
        prefix = {
            "SESSION_START": "▶",
            "SESSION_END": "■",
            "AUTH_OK": "🔑",
            "AUTH_FAIL": "✗",
            "UPLOAD_OK": "✓",
            "UPLOAD_FAIL": "✗",
            "ERROR": "⚠",
            "FILE_SAVED": "💾",
            "DIFF_NEEDS_UPDATE": "↑",
        }.get(ev.event_type, "·")

        ts = _fmt_ts(ev.timestamp)
        line = f"    [{ts}] {prefix} [{ev.event_type}] {ev.message}"
        lines.append(line)
        # Detalles extra (si los hay y no son triviales)
        if ev.details:
            for dk, dv in ev.details.items():
                lines.append(f"      {dk}: {dv}")

    lines.append("=" * 70)

    with open(log_path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


class AuditService:
    """
    Administra el ciclo de vida de una sesión de auditoría.

    Uso:
        audit = AuditService(mode="k8s-sync")
        audit.session.add(EventType.SCRAPE_OK, "Datos extraídos", count=44)
        audit.close(status="OK", total=44, uploaded=5)
    """

    def __init__(self, mode: str):
        now = datetime.now(timezone.utc)
        self._json_path, self._log_path = _audit_paths(now)

        self.session = AuditSession(
            session_id=str(uuid.uuid4())[:8].upper(),
            mode=mode,
            user=BHD_USERNAME or os.getenv("USERNAME", "desconocido"),
        )

        # Registrar inicio de sesión
        self.session.add(
            EventType.SESSION_START,
            f"Automatización iniciada en modo '{mode}'",
            user=self.session.user,
            session_id=self.session.session_id,
        )

        print(f"\n  [Audit] Sesión {self.session.session_id} iniciada → {self._log_path}")

    def log(self, event_type: EventType, message: str = "", **details) -> None:
        """Atajo para agregar un evento a la sesión activa."""
        self.session.add(event_type, message, **details)

    def close(self, status: str = "OK", **summary_fields) -> None:
        """
        Cierra la sesión, escribe el JSON acumulativo y el .log legible.
        Llamar siempre al final, incluso si hubo error (usar try/finally).
        """
        self.session.close(status=status, **summary_fields)

        # ── Persistir JSON acumulativo ──
        sessions = _load_json_log(self._json_path)
        sessions.append(self.session.to_dict())
        with open(self._json_path, "w", encoding="utf-8") as f:
            json.dump(sessions, f, ensure_ascii=False, indent=2)

        # ── Persistir .log legible ──
        _write_log_entry(self._log_path, self.session)

        print(f"  [Audit] ✓ Log guardado:")
        print(f"    JSON : {self._json_path}")
        print(f"    LOG  : {self._log_path}")
