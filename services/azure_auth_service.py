"""
services/azure_auth_service.py
────────────────────────────────────────
Maneja la autenticación y configuración robusta de contextos de AKS en Azure.
Implementa las lecciones aprendidas:
- `az login` con tenant forzado
- `az account set` con validación estricta
- `az role assignment` pre-flight check para evitar fallos lentos de get-credentials
- `kubelogin convert-kubeconfig` tras obtener credenciales
- `kubectl auth can-i` para validación real de RBAC a nivel namespace
- Timeout estricto usando subprocess
"""

import os
import json
import subprocess
import threading
from typing import Optional

AZ_TIMEOUT = 30
TENANT_ID = "d0b50d16-a5e9-4cf2-a9d0-4733a3470110"

class AzureAuthService:
    def __init__(self):
        # Tomar Object ID desde variable de entorno o usar el del usuario por defecto
        self.assignee_id = os.getenv("AZURE_ASSIGNEE_ID", "5991c772-3256-4113-b62a-d37b40d25ead")

    def run_cmd(self, cmd: list[str], timeout: int = AZ_TIMEOUT, shell: bool = False) -> subprocess.CompletedProcess:
        """Ejecuta un comando con timeout fijo."""
        if os.name == "nt" and cmd[0] == "az":
            cmd[0] = "az.cmd"
        elif os.name == "nt" and cmd[0] == "kubelogin":
            cmd[0] = "kubelogin.exe"
            
        env = os.environ.copy()
        # Bypass broken permissions in ~/.azure/cliextensions by using a local dir
        env["AZURE_EXTENSION_DIR"] = os.path.join(os.getcwd(), ".az_ext")
            
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=shell,
            env=env
        )

    def ensure_az_session(self) -> bool:
        """
        Garantiza que haya una sesión de Azure activa en el tenant correcto.
        """
        print("[Azure Auth] Verificando sesión activa de Azure CLI...")
        try:
            # Check current account
            res = self.run_cmd(["az", "account", "show", "--query", "tenantId", "-o", "tsv"])
            if res.returncode == 0 and res.stdout.strip() == TENANT_ID:
                print("  [OK] Sesión activa confirmada.")
                return True
                
            print(f"  [!] Sesión ausente o en tenant equivocado. Iniciando login interactivo/device-code en tenant {TENANT_ID}...")
            # Forzar login. En un entorno server, ideal usar --identity o --service-principal
            res_login = self.run_cmd(["az", "login", "--tenant", TENANT_ID], timeout=120)
            if res_login.returncode == 0:
                print("  [OK] Login exitoso.")
                return True
            else:
                print(f"  [ERROR] Falló az login: {res_login.stderr}")
                return False
        except subprocess.TimeoutExpired:
            print("  [ERROR] Timeout al intentar verificar/iniciar sesión.")
            return False
        except FileNotFoundError:
            print("  [ERROR] 'az' cli no encontrado en el PATH.")
            return False

    def interactive_device_login(self, callback) -> bool:
        """
        Inicia el login de Azure CLI forzando el modo device-code.
        Captura el stdout en tiempo real para pasar el código al frontend.
        """
        import subprocess
        
        callback("[Azure Auth] Solicitando código de dispositivo (Device Code) a Azure...")
        
        tenant_id = os.getenv("AZURE_TENANT_ID", TENANT_ID)
        cmd = ["az", "login", "--use-device-code", "--tenant", tenant_id]
        
        if os.name == "nt":
            cmd[0] = "az.cmd"
            
        env = os.environ.copy()
        env["AZURE_EXTENSION_DIR"] = os.path.join(os.getcwd(), ".az_ext")
        
        # Ejecutar asíncronamente o en un hilo
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
            bufsize=1,
            universal_newlines=True
        )
        
        for line in iter(process.stdout.readline, ''):
            if line:
                callback(line.strip())
                
        process.stdout.close()
        return_code = process.wait()
        
        if return_code == 0:
            callback("[OK] Login interactivo completado exitosamente.")
            return True
        else:
            callback(f"[ERROR] El login interactivo falló (Code {return_code}).")
            return False

    def ensure_resource_graph_extension(self) -> bool:
        """
        Asegura que la extensión resource-graph esté instalada para poder descubrir los clusters.
        """
        print("[Azure Auth] Asegurando extensión 'resource-graph'...")
        cmd = ["az", "extension", "add", "--name", "resource-graph", "--only-show-errors"]
        res = self.run_cmd(cmd, timeout=60)
        if res.returncode != 0:
            print(f"  [ERROR] az extension add devolvió error: {res.stderr.strip()}")
            return False
        return True

    def discover_all_aks_clusters(self) -> list[dict]:
        """
        Usa Azure Resource Graph para descubrir dinámicamente todos los clusters AKS 
        a los que el usuario tiene acceso en el tenant.
        Retorna: [{"name": "...", "resourceGroup": "...", "subscriptionId": "...", "location": "..."}, ...]
        """
        print("[Azure Auth] Descubriendo clusters AKS (Resource Graph)...")
        query = "Resources | where type =~ 'microsoft.containerservice/managedclusters' | project name, resourceGroup, subscriptionId, location"
        cmd = [
            "az", "graph", "query",
            "-q", query,
            "--query", "data",
            "-o", "json"
        ]
        
        try:
            res = self.run_cmd(cmd, timeout=60)
            if res.returncode == 0:
                data = json.loads(res.stdout.strip() or "[]")
                print(f"  [OK] Descubiertos {len(data)} clusters en total.")
                return data
            else:
                print(f"  [ERROR] Falló query de Resource Graph: {res.stderr}")
                return []
        except Exception as e:
            print(f"  [ERROR] Excepción en Resource Graph: {e}")
            return []

    def set_subscription(self, subscription_id: str) -> bool:
        """
        Cambia a la suscripción solicitada y valida que el cambio surtió efecto.
        """
        try:
            res = self.run_cmd(["az", "account", "set", "--subscription", subscription_id])
            if res.returncode != 0:
                print(f"  [ERROR] az account set falló: {res.stderr}")
                return False
                
            # Validación estricta
            check = self.run_cmd(["az", "account", "show", "--query", "id", "-o", "tsv"])
            if check.returncode == 0 and check.stdout.strip() == subscription_id:
                return True
            else:
                print("  [ERROR] La suscripción actual no coincide tras el comando 'set'.")
                return False
        except Exception as e:
            print(f"  [ERROR] Excepción al cambiar suscripción: {e}")
            return False

    def preflight_rbac_check(self, subscription_id: str, resource_group: str, cluster_name: str) -> bool:
        """
        Verifica si el usuario actual tiene asignaciones de rol (incluyendo heredadas y de grupos)
        sobre el clúster. Esto evita ejecutar get-credentials y kubelogin inútilmente si no hay permisos.
        """
        # Obtenemos y cacheamos el ID del usuario firmado
        if not hasattr(self, "_user_id"):
            res = self.run_cmd(["az", "ad", "signed-in-user", "show", "--query", "id", "-o", "tsv"])
            if res.returncode == 0 and res.stdout.strip():
                self._user_id = res.stdout.strip()
            else:
                print("  [WARN] No se pudo obtener el ID del usuario firmado. Omitiendo preflight estricto.")
                return True  # Fallback
                
        scope = f"/subscriptions/{subscription_id}/resourceGroups/{resource_group}/providers/Microsoft.ContainerService/managedClusters/{cluster_name}"
        cmd = [
            "az", "role", "assignment", "list",
            "--assignee", self._user_id,
            "--scope", scope,
            "--include-inherited",
            "--include-groups",
            "-o", "json"
        ]
        
        try:
            res = self.run_cmd(cmd, timeout=30)
            if res.returncode == 0:
                import json
                assignments = json.loads(res.stdout.strip() or "[]")
                return len(assignments) > 0
            else:
                print(f"  [WARN] Error consultando role assignments: {res.stderr.strip()}")
                return True  # Fallback a intentar conectarse
        except Exception as e:
            print(f"  [WARN] Excepción en preflight_rbac_check: {e}")
            return True

    def configure_cluster_context(self, resource_group: str, cluster_name: str) -> bool:
        """
        Descarga credenciales del clúster sobrescribiendo existentes y convierte a kubelogin.
        """
        try:
            # 1. az aks get-credentials
            cmd_get = [
                "az", "aks", "get-credentials",
                "--resource-group", resource_group,
                "--name", cluster_name,
                "--overwrite-existing"
            ]
            res_get = self.run_cmd(cmd_get, timeout=60)
            if res_get.returncode != 0:
                print(f"  [ERROR] Falló get-credentials: {res_get.stderr.strip()}")
                return False
                
            # 2. kubelogin convert-kubeconfig (CLAVE para evitar devicecode loop)
            cmd_convert = [
                "kubelogin", "convert-kubeconfig",
                "-l", "azurecli"
            ]
            res_convert = self.run_cmd(cmd_convert, timeout=30)
            if res_convert.returncode != 0:
                print(f"  [ERROR] Falló kubelogin convert-kubeconfig: {res_convert.stderr.strip()}")
                return False
                
            return True
        except Exception as e:
            print(f"  [ERROR] Excepción configurando contexto: {e}")
            return False

    def auth_cani_check(self, context_name: str) -> bool:
        """
        Verificación real de conectividad: ¿podemos listar pods en kube-system?
        """
        cmd = [
            "kubectl", "auth", "can-i", "list", "pods", 
            "-n", "kube-system", 
            "--context", context_name
        ]
        try:
            res = self.run_cmd(cmd, timeout=15)
            # 'yes' if successful, 'no' or error otherwise
            if res.returncode == 0 and "yes" in res.stdout.lower():
                return True
            else:
                print(f"  [!] auth can-i denegado. Output: {res.stdout.strip()} {res.stderr.strip()}")
                return False
        except Exception as e:
            print(f"  [ERROR] Excepción en auth can-i: {e}")
            return False

    def check_azure_rbac_enabled(self, resource_group: str, cluster_name: str) -> bool:
        """
        Consulta si el clúster usa Azure RBAC for Kubernetes.
        """
        cmd = [
            "az", "aks", "show", 
            "--resource-group", resource_group, 
            "--name", cluster_name, 
            "--query", "aadProfile.enableAzureRbac", 
            "-o", "json"
        ]
        try:
            res = self.run_cmd(cmd, timeout=30)
            if res.returncode == 0:
                output = res.stdout.strip().lower()
                return output == "true"
        except Exception as e:
            print(f"  [ERROR] Excepción verificando Azure RBAC: {e}")
        return False
