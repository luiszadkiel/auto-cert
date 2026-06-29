import asyncio
import ssl
import socket
from datetime import datetime, timezone
from cryptography import x509
from cryptography.hazmat.backends import default_backend

from config.legacy_targets import LEGACY_SERVERS

class LegacyDiscoveryService:
    def __init__(self):
        self.semaphore = asyncio.Semaphore(50)  # Max concurrent connections

    async def scan_all(self) -> list[dict]:
        print(f"[Legacy] Iniciando escaneo concurrente de {len(LEGACY_SERVERS)} servidores...")
        tasks = [self._scan_server(target) for target in LEGACY_SERVERS]
        results = await asyncio.gather(*tasks)
        
        # Filtramos None (aunque _scan_server retorna dicts siempre)
        valid_results = [r for r in results if r is not None]
        print(f"[Legacy] Escaneo completado. {len(valid_results)} procesados.")
        return valid_results

    async def _scan_server(self, target: str) -> dict:
        async with self.semaphore:
            parts = target.split('|')
            conn = parts[0]
            server_name = parts[1]
            alias = parts[2]
            desc = parts[3]
            
            ip_only = conn.split(':')[0] if ':' in conn else conn
            
            if conn == "PENDIENTE:443":
                print(f"[Legacy] Saltando {alias} (SIN IP)")
                return {
                    "SERVIDOR": alias,
                    "IP": "SIN IP",
                    "DESCRIPCION": desc,
                    "VENCIMIENTO": "PENDIENTE",
                    "DIAS RESTANTES": "N/A",
                    "COMMON NAME (CN)": "Completar IP"
                }

            host = ip_only
            port = int(conn.split(':')[1]) if ':' in conn else 443
            
            try:
                # Corremos la operacion sincrona de SSL en un hilo
                loop = asyncio.get_running_loop()
                cert = await loop.run_in_executor(None, self._fetch_cert_sync, host, port, server_name)
                
                # Parsear
                cn = self._get_cn(cert)
                not_after = cert.not_valid_after_utc if hasattr(cert, 'not_valid_after_utc') else cert.not_valid_after
                if not_after.tzinfo is None:
                    not_after = not_after.replace(tzinfo=timezone.utc)
                
                now = datetime.now(timezone.utc)
                days_left = (not_after - now).days
                short_date = not_after.strftime("%b %d %Y")
                
                print(f"[Legacy] [OK] {alias} -> Vence: {short_date} ({days_left} dias)")
                
                return {
                    "SERVIDOR": alias,
                    "IP": ip_only,
                    "DESCRIPCION": desc,
                    "VENCIMIENTO": short_date,
                    "DIAS RESTANTES": days_left,
                    "COMMON NAME (CN)": cn
                }
            except Exception as e:
                print(f"[Legacy] [ERROR] {alias} ({ip_only}) -> {str(e)[:50]}")
                return {
                    "SERVIDOR": alias,
                    "IP": ip_only,
                    "DESCRIPCION": desc,
                    "VENCIMIENTO": "ERROR CONEX",
                    "DIAS RESTANTES": "N/A",
                    "COMMON NAME (CN)": "No se pudo obtener el certificado"
                }

    def _fetch_cert_sync(self, host: str, port: int, server_name: str) -> x509.Certificate:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        
        with socket.create_connection((host, port), timeout=5) as sock:
            with context.wrap_socket(sock, server_hostname=server_name) as ssock:
                cert_der = ssock.getpeercert(binary_form=True)
                return x509.load_der_x509_certificate(cert_der, default_backend())

    def _get_cn(self, cert: x509.Certificate) -> str:
        for attribute in cert.subject:
            if attribute.oid == x509.NameOID.COMMON_NAME:
                return attribute.value
        return "Unknown"
