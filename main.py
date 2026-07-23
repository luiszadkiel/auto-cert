"""
main.py
€€€€€€€€
Entry point de la automatización BHD DevOps Portal.

Modos disponibles:
  py main.py               → extrae inventario de certificados (Brokers / K8s)
  py main.py certs         → ídem modo explícito
  py main.py k8s-sync      → exploración masiva de TODOS los certificados K8s
                              (CRT + JKS) en TODOS los clusters/namespaces.
  py main.py jks-discovery → alias de k8s-sync (nombre más explícito)
  py main.py aks-only      → igual que jks-discovery pero SIN escaneo legacy.
                              Ideal para correr en local rápidamente:
                              device-code en la terminal, omite prod, solo AKS.
  py main.py broker-sync   → sube certs vencidos al portal (tab Brokers / JKS)
  py main.py jks-update    → detecta JKS vencidos, descarga cert fresco del
                              host, reconstruye el keystore y lo sube al cluster.
                              JKS_UPDATE_APPLY=true para escribir (default: dry-run)
                              JKS_UPDATE_PRUNE=true para borrar duplicados vencidos

Uso:
  py main.py [modo]
"""

import asyncio
import sys

import os

sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]

# ── Extension dir limpio para Azure CLI ──────────────────────────────────────
# Forzar AZURE_EXTENSION_DIR al dir local ANTES de cualquier import que use az.
# Esto garantiza que kubectl -> kubelogin -> az tambien usen el dir limpio,
# evitando el error 'AzureCLICredential: exit status 1' por extension corrupta.
_project_root = os.path.dirname(os.path.abspath(__file__))
_az_ext_dir   = os.path.join(_project_root, ".az_ext")
os.makedirs(_az_ext_dir, exist_ok=True)
os.environ["AZURE_EXTENSION_DIR"] = _az_ext_dir

# Cargar variables de entorno desde .env (si existe)
# Las variables ya definidas en el sistema tienen prioridad.
try:
    from dotenv import load_dotenv
    load_dotenv(override=False)  # override=False: el sistema gana sobre .env
except ImportError:
    pass  # sin python-dotenv instalado: continuar igual

from controllers.cert_controller import CertController
from controllers.jks_discovery_controller import JksDiscoveryController
from controllers.broker_sync_controller import BrokerSyncController
from controllers.jks_update_controller import JksUpdateController

CONTROLLERS = {
    "certs":          CertController,
    "k8s-sync":       JksDiscoveryController,   # exploracion masiva, sin portal
    "jks-discovery":  JksDiscoveryController,   # alias explicito
    "aks-only":       JksDiscoveryController,   # solo AKS, sin legacy
    "broker-sync":    BrokerSyncController,
    # "inventory" se carga de forma lazy por dependencia de arquitectura legacy
    "jks-update":     JksUpdateController,      # actualiza JKS vencidos en el cluster
}

# kwargs extra que se pasan al método run() según el modo.
# Los modos que no aparecen aquí llaman run() sin argumentos adicionales.
RUN_KWARGS: dict[str, dict] = {
    "aks-only": {"include_legacy": False, "scan_prod": False},
}

# Variables de entorno extra que se activan segun el modo
RUN_ENV: dict[str, dict[str, str]] = {
    # aks-only: saltear el check lento de 'az role assignment list' (20-30s/cluster)
    "aks-only": {"SKIP_RBAC_PREFLIGHT": "true"},
}

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "certs"

    # Carga lazy de controladores con dependencias legacy
    if mode == "inventory":
        from controllers.inventory_controller import InventoryController
        CONTROLLERS["inventory"] = InventoryController

    ctrl_class = CONTROLLERS.get(mode)
    if ctrl_class is None:
        print(f"[ERROR] Modo desconocido: '{mode}'")
        print(f"  Modos validos: {list(CONTROLLERS.keys()) + ['inventory']}")
        sys.exit(1)

    print(f"[main] Modo: {mode}")
    kwargs = RUN_KWARGS.get(mode, {})
    # Aplicar variables de entorno especificas del modo
    for k, v in RUN_ENV.get(mode, {}).items():
        os.environ[k] = v
    try:
        asyncio.run(ctrl_class().run(**kwargs))
    except KeyboardInterrupt:
        print("\n[main] Proceso cancelado por el usuario.")
        sys.exit(0)
