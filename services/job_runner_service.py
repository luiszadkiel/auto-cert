"""
services/job_runner_service.py
───────────────────────────────
Singleton (por proceso) que gestiona la ejecución del JksDiscoveryController
como una tarea en background de asyncio.

Diseñado para un solo worker de uvicorn — la "atomicidad" del chequeo
de estado + lanzamiento se logra porque no hay ningún `await` entre
el check de `_state.status == "running"` y la asignación a "running".

Guarda la referencia al asyncio.Task para evitar que el GC la destruya
a mitad de ejecución (comportamiento reproducido en pruebas).
"""

import asyncio
import json
import os
import traceback
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional

from config.settings import OUTPUT_DIR


@dataclass
class JobState:
    status: str = "idle"           # idle | running | ok | error
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    error: Optional[str] = None
    total_certs: int = 0           # CRT + alias JKS encontrados en total
    total_jks: int = 0             # cuántos de esos son alias dentro de un JKS
    total_crt: int = 0             # cuántos son CRT directos
    vencidos: int = 0              # cuántos ya vencieron (notAfter <= hoy)
    clusters_escaneados: int = 0


_PERSIST_PATH = os.path.join(OUTPUT_DIR, "last_run_state.json")


class JobRunnerService:
    """
    Gestiona la ejecución async de la exploración masiva de JKS/CRT.
    Todo classmethod — singleton de proceso (un solo worker uvicorn).
    """
    _state: JobState = JobState()
    _last_payload: list[dict] = []
    _running_task: Optional[asyncio.Task] = None  # referencia fuerte → evita GC

    @classmethod
    def try_trigger(cls) -> tuple[bool, str]:
        """
        Intenta lanzar una ejecución de la exploración masiva.

        Returns:
            (started, message)
            - (True, "...") si se lanzó con éxito
            - (False, "...") si ya había una corriendo
        """
        if cls._state.status == "running":
            return False, f"Ya hay una ejecución en curso (desde {cls._state.started_at})"

        # ── Atómico: sin await entre check y asignación ───────────────────
        cls._state = JobState(
            status="running",
            started_at=datetime.now(timezone.utc).isoformat(),
        )
        cls._running_task = asyncio.create_task(cls._run())

        return True, "Exploracion masiva de JKS/CRT iniciada en background"

    @classmethod
    async def _run(cls) -> None:
        """Ejecuta JksDiscoveryController y guarda el payload para la API."""
        try:
            from controllers.jks_discovery_controller import JksDiscoveryController
            from config.settings import get_run_filter

            controller = JksDiscoveryController()
            result = await controller.run(run_filter=get_run_filter())

            payload = result.get("payload", [])
            cls._last_payload = payload

            cls._state = JobState(
                status="ok",
                started_at=cls._state.started_at,
                finished_at=datetime.now(timezone.utc).isoformat(),
                total_certs=result.get("total_certs", len(payload)),
                total_jks=result.get("jks_count", 0),
                total_crt=result.get("crt_count", 0),
                vencidos=result.get("vencidos", 0),
                clusters_escaneados=len(result.get("cluster_summaries", [])),
            )

        except Exception:
            tb = traceback.format_exc()
            # Recortar a las últimas 3 líneas del traceback
            tb_short = "\n".join(tb.strip().splitlines()[-3:])

            cls._state = JobState(
                status="error",
                started_at=cls._state.started_at,
                finished_at=datetime.now(timezone.utc).isoformat(),
                error=tb_short,
            )
            # No borrar _last_payload — conservar el último conocido bueno

        finally:
            cls._running_task = None
            cls._persist()

    @classmethod
    def _persist(cls) -> None:
        """Escribe el estado y payload a disco para sobrevivir reinicios."""
        try:
            os.makedirs(os.path.dirname(_PERSIST_PATH) or ".", exist_ok=True)
            data = {
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "status": cls._state.status,
                "certificados": cls._last_payload,
            }
            with open(_PERSIST_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as exc:
            print(f"[JobRunner] ⚠ No se pudo persistir estado: {exc}")

    @classmethod
    def get_status(cls) -> dict:
        """Retorna el estado actual como dict serializable."""
        return {
            "status": cls._state.status,
            "started_at": cls._state.started_at,
            "finished_at": cls._state.finished_at,
            "error": cls._state.error,
            "total_certs": cls._state.total_certs,
            "total_jks": cls._state.total_jks,
            "total_crt": cls._state.total_crt,
            "vencidos": cls._state.vencidos,
            "clusters_escaneados": cls._state.clusters_escaneados,
        }

    @classmethod
    def get_last_payload(cls) -> list[dict]:
        """
        Retorna el último payload de certificados.
        Si la memoria está vacía (restart), intenta leer de disco.
        """
        if not cls._last_payload:
            cls._load_from_disk()
        return cls._last_payload

    @classmethod
    def _load_from_disk(cls) -> None:
        """Carga el último estado persistido desde disco."""
        try:
            if os.path.isfile(_PERSIST_PATH):
                with open(_PERSIST_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                cls._last_payload = data.get("certificados", [])
        except Exception as exc:
            print(f"[JobRunner] ⚠ No se pudo cargar estado de disco: {exc}")
