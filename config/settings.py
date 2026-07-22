"""
config/settings.py
â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
Configuración centralizada de la automatización.
Cualquier valor que cambie según entorno o que quieras
parametrizar sin tocar la lógica va aquí.

Las credenciales se leen desde el archivo .env (nunca hardcodeadas aquí).
"""

import os
from pathlib import Path

# â”€â”€â”€ Cargar .env automáticamente â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
try:
    from dotenv import load_dotenv  # type: ignore[import-untyped]
    _env_path = Path(__file__).parent.parent / ".env"
    load_dotenv(dotenv_path=_env_path)
except ImportError:
    pass  # python-dotenv no instalado: las vars deben estar en el entorno del SO

# â”€â”€â”€ Portal â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
TARGET_URL  = "https://devops.cfbhd.com/certificados/inventariodecertificados"
HEADLESS: bool = os.getenv("HEADLESS", "False").lower() in ("true", "1", "yes")
LOGIN_WAIT  = 300          # segundos que espera el MFA (5 min)

# â”€â”€â”€ Credenciales Microsoft / Entra ID â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Se leen desde .env â€” ver .env.example para el template
BHD_USERNAME: str = os.getenv("BHD_USERNAME", "")
BHD_PASSWORD: str = os.getenv("BHD_PASSWORD", "")

# â”€â”€â”€ Playwright â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
BROWSER_ARGS = [
    "--ignore-certificate-errors",
    "--ignore-ssl-errors",
    "--start-maximized",
]
VIEWPORT = {"width": 1400, "height": 900}

# â”€â”€â”€ Salida â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "..", "output")

# â”€â”€â”€ Kubernetes â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# (Los clústeres ahora se descubren dinámicamente usando Azure Resource Graph)

# Timeout para cada llamada kubectl (segundos)
KUBECTL_TIMEOUT: int = int(os.getenv("KUBECTL_TIMEOUT", "30"))

# Tipos de llave que contienen un certificado X.509 dentro del secret
CERT_SECRET_KEYS: list[str] = ["tls.crt", "ca.crt", "cert.pem", "certificate.crt"]

# â”€â”€â”€ JKS (keystores) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Patrón del secret de password:
#   keystore-facephi â†’ keystore-password-facephi
#   (se reemplaza "keystore-" por "keystore-password-")
JKS_PASSWORD_SECRET_PREFIX: str = "keystore-password-"

# Key dentro del secret de password (siempre la misma â€” formato IBM ACE setdbparms)
JKS_PASSWORD_DATA_KEY: str = "setdbparms.txt"

# Password de fallback si no se encuentra el secret de password
JKS_FALLBACK_PASSWORD: str = os.getenv("JKS_FALLBACK_PASSWORD", "12345678")

# Timeout para keytool (segundos)
KEYTOOL_TIMEOUT: int = int(os.getenv("KEYTOOL_TIMEOUT", "15"))

# ─── JKS Update (modo jks-update) ────────────────────────────────────────────
# JKS_UPDATE_APPLY=true  → escribe en el cluster (default: dry-run)
JKS_UPDATE_APPLY: bool = os.getenv("JKS_UPDATE_APPLY", "false").lower() in ("true", "1", "yes")

# JKS_UPDATE_PRUNE=true  → borra aliases duplicados vencidos al aplicar
JKS_UPDATE_PRUNE: bool = os.getenv("JKS_UPDATE_PRUNE", "false").lower() in ("true", "1", "yes")

# Puerto TLS para re-bajar el certificado del host
JKS_REFETCH_PORT: int = int(os.getenv("JKS_REFETCH_PORT", "443"))

# Timeout de conexión TLS (segundos)
JKS_REFETCH_TIMEOUT: int = int(os.getenv("JKS_REFETCH_TIMEOUT", "10"))

# Directorio donde se guardan backups de .jks antes de modificarlos
JKS_BACKUP_DIR: str = os.getenv(
    "JKS_BACKUP_DIR",
    os.path.join(os.path.dirname(__file__), "..", "output", "jks_backups"),
)

# â”€â”€â”€ Inventario (modo inventory) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
INVENTORY_INPUT_PATH: str = os.path.join(
    os.path.dirname(__file__), "..", "micro_servicos_excel",
    "Inventario Certificados No Prod(Sheet1).csv",
)


# â”€â”€â”€ Brokers (mqsi) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Las credenciales por servidor van en servers.xlsx (no en .env).
# Solo se mantiene aquí el correo genérico de interesados y la carpeta de certs.

# Correo de interesados que se incluye en el formulario de carga (genérico del equipo)
CERT_EMAIL: str = os.getenv("CERT_EMAIL", "")

# Carpeta local donde deben estar los archivos .crt / .cert a subir
CERTS_DIR: str = os.path.join(
    os.path.dirname(__file__), "..",
    "certs",
)

# â”€â”€â”€ Modo de Ejecución y Filtros â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# RUN_MODE â†’ Qué flujo ejecutar automáticamente ("broker-sync", "k8s-sync", etc)
RUN_MODE: str = os.getenv("RUN_MODE", "broker-sync")

# Filtro por nombre (JKS name o K8s namespace). Separados por coma.
FILTER_NAME: str = os.getenv("FILTER_NAME", "")

# Límite numérico (0 = todos)
FILTER_LIMIT: int = int(os.getenv("FILTER_LIMIT", "0"))


def get_run_filter() -> dict:
    """
    Construye el dict de filtro a partir de las variables de entorno.
    Sirve tanto para broker-sync (nombre JKS) como para k8s-sync (namespace).

    Prioridad:
      1. Si FILTER_NAME tiene valor â†’ mode="names"
      2. Si FILTER_LIMIT > 0        â†’ mode="limit"
      3. Si ambos vacíos/0          â†’ mode="all"
    """
    # Prioridad 1: nombres específicos
    if FILTER_NAME.strip():
        names = [n.strip() for n in FILTER_NAME.split(",") if n.strip()]
        if names:
            return {"mode": "names", "names": names}

    # Prioridad 2: límite numérico
    if FILTER_LIMIT > 0:
        return {"mode": "limit", "limit": FILTER_LIMIT}

    # Default: todos
    return {"mode": "all"}


# â”€â”€â”€ Playwright Session Persistence â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
STORAGE_STATE_PATH: str = os.getenv(
    "STORAGE_STATE_PATH",
    os.path.join(os.path.dirname(__file__), "..", "auth", "storage_state.json"),
)

# â”€â”€â”€ Umbrales de certificados (para Zabbix/alertas) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
CERT_DIAS_WARNING: int = int(os.getenv("CERT_DIAS_WARNING", "30"))
CERT_DIAS_CRITICAL: int = int(os.getenv("CERT_DIAS_CRITICAL", "7"))

# â”€â”€â”€ API FastAPI â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
API_HOST: str = os.getenv("API_HOST", "0.0.0.0")
API_PORT: int = int(os.getenv("API_PORT", "8088"))
API_TRIGGER_TOKEN: str = os.getenv("API_TRIGGER_TOKEN", "")
