"""
models/certificate.py
──────────────────────
Modelo de datos: representa un certificado extraído del portal.
Sin dependencia de Playwright ni de lógica de negocio.
"""

from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any


@dataclass
class Certificate:
    """Un certificado tal y como viene de la tabla del portal."""
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return self.raw

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "Certificate":
        return cls(raw=row)


@dataclass
class PageStructure:
    """
    Metadatos del DOM analizados antes de la extracción.
    Permite que la vista y el controlador sepan qué encontró el scraper.
    """
    url: str = ""
    title: str = ""
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    tables: list[dict] = field(default_factory=list)
    buttons: list[dict] = field(default_factory=list)
    inputs: list[dict] = field(default_factory=list)
    selects: list[dict] = field(default_factory=list)
    pagination: bool = False

    def to_dict(self) -> dict:
        return asdict(self)
