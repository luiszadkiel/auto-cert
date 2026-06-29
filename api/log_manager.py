import asyncio
from typing import List
from fastapi import WebSocket

class LogManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []
        self.logs_cache: List[str] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)
        for log in self.logs_cache[-100:]:
            await websocket.send_text(log)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: str):
        self.logs_cache.append(message)
        if len(self.logs_cache) > 500:
            self.logs_cache.pop(0)
            
        for connection in self.active_connections:
            try:
                await connection.send_text(message)
            except Exception:
                pass

log_manager = LogManager()
legacy_log_manager = LogManager()

def send_log_sync(message: str):
    """Permite enviar logs desde código síncrono."""
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(log_manager.broadcast(message + "\n"))
    except RuntimeError:
        pass

def send_legacy_log_sync(message: str):
    """Permite enviar logs de legacy desde código síncrono."""
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(legacy_log_manager.broadcast(message + "\n"))
    except RuntimeError:
        pass
