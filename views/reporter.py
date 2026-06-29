"""
views/reporter.py
──────────────────
View: toda la lógica de salida — JSON, CSV, screenshots, console.
No sabe nada de Playwright ni de cómo se obtuvieron los datos.
"""

import csv
import json
import os
from datetime import datetime


def _ts() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _ensure(path: str) -> None:
    os.makedirs(os.path.dirname(path) if os.path.dirname(path) else ".", exist_ok=True)


# ─── Console ──────────────────────────────────────────────────────────────────

def print_banner(msg: str) -> None:
    line = "=" * 60
    print(f"\n{line}\n  {msg}\n{line}\n")


def print_login_prompt(wait_seconds: int) -> None:
    print("\n" + "=" * 60)
    print("  [LOGIN REQUERIDO]")
    print("=" * 60)
    print("  → El browser se abrió con la pantalla de login de Microsoft.")
    print("  → Ingresa tu usuario y contraseña en la ventana del browser.")
    print("  → Si tienes MFA/Authenticator, apruébalo en tu celular.")
    print(f"  → Tienes {wait_seconds} segundos para completar el login.")
    print("=" * 60 + "\n")


def print_structure_summary(structure: dict) -> None:
    print(f"  Botones   : {[b['text'] for b in structure.get('buttons', [])]}")
    print(f"  Paginación: {'Sí' if structure.get('pagination') else 'No detectada'}")


def print_extraction_summary(records: list[dict]) -> None:
    if not records:
        return
    print(f"\n[3/3] Resumen final:")
    print(f"  ✓ {len(records)} certificados extraídos")
    print(f"  ✓ Columnas: {list(records[0].keys())}")


# ─── Persistencia ─────────────────────────────────────────────────────────────

def save_json(data: dict | list, path: str) -> str:
    _ensure(path)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"  ✓ JSON  → {path}")
    return path


def save_csv(data: list[dict], path: str) -> str:
    if not data:
        return ""
    _ensure(path)
    keys = list(data[0].keys())
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=keys)
        writer.writeheader()
        writer.writerows(data)
    print(f"  ✓ CSV   → {path}")
    return path


def save_excel(data: list[dict], path: str) -> str:
    if not data:
        return ""
    _ensure(path)
    try:
        import pandas as pd
        df = pd.DataFrame(data)
        df.to_excel(path, index=False)
        print(f"  ✓ EXCEL → {path}")
    except ImportError:
        print(f"  ⚠ EXCEL → No se pudo generar {path} (pandas/openpyxl no instalados)")
    return path


def build_output_paths(output_dir: str, prefix: str) -> dict[str, str]:
    """Genera rutas con timestamp para los archivos de salida."""
    stamp = _ts()
    return {
        "json": os.path.join(output_dir, f"{prefix}_{stamp}.json"),
        "csv":  os.path.join(output_dir, f"{prefix}_{stamp}.csv"),
        "excel": os.path.join(output_dir, f"{prefix}_{stamp}.xlsx"),
        "structure": os.path.join(output_dir, f"estructura_{stamp}.json"),
    }
