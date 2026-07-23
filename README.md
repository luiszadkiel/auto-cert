# auto-cert — Guia de uso

Herramienta de automatizacion BHD DevOps para inventario y actualizacion de certificados en clusters AKS.

---

## Requisitos previos (solo la primera vez)

```bash
# 1. Instalar dependencias de Python
pip install -r requirements.txt

# 2. Verificar herramientas en el PATH
az --version
kubectl version --client
kubelogin --version
keytool -help
```

> Si alguna herramienta no esta instalada:
> - **az** : https://learn.microsoft.com/es-es/cli/azure/install-azure-cli
> - **kubectl** : https://kubernetes.io/docs/tasks/tools/
> - **kubelogin** : https://azure.github.io/kubelogin/
> - **keytool** : viene con Java JDK

---

## Como ejecutar

Abre una terminal en la carpeta del proyecto:

```
c:\Users\zadkiel\Downloads\auto-cert
```

---

### Modo 1 — Solo inventario AKS *(el mas util)*

```bash
py main.py aks-only
```

- Recorre **todos los clusters que NO son produccion** (DEV, QA, UAT, NOPROD, etc.)
  Los clusters de produccion se omiten automaticamente.
- Extrae todos los certificados encontrados (CRT + JKS)
- Si no estas logueado en Azure: aparece el **device-code** en la terminal
  - Ve a `aka.ms/devicelogin`, ingresa el codigo, y el proceso continua solo
- **Salida:** `output/jks_discovery_<timestamp>.json` + `.xlsx`

---

### Modo 2 — Inventario completo (AKS + servidores legacy)

```bash
py main.py jks-discovery
```

- **Igual que el Modo 1** (todos los clusters no-produccion), pero ademas
  escanea los **servidores legacy** (servidores fisicos/VM fuera de AKS)
- Usa este modo cuando quieras el inventario completo de toda la empresa
- **Salida:** `output/jks_discovery_<timestamp>.json` + `.xlsx`

---

### Modo 3 — Ver que JKS actualizaria *(dry-run, sin escribir nada)*

```bash
py main.py jks-update
```

- Detecta keystores JKS vencidos en todos los clusters no-produccion
- Muestra que haria, pero **no toca el cluster**
- **Salida:** `output/jks_update_<timestamp>.json` + `.xlsx`

---

### Modo 4 — Actualizar JKS vencidos de verdad

```bash
$env:JKS_UPDATE_APPLY="true"
py main.py jks-update
```

> **Atencion:** este modo escribe en el cluster. Usarlo solo cuando estes seguro del resultado del dry-run.

Para borrar tambien los aliases duplicados vencidos dentro del keystore:

```bash
$env:JKS_UPDATE_APPLY="true"
$env:JKS_UPDATE_PRUNE="true"
py main.py jks-update
```

---

## Login de Azure

El login ocurre automaticamente al inicio de cada modo.

| Situacion | Lo que pasa |
|---|---|
| Ya tienes sesion activa (`az login` previo) | Continua sin interrupciones |
| Usuario/password en `.env` | Intenta login directo (solo si no tienes MFA) |
| Sin sesion activa | Muestra device-code en la terminal |
| Service Principal configurado (Zabbix) | Login 100% automatico, sin intervencion humana |

Para configurar el Service Principal cuando Seguridad entregue las credenciales, edita el archivo `.env`:

```ini
AZURE_CLIENT_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
AZURE_CLIENT_SECRET=tu-client-secret-aqui
```

---

## Archivos de salida

Todos los reportes se guardan en la carpeta `output/`:

| Archivo | Contenido |
|---|---|
| `jks_discovery_<timestamp>.json` | Inventario completo en JSON |
| `jks_discovery_<timestamp>.xlsx` | Inventario en Excel |
| `jks_update_<timestamp>.json` | Resultado del update en JSON |
| `jks_update_<timestamp>.xlsx` | Resultado del update en Excel |
