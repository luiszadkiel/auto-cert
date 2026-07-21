# 🚀 Guía de Despliegue de Auto-Cert en Zabbix

Este documento detalla los requerimientos técnicos, configuraciones de red y procesos de autenticación necesarios para ejecutar la herramienta **Auto-Cert** dentro del servidor Zabbix (`serzbxdev01`).

---

## 🌐 1. Requerimientos de Red y Puertos

El sistema Auto-Cert requiere la configuración de dos tipos de puertos: uno de **entrada** (para recibir peticiones locales) y uno de **salida** (para consultar a la nube de Azure).

### 📥 Puertos de ENTRADA (Inbound)
* **Puerto `8088` (TCP):** Es el puerto donde la API (FastAPI) de Auto-Cert escucha peticiones. 
* Zabbix (o cualquier cliente HTTP/Postman) utiliza este puerto para iniciar escaneos o ver los logs en tiempo real.
* **Configuración en Docker:** Al levantar el contenedor, este puerto debe mapearse hacia el servidor host utilizando el flag `-p`:
  ```bash
  sudo docker run -d --name mi-autocert -p 8088:8088 autocert-app:latest
  ```

### 📤 Puertos de SALIDA (Outbound / Firewall)
Para que el contenedor pueda comunicarse con Azure y extraer la información de los certificados, el servidor anfitrión debe tener habilitada la **resolución DNS** y salida TCP por el **puerto `443` (HTTPS)** hacia los siguientes dominios:

#### 🔐 Endpoints de Autenticación y Administración
| Dominio | Puerto | Protocolo | Propósito |
|---------|--------|-----------|-----------|
| `login.microsoftonline.com` | **443** | TCP / HTTPS | Autenticación de Azure AD |
| `graph.microsoft.com` | **443** | TCP / HTTPS | Consultas al directorio (Microsoft Graph API) |
| `management.azure.com` | **443** | TCP / HTTPS | Descubrimiento de suscripciones y recursos de Azure |

#### ☸️ Endpoints de Kubernetes (AKS)
Para consultar los certificados alojados en Kubernetes, es estrictamente necesario habilitar la salida hacia los API Servers de AKS:
| Dominio | Puerto | Protocolo | Propósito |
|---------|--------|-----------|-----------|
| `*.hcp.eastus2.azmk8s.io` | **443** | TCP / HTTPS | API Servers de clusters AKS en la región East US 2 (Principal de BHD) |
| `*.azmk8s.io` | **443** | TCP / HTTPS | (Opcional/Recomendado) Wildcard general para cubrir clusters en cualquier región |

> [!WARNING]
> **Bloqueo a nivel de DNS**  
> Durante el despliegue inicial se detectó que el firewall no solo bloqueaba el puerto TCP, sino que también aplicaba un bloqueo a nivel DNS (los dominios daban `Unknown host`). Es vital que Infraestructura/Seguridad habilite **ambas capas** (Resolución DNS + TCP 443).

---

## 🔑 2. Métodos de Autenticación en Azure

### 🤖 Opción A: Service Principal (Recomendado / Modo Máquina)
Esta es la forma correcta de ejecutar procesos desatendidos en servidores. Evita los bloqueos de **Conditional Access Policies** (como el MFA obligatorio por ubicación o dispositivo).

**Requerimientos a solicitar a DevOps/Seguridad:**
* Creación de un **App Registration** (Service Principal).
* **Tenant ID:** `d0b50d16-a5e9-4cf2-a9d0-4733a3470110`
* **Permiso necesario:** Rol de `Azure Kubernetes Service Cluster User Role`.
* **Alcance (Scope):** Las suscripciones donde residen los clusters de AKS de NoProd.

Al obtener las credenciales, el contenedor se autentica internamente usando el *Client ID* y el *Client Secret* provistos.

### 🧑‍💻 Opción B: Device Code Flow (Modo Interactivo / Pruebas)
Este método se usa si no se cuenta con un Service Principal, pero requiere intervención humana cada vez que el token expira o el contenedor se reinicia.

**Comando de autenticación manual:**
```bash
sudo docker exec -it mi-autocert az login --use-device-code --tenant d0b50d16-a5e9-4cf2-a9d0-4733a3470110
```
**Pasos:**
1. El comando arrojará un código alfanumérico (ej: `ATY2Q3TRJ`).
2. Abrir en un navegador web: `https://login.microsoft.com/device`
3. Ingresar el código y autenticarse con las credenciales corporativas (ej: `luis_duran@bhd.com.do`).

> [!NOTE]
> Este método puede fallar si la política corporativa bloquea inicios de sesión interactivos desde IPs de servidores.

---

## 🐳 3. Comandos Útiles de Docker

Asumiendo que el archivo de imagen transferido se llama `autocert.tar` y el contenedor en ejecución se llama `mi-autocert`.

* **📥 Cargar la imagen al servidor Zabbix:**
  ```bash
  sudo docker load -i autocert.tar
  ```
* **🔍 Verificar que el contenedor está corriendo:**
  ```bash
  sudo docker ps | grep mi-autocert
  ```
* **💻 Ingresar a la consola del contenedor (Modo Interactivo):**
  ```bash
  sudo docker exec -it mi-autocert /bin/bash
  ```
* **📜 Ver los logs de la aplicación en tiempo real:**
  ```bash
  sudo docker logs -f mi-autocert
  ```
* **⚙️ Ejecutar un script manualmente dentro del contenedor:**
  ```bash
  sudo docker exec -it mi-autocert python summary.py
  ```

---

## 📁 4. Estructura y Archivos Extraídos

El entorno Docker de producción contiene algunas actualizaciones menores (de la rama del 9 de Julio) que se encuentran alojadas en:
* 📄 `/app/controllers/jks_discovery_controller.py`
* 📄 `/app/services/azure_auth_service.py`
* 📄 `/app/services/k8s_service.py`

> [!TIP]
> **Respaldo de Archivos**  
> Si en algún momento necesitas extraer un archivo directamente del contenedor de producción hacia el servidor local (ej. la carpeta `/tmp/`), puedes usar:
> ```bash
> sudo docker cp mi-autocert:/app/ruta/al/archivo.py /tmp/
> ```
