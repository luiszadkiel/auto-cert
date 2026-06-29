"""
services/excel_loader_service.py
──────────────────────────────────
Carga el Excel/CSV de inventario de certificados y retorna una lista
de InventoryCert normalizados.

Soporta:
  - CSV (csv.DictReader)
  - XLSX (openpyxl)

El archivo de entrada mezcla formatos de fecha:
  - "1/3/2023 16:37"
  - "01/15/2038 08:00:00"
  - "03/16/2024 20:00:00"

El parser prueba múltiples formatos automáticamente.
"""

import csv
import os
from typing import Optional

from models.inventory_cert import InventoryCert


class ExcelLoaderService:
    """Carga el inventario de certificados desde CSV o XLSX."""

    def load(self, path: str) -> list[InventoryCert]:
        """
        Lee el archivo de inventario y retorna lista de InventoryCert.
        Detecta el formato por extensión.

        Args:
            path: Ruta al archivo CSV o XLSX.

        Returns:
            Lista de InventoryCert con source="excel".
        """
        abs_path = os.path.abspath(path)

        if not os.path.exists(abs_path):
            print(f"  [ExcelLoader] ERROR Archivo no encontrado: {abs_path}")
            return []

        ext = os.path.splitext(abs_path)[1].lower()

        if ext == ".csv":
            certs = self._load_csv(abs_path)
        elif ext in (".xlsx", ".xls"):
            certs = self._load_xlsx(abs_path)
        else:
            print(f"  [ExcelLoader] ERROR Formato no soportado: {ext}")
            return []

        print(f"  [ExcelLoader] OK {len(certs)} certificados cargados desde {os.path.basename(abs_path)}")
        return certs

    def _load_csv(self, path: str) -> list[InventoryCert]:
        """Lee un CSV con cabeceras y retorna lista de InventoryCert."""
        certs: list[InventoryCert] = []

        # Detectar encoding
        encoding = self._detect_encoding(path)

        with open(path, "r", encoding=encoding, newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    cert = InventoryCert.from_excel_row(row)
                    if cert.namespace or cert.secret_name:  # descartar filas vacías
                        certs.append(cert)
                except Exception as exc:
                    print(f"  [ExcelLoader] WARN Fila ignorada: {exc}")

        return certs

    def _load_xlsx(self, path: str) -> list[InventoryCert]:
        """Lee un XLSX y retorna lista de InventoryCert."""
        try:
            import openpyxl  # type: ignore[import-untyped]
        except ImportError:
            print("  [ExcelLoader] ERROR openpyxl no instalado. Usa: pip install openpyxl")
            return []

        certs: list[InventoryCert] = []
        wb = openpyxl.load_workbook(path, read_only=True)
        ws = wb.active
        if ws is None:
            print("  [ExcelLoader] ERROR No active sheet found")
            wb.close()
            return []

        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            wb.close()
            return []

        # Primera fila = cabeceras
        headers = [str(h).strip() if h else "" for h in rows[0]]

        for row_values in rows[1:]:
            row_dict = {}
            for col_idx, header in enumerate(headers):
                if col_idx < len(row_values):
                    val = row_values[col_idx]
                    row_dict[header] = str(val).strip() if val is not None else ""
                else:
                    row_dict[header] = ""

            try:
                cert = InventoryCert.from_excel_row(row_dict)
                if cert.namespace or cert.secret_name:
                    certs.append(cert)
            except Exception as exc:
                print(f"  [ExcelLoader] WARN Fila ignorada: {exc}")

        wb.close()
        return certs

    def _detect_encoding(self, path: str) -> str:
        """Detecta encoding del archivo. Prueba UTF-8 primero, fallback a latin-1."""
        with open(path, "rb") as f:
            head = f.read(4)
        if head[:3] == b"\xef\xbb\xbf":
            return "utf-8-sig"

        # Intentar leer como UTF-8 — si falla, usar latin-1
        try:
            with open(path, "r", encoding="utf-8") as f:
                f.read(1024)
            return "utf-8"
        except UnicodeDecodeError:
            return "latin-1"
