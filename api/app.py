"""
api/app.py
â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
FastAPI application â€” API REST simplificada.
Un solo endpoint sincrónico que ejecuta todo el flujo de descubrimiento
(Azure Graph, K8s namespaces, secrets, extracción JKS/CRT) y retorna
inmediatamente el JSON con la data consolidada.
"""

import sys
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore

import os
import threading
import builtins
import io
from fastapi import FastAPI, Header, HTTPException, Body, WebSocket, WebSocketDisconnect  # type: ignore
from fastapi.responses import HTMLResponse, RedirectResponse  # type: ignore
from fastapi.staticfiles import StaticFiles  # type: ignore
from typing import Optional

from config.settings import API_TRIGGER_TOKEN
from controllers.jks_discovery_controller import JksDiscoveryController
from api.log_manager import log_manager, send_log_sync, legacy_log_manager, send_legacy_log_sync
from services.azure_auth_service import AzureAuthService

# Sobrescribir print() globalmente para interceptar logs
_original_print = builtins.print

def broadcast_print(*args, **kwargs):
    _original_print(*args, **kwargs)
    sio = io.StringIO()
    _original_print(*args, **kwargs, file=sio)
    msg = sio.getvalue()
    # Si el log contiene "[Legacy]" mandarlo al canal legacy, sino al normal
    if "[Legacy]" in msg:
        send_legacy_log_sync(msg)
    else:
        send_log_sync(msg)

builtins.print = broadcast_print

app = FastAPI(
    title="Cert Automation API",
    description="API REST de exploracion masiva de certificados BHD (CRT + JKS) â€” integracion simplificada",
    version="2.0.0",
)

os.makedirs("output", exist_ok=True)
os.makedirs("api/static", exist_ok=True)

app.mount("/static", StaticFiles(directory="api/static"), name="static")
app.mount("/output", StaticFiles(directory="output"), name="output")

@app.get("/")
async def redirect_root():
    return RedirectResponse(url="/dashboard")

@app.get("/dashboard", response_class=HTMLResponse)
async def read_dashboard():
    if not os.path.exists("api/static/index.html"):
        return "<h1>Dashboard UI not found</h1>"
    with open("api/static/index.html", "r", encoding="utf-8") as f:
        return f.read()

@app.websocket("/ws/logs")
async def websocket_endpoint(websocket: WebSocket):
    await log_manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        log_manager.disconnect(websocket)

@app.websocket("/ws/logs/legacy")
async def websocket_legacy_endpoint(websocket: WebSocket):
    await legacy_log_manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        legacy_log_manager.disconnect(websocket)

@app.post("/api/v1/auth/login")
async def trigger_device_login():
    """Ejecuta el login interactivo y envía el código al WebSocket"""
    def run_login():
        auth = AzureAuthService()
        auth.interactive_device_login(send_log_sync)
        
    threading.Thread(target=run_login, daemon=True).start()
    return {"message": "Login iniciado. Revisa los logs para el código de dispositivo."}

@app.get("/api/v1/results")
async def list_results():
    """Lista los archivos generados en output/"""
    files = []
    if os.path.exists("output"):
        for f in os.listdir("output"):
            if f.endswith(".json") or f.endswith(".xlsx"):
                files.append({
                    "name": f,
                    "url": f"/output/{f}",
                    "size": os.path.getsize(os.path.join("output", f))
                })
    return {"files": sorted(files, key=lambda x: x["name"], reverse=True)}

# â”€â”€â”€ Healthcheck â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

@app.get("/api/v1/health")
async def health():
    """Healthcheck simple â€” confirma que el proceso está vivo."""
    return {"status": "ok"}


# â”€â”€â”€ Discovery (Sync) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

@app.post("/api/v1/k8s-sync/scan")
@app.get("/api/v1/k8s-sync/scan")
async def scan_certificates(
    x_api_key: Optional[str] = Header(None),
    filter_data: Optional[dict] = Body(None)
):
    """
    Dispara la exploración masiva de certificados de manera SINCRÃ“NICA.
    
    Puedes hacer un GET (escanea todo) o un POST pasándole un body:
    {"names": ["nombre-cluster"]} para filtrar.
    
    Retorna directamente el JSON con el payload de certificados descubiertos.
    RESILIENCIA: Siempre retorna un resultado, incluso parcial. Solo retorna
    500 si la autenticación falla completamente.
    """
    # Validar token si está configurado en el .env
    if API_TRIGGER_TOKEN and x_api_key != API_TRIGGER_TOKEN:
        raise HTTPException(status_code=403, detail="Token de API inválido o ausente")

    # Mapear el body a los argumentos del controller
    filter_mode = None
    run_filter = {}
    
    if filter_data and "names" in filter_data and isinstance(filter_data["names"], list):
        filter_mode = "names"
        run_filter = {"names": filter_data["names"], "mode": "names"}

    # Ejecutar controlador
    print("[API] Iniciando escaneo masivo síncrono...")
    controller = JksDiscoveryController()
    
    try:
        resultado = await controller.run(
            run_filter=run_filter if run_filter else None
        )
        
        # Solo retornar 500 si es un error de autenticación fatal
        if resultado.get("error") == "Azure session failed":
            raise HTTPException(
                status_code=401, 
                detail="Sesión de Azure no válida. Ejecute el login primero via /api/v1/auth/login"
            )
            
        # Para cualquier otro caso, retornar 200 con los datos que se pudieron obtener
        total = resultado.get("total_certs", 0)
        errors = resultado.get("errors", [])
        
        if errors:
            print(f"[API] Escaneo completado con {len(errors)} advertencia(s). Se retornan {total} certificados.")
        else:
            print(f"[API] Escaneo completado exitosamente. Se retornan {total} certificados.")
        
        return {
            "total": total,
            "certificados": resultado.get("payload", []),
            "cluster_summaries": resultado.get("cluster_summaries", []),
            "vencidos": resultado.get("vencidos", 0),
            "errors": errors,
        }
        
    except HTTPException:
        raise  # Re-lanzar HTTPExceptions intactas (401, 403)
    except Exception as e:
        # Catch-all: incluso si algo inesperado falla, el servidor no muere
        print(f"[API] Error inesperado durante el escaneo: {e}")
        import traceback
        traceback.print_exc()
        return {
            "total": 0,
            "certificados": [],
            "cluster_summaries": [],
            "vencidos": 0,
            "errors": [f"Error inesperado: {str(e)}"],
        }

