"""
main.py
€€€€€€€€
Entry point de la automatización BHD DevOps Portal.

Modos disponibles:
  py main.py               → extrae inventario de certificados (Brokers / K8s)
  py main.py certs         → ídem modo explícito
  py main.py k8s-sync      → [ACTUALIZADO] exploración masiva de TODOS los
                              certificados K8s (CRT + JKS) en TODOS los
                              clusters/namespaces — YA NO depende del portal.
  py main.py jks-discovery → alias del modo anterior (nombre más explícito)
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

sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]

from controllers.cert_controller import CertController
from controllers.jks_discovery_controller import JksDiscoveryController
from controllers.broker_sync_controller import BrokerSyncController
from controllers.inventory_controller import InventoryController
from controllers.jks_update_controller import JksUpdateController

CONTROLLERS = {
    "certs":          CertController,
    "k8s-sync":       JksDiscoveryController,   # exploración masiva, sin portal
    "jks-discovery":  JksDiscoveryController,   # alias explícito
    "broker-sync":    BrokerSyncController,
    "inventory":      InventoryController,
    "jks-update":     JksUpdateController,      # actualiza JKS vencidos en el cluster
}

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "certs"

    ctrl_class = CONTROLLERS.get(mode)
    if ctrl_class is None:
        print(f"[ERROR] Modo desconocido: '{mode}'")
        print(f"  Modos válidos: {list(CONTROLLERS.keys())}")
        sys.exit(1)

    print(f"[main] Modo: {mode}")
    asyncio.run(ctrl_class().run())
