"""
services/k8s_service.py
────────────────────────
Servicio de Kubernetes: interactúa con el cluster via kubectl.

Responsabilidades:
  - Listar secrets de un namespace
  - Extraer y parsear el certificado X.509 de un secret
  - Retornar K8sCert con CN, not_before, not_after y el PEM raw
  - [NUEVO] Soporte para keystores JKS:
      • Descubrimiento dinámico de contraseña (setdbparms.txt)
      • Parseo con keytool → múltiples certs por keystore

Prerequisito: kubectl instalado y configurado con los contextos de los clusters.
"""

import base64
import json
import os
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from typing import Optional

from config.settings import (
    KUBECTL_TIMEOUT,
    CERT_SECRET_KEYS,
    JKS_PASSWORD_SECRET_PREFIX,
    JKS_PASSWORD_DATA_KEY,
    JKS_FALLBACK_PASSWORD,
    KEYTOOL_TIMEOUT,
)
from models.k8s_cert import K8sCert


# ─── Helpers de ambiente ──────────────────────────────────────────────────────

_AMBIENTE_ALIASES: dict[str, str] = {
    # Producción
    "prod": "PRD", "produccion": "PRD", "production": "PRD", "prd": "PRD",
    # Pre-producción
    "pre": "PPD", "preprod": "PPD", "ppd": "PPD", "staging": "PPD", "stg": "PPD",
    # SQA
    "sqa": "SQA", "qa": "SQA", "test": "SQA", "testing": "SQA",
    # Desarrollo
    "dev": "DEV", "develop": "DEV", "development": "DEV",
}


def _normalizar_ambiente(raw: str) -> Optional[str]:
    """Normaliza un texto libre de ambiente a DEV/SQA/PPD/PRD o None."""
    if not raw:
        return None
    return _AMBIENTE_ALIASES.get(raw.strip().lower())


def _inferir_ambiente_de_namespace(namespace: str) -> Optional[str]:
    """
    Intenta inferir el ambiente desde el nombre de un namespace.
    Toma el sufijo después del último '-' (ej: t24-sqa → sqa) y lo normaliza.
    None si el namespace no tiene guion o el sufijo no matchea nada conocido.
    """
    if not namespace or "-" not in namespace:
        return None
    suffix = namespace.rsplit("-", 1)[-1]
    return _normalizar_ambiente(suffix)


def _ambiente_coincide(ambiente_esperado: Optional[str], namespace_encontrado: str) -> bool:
    """
    True si no hay evidencia de conflicto de ambiente.

    Solo devuelve False cuando AMBOS lados se pudieron clasificar con confianza
    y son distintos. Si cualquiera de los dos es None, asume compatible
    (prefiero un falso positivo ocasional a bloquear búsquedas válidas
    por una convención de nombre que no conozco).
    """
    norm_esperado = _normalizar_ambiente(ambiente_esperado) if ambiente_esperado else None
    norm_encontrado = _inferir_ambiente_de_namespace(namespace_encontrado)

    if norm_esperado is None or norm_encontrado is None:
        return True  # sin evidencia suficiente → compatible

    return norm_esperado == norm_encontrado


# ─── Construcción de K8sCert desde secret_data (compartido) ──────────────────

def _build_certs_from_secret_data(
    secret_data: dict,
    cluster_name: str,
    namespace: str,
    secret_name: str,
    context: str,
    pods: str = "",
    nodes: str = "",
) -> list["K8sCert"]:
    """
    Construye la lista de K8sCert (1 para CRT, N para JKS — uno por alias)
    a partir de un secret_data YA OBTENIDO.

    No hace búsqueda profunda: asume que (cluster_name, namespace, secret_name)
    es la ubicación real del secret (se usa en el escaneo masivo, donde el
    secret se acaba de listar). get_secret_all_certs sigue haciendo búsqueda
    profunda antes de llamar a esta función.
    """
    # ── Intentar CRT primero ─────────────────────────────────────────
    pem, pem_key = _extract_cert_pem_with_key(secret_data)
    if pem:
        cert_obj = _parse_x509(pem)
        if cert_obj:
            cn, san, org, ou = _extract_cn_and_san(cert_obj)
            cn = cn or (san[0] if san else secret_name)

            try:
                not_after = cert_obj.not_valid_after_utc
                not_before = cert_obj.not_valid_before_utc
            except AttributeError:
                not_after = cert_obj.not_valid_after
                not_before = cert_obj.not_valid_before

            return [K8sCert(
                cluster=cluster_name,
                namespace=namespace,
                secret_name=secret_name,
                common_name=cn,
                organization=org,
                organizational_unit=ou,
                not_before=not_before,
                not_after=not_after,
                secret_type="CRT",
                san=san,
                cert_pem=pem,
                data_key=pem_key,
                pods=pods,
                nodes=nodes,
            )]

    # ── Intentar JKS ─────────────────────────────────────────────────
    jks_data, jks_key = _extract_jks_data(secret_data)
    if jks_data:
        print(f"  [K8s] JKS encontrado: {namespace}/{secret_name} key='{jks_key}' ({len(jks_data)} bytes)")

        password = _get_jks_password(
            namespace=namespace,
            secret_name=secret_name,
            jks_key=jks_key,
            context=context,
            original_secret_data=secret_data,
        )

        entries = _parse_jks_certs(jks_data, password)

        if entries:
            print(f"  [K8s] ✓ {len(entries)} certificado(s) encontrado(s) en el keystore")
            certs: list[K8sCert] = []
            for entry in entries:
                cert_name = str(entry.get("full_subject", entry.get("cn", "")) or "")
                certs.append(K8sCert(
                    cluster=cluster_name,
                    namespace=namespace,
                    secret_name=secret_name,
                    common_name=cert_name,
                    organization=entry.get("organization", ""),
                    organizational_unit=entry.get("organizational_unit", ""),
                    not_before=entry.get("not_before"),
                    not_after=entry.get("not_after"),
                    secret_type="JKS",
                    cert_pem=jks_data,
                    data_key=jks_key,
                    alias=entry.get("alias", ""),
                    password=password,
                    pods=pods,
                    nodes=nodes,
                ))
            return certs
        else:
            print(f"  [K8s] ⚠ keytool no pudo extraer certificados del JKS de {namespace}/{secret_name}")
            # Retornar un K8sCert parcial para que el dashboard/reporte sepa que SÍ se encontró
            return [K8sCert(
                cluster=cluster_name,
                namespace=namespace,
                secret_name=secret_name,
                common_name=f"[JKS: {jks_key} — password incorrecta]",
                not_before=None,
                not_after=None,
                secret_type="JKS",
                cert_pem=jks_data,
                data_key=jks_key,
                alias="⚠ password incorrecta",
                password=password,
                pods=pods,
                nodes=nodes,
            )]

    # ── Sin cert reconocible ─────────────────────────────────────────
    return []


# ─── Descubrimiento compartido ────────────────────────────────────────────────

_DISCOVERED_CLUSTERS: dict = {}  # llenado por register_discovery()


def register_discovery(cluster_statuses: dict) -> None:
    """
    Registra los ClusterStatus descubiertos por el controller en el pre-flight.
    Permite que _deep_search_secret use clusters reales del portal en vez del
    mapa estático K8S_CLUSTERS de settings.py.
    """
    global _DISCOVERED_CLUSTERS
    _DISCOVERED_CLUSTERS = cluster_statuses


def _candidatos_otros_clusters(original_context: str) -> dict[str, str]:
    """
    Retorna clusters candidatos para búsqueda multi-cluster (excluyendo el original).

    Si _DISCOVERED_CLUSTERS tiene datos (controller hizo pre-flight), usa solo
    los que están reachable. Si está vacío (uso directo sin controller), cae
    al K8S_CLUSTERS estático de settings.py.
    """
    if _DISCOVERED_CLUSTERS:
        return {
            name: status.kubectl_context
            for name, status in _DISCOVERED_CLUSTERS.items()
            if status.reachable and status.kubectl_context != original_context
        }
    return {}


# ─── Extracción de CN y SAN ──────────────────────────────────────────────────

def _extract_cn_and_san(cert_obj) -> tuple[str, list[str], str, str]:
    """
    Extrae el Common Name, Subject Alternative Names, Organization y Organizational Unit de un cert X.509.
    Retorna (cn, lista_de_sans, org, ou).
    """
    cn = ""
    san: list[str] = []
    org = ""
    ou = ""
    try:
        from cryptography.x509.oid import NameOID  # type: ignore[import-untyped]
        from cryptography import x509 as cx509  # type: ignore[import-untyped]

        attrs_cn = cert_obj.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
        cn = attrs_cn[0].value if attrs_cn else ""

        attrs_o = cert_obj.subject.get_attributes_for_oid(NameOID.ORGANIZATION_NAME)
        org = attrs_o[0].value if attrs_o else ""

        attrs_ou = cert_obj.subject.get_attributes_for_oid(NameOID.ORGANIZATIONAL_UNIT_NAME)
        ou = attrs_ou[0].value if attrs_ou else ""

        try:
            san_ext = cert_obj.extensions.get_extension_for_class(
                cx509.SubjectAlternativeName
            )
            san = san_ext.value.get_values_for_type(cx509.DNSName)
        except cx509.ExtensionNotFound:
            pass
    except Exception:
        pass
    return cn, san, org, ou


# ─── kubectl ──────────────────────────────────────────────────────────────────

def _run_kubectl(*args: str, context: str) -> dict:
    """
    Ejecuta un comando kubectl con el contexto indicado.
    Retorna el JSON parseado del stdout.
    Lanza RuntimeError si kubectl falla.
    """
    cmd = ["kubectl", "--context", context, *args, "-o", "json"]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=KUBECTL_TIMEOUT,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"[K8s] kubectl falló (rc={result.returncode}): {result.stderr.strip()}"
        )
    return json.loads(result.stdout)


# ─── Extracción PEM (reescrita: dos pasadas con validación de contenido) ──────

_PEM_MARKER = b"-----BEGIN CERTIFICATE-----"


def _extract_cert_pem_with_key(secret_data: dict) -> tuple[Optional[bytes], str]:
    """
    Extrae el certificado PEM de un secret de Kubernetes.

    Dos pasadas:
      1. Busca por nombre de key conocido (CERT_SECRET_KEYS) y valida que
         el contenido decodificado contenga -----BEGIN CERTIFICATE-----
      2. Si no encuentra nada, recorre TODAS las demás keys (saltando .jks)
         buscando por contenido PEM, sin importar el nombre de la key.

    Retorna (bytes_pem, key_name) o (None, "").
    """
    data_fields = secret_data.get("data", {})

    # ── Pasada 1: keys conocidas + validación de contenido ────────────────
    for key in CERT_SECRET_KEYS:
        if key in data_fields:
            try:
                decoded = base64.b64decode(data_fields[key])
                if _PEM_MARKER in decoded:
                    return decoded, key
            except Exception:
                continue

    # ── Pasada 2: cualquier key con contenido PEM ─────────────────────────
    for key, b64_val in data_fields.items():
        if key.lower().endswith(".jks"):
            continue  # saltar keystores
        if key in CERT_SECRET_KEYS:
            continue  # ya se probó en pasada 1
        try:
            decoded = base64.b64decode(b64_val)
            if _PEM_MARKER in decoded:
                return decoded, key
        except Exception:
            continue

    return None, ""


def _extract_cert_pem(secret_data: dict) -> Optional[bytes]:
    """
    Extrae el primer campo de certificado encontrado en el secret (base64 → bytes PEM).
    Wrapper sobre _extract_cert_pem_with_key.
    """
    pem, _ = _extract_cert_pem_with_key(secret_data)
    return pem


def _parse_x509(pem: bytes):
    """
    Parsea un certificado X.509 PEM.
    Requiere: pip install cryptography
    Retorna el objeto Certificate de cryptography, o None si falla.
    """
    try:
        from cryptography import x509  # type: ignore[import-untyped]
        from cryptography.hazmat.backends import default_backend  # type: ignore[import-untyped]

        # Puede venir una cadena de certs — tomamos el primero
        pem_str = pem.decode("utf-8", errors="ignore")
        begin = "-----BEGIN CERTIFICATE-----"
        end = "-----END CERTIFICATE-----"
        if begin in pem_str:
            first = pem_str[pem_str.index(begin): pem_str.index(end) + len(end)]
            pem = first.encode()

        return x509.load_pem_x509_certificate(pem, default_backend())
    except Exception as exc:
        print(f"  [K8s] No se pudo parsear el cert PEM: {exc}")
        return None


# ─── Extracción JKS (NUEVO) ──────────────────────────────────────────────────

def _extract_jks_data(secret_data: dict) -> tuple[Optional[bytes], str]:
    """
    Busca dinámicamente cualquier key que termine en .jks dentro del secret.
    Retorna (bytes_jks, key_name) o (None, "").
    """
    data_fields = secret_data.get("data", {})
    for key in data_fields:
        if key.lower().endswith(".jks"):
            try:
                return base64.b64decode(data_fields[key]), key
            except Exception:
                continue
    return None, ""


def _get_jks_password(
    namespace: str,
    secret_name: str,
    jks_key: str,
    context: str,
    original_secret_data: Optional[dict] = None,
) -> str:
    """
    Descubre la contraseña del keystore JKS con búsqueda en profundidad.

    Niveles de búsqueda:
      1. Busca dentro del mismo secreto original (claves 'password', 'pass', etc.)
      2. Busca en secretos derivados con múltiples patrones de nombres
      3. Parsea formato IBM ACE (setdbparms.txt) o usa contenido directo

    Fallback: JKS_FALLBACK_PASSWORD ("changeit")
    """
    jks_base = jks_key.replace(".jks", "")

    def _extract_password_from_content(content: str) -> Optional[str]:
        """Intenta extraer la password de un contenido de texto."""
        # Intentar formato IBM ACE: "brokerKeystore::password  aceuser 12345678"
        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if f"{jks_base}::password" in line.lower() or f"{jks_base}::password" in line:
                parts = line.split()
                if len(parts) >= 3:
                    return parts[2]
        # Buscar cualquier línea con ::password
        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "::password" in line.lower():
                parts = line.split()
                if len(parts) >= 3:
                    return parts[2]
        # Si es una sola línea sin espacios, es la password directa
        stripped = content.strip()
        if stripped and "\n" not in stripped and " " not in stripped and len(stripped) > 2:
            return stripped
        return None

    def _try_extract_from_data(data_fields: dict, source_label: str) -> Optional[str]:
        """Intenta extraer password de un dict de data (base64)."""
        # Priorizar keys conocidas
        priority_keys = [JKS_PASSWORD_DATA_KEY, "password", "pass", "keystore-password",
                         "jks-password", "storepass", "keystorePassword"]
        # Primero buscar keys exactas
        for pk in priority_keys:
            if pk in data_fields:
                try:
                    content = base64.b64decode(data_fields[pk]).decode("utf-8", errors="ignore").strip()
                    pwd = _extract_password_from_content(content)
                    if pwd:
                        print(f"  [K8s] ✓ Password encontrada en {source_label} (key='{pk}')")
                        return pwd
                except Exception:
                    pass
        # Luego buscar keys parciales
        for key, b64_val in data_fields.items():
            key_lower = key.lower()
            if any(p in key_lower for p in ["password", "pass", "pwd", "secret", "cred"]):
                try:
                    content = base64.b64decode(b64_val).decode("utf-8", errors="ignore").strip()
                    pwd = _extract_password_from_content(content) or content
                    if pwd and len(pwd) > 2:
                        print(f"  [K8s] ✓ Password encontrada en {source_label} (key='{key}')")
                        return pwd
                except Exception:
                    pass
        # Último recurso: primera key de texto que parezca password
        for key, b64_val in data_fields.items():
            if key.lower().endswith(".jks"):
                continue  # Saltar el propio JKS
            try:
                content = base64.b64decode(b64_val).decode("utf-8", errors="ignore").strip()
                pwd = _extract_password_from_content(content)
                if pwd:
                    print(f"  [K8s] ✓ Password encontrada en {source_label} (key='{key}', fallback)")
                    return pwd
            except Exception:
                pass
        return None

    # ── Nivel 1: Buscar en el mismo secreto original ─────────────────────
    if original_secret_data and "data" in original_secret_data:
        pwd = _try_extract_from_data(original_secret_data["data"], f"secreto original '{secret_name}'")
        if pwd:
            return pwd

    # ── Nivel 2: Buscar en secretos derivados (múltiples patrones) ───────
    candidate_names = []
    if secret_name.startswith("keystore-"):
        candidate_names.append(f"{JKS_PASSWORD_SECRET_PREFIX}{secret_name[len('keystore-'):]}")
    candidate_names.extend([
        f"{secret_name}-password",
        f"{secret_name}-pass",
        f"password-{secret_name}",
        f"{JKS_PASSWORD_SECRET_PREFIX}{secret_name}",
        f"{jks_base}-password",
        f"password-{jks_base}",
    ])
    # Eliminar duplicados preservando orden
    candidate_names = list(dict.fromkeys(candidate_names))

    for pwd_secret_name in candidate_names:
        try:
            pwd_secret = _run_kubectl("get", "secret", pwd_secret_name, "-n", namespace, context=context)
            data_fields = pwd_secret.get("data", {})
            if not data_fields:
                continue
            pwd = _try_extract_from_data(data_fields, f"secreto derivado '{pwd_secret_name}'")
            if pwd:
                return pwd
        except RuntimeError:
            continue

    print(f"  [K8s] ⚠ No se encontró password en ningún lugar → usando fallback")
    return JKS_FALLBACK_PASSWORD


# ─── Búsqueda profunda de secretos (Self-Healing) ────────────────────────────

def _deep_search_secret(
    secret_name: str,
    original_namespace: str,
    original_context: str,
    original_cluster: str,
    ambiente: Optional[str] = None,
) -> Optional[tuple[dict, str, str, str]]:
    """
    Búsqueda profunda de un secreto en toda la infraestructura K8s.

    Niveles:
      1. Búsqueda directa (namespace + cluster del portal)
      2. Búsqueda global en el mismo cluster (todos los namespaces)
         — si hay más de un resultado y se conoce el ambiente, filtra por ambiente
      3. Búsqueda multi-cluster (clusters descubiertos o estáticos)
         — descarta clusters donde el ambiente no coincide

    Retorna (secret_data, namespace_real, context_real, cluster_real) o None.
    """
    # ── Nivel 1: Búsqueda directa ────────────────────────────────────────
    print(f"  [🔍 Nivel 1] Buscando '{secret_name}' en {original_namespace} @ {original_cluster}...")
    try:
        secret_data = _run_kubectl(
            "get", "secret", secret_name, "-n", original_namespace,
            context=original_context,
        )
        print(f"  [🔍] ✓ Encontrado directamente")
        return secret_data, original_namespace, original_context, original_cluster
    except RuntimeError as exc:
        err_msg = str(exc)
        if "Forbidden" in err_msg:
            print(f"  [🔍] ⚠ Acceso denegado (Forbidden) en {original_namespace} @ {original_cluster}")
        elif "not found" in err_msg.lower() or "NotFound" in err_msg:
            print(f"  [🔍] ✗ No existe en {original_namespace}")
        else:
            print(f"  [🔍] ⚠ Error: {err_msg[:80]}")

    # ── Nivel 2: Búsqueda global en el mismo cluster ─────────────────────
    print(f"  [🔍 Nivel 2] Buscando '{secret_name}' en TODOS los namespaces de {original_cluster}...")
    try:
        cmd = ["kubectl", "--context", original_context,
               "get", "secrets", "-A",
               "--field-selector", f"metadata.name={secret_name}",
               "-o", "json", "--request-timeout=10s"]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if result.returncode == 0:
            all_secrets = json.loads(result.stdout)
            items = all_secrets.get("items", [])
            if items:
                # Si hay más de un resultado y conocemos el ambiente, filtrar
                if len(items) > 1 and ambiente:
                    filtered = [
                        it for it in items
                        if _ambiente_coincide(ambiente, it.get("metadata", {}).get("namespace", ""))
                    ]
                    if not filtered:
                        print(f"  [🔍] ⚠ {len(items)} resultados pero ninguno coincide con ambiente '{ambiente}' — usando el primero")
                        filtered = items
                    elif len(filtered) > 1:
                        nss = [it.get("metadata", {}).get("namespace", "?") for it in filtered]
                        print(f"  [🔍] ⚠ Múltiples coincidencias de ambiente: {nss} — usando el primero")
                    items = filtered

                found = items[0]
                real_ns = found.get("metadata", {}).get("namespace", original_namespace)
                print(f"  [🔍] ✓ ¡Encontrado en namespace '{real_ns}' (portal decía '{original_namespace}')!")
                return found, real_ns, original_context, original_cluster
            else:
                print(f"  [🔍] ✗ No existe en ningún namespace de {original_cluster}")
        else:
            stderr = result.stderr.strip()
            if "Forbidden" in stderr:
                print(f"  [🔍] ⚠ Sin permisos para buscar globalmente en {original_cluster}")
            else:
                print(f"  [🔍] ⚠ Búsqueda global falló: {stderr[:80]}")
    except (subprocess.TimeoutExpired, Exception) as e:
        print(f"  [🔍] ⚠ Timeout en búsqueda global: {e}")

    # ── Nivel 3: Búsqueda multi-cluster (dinámico o estático) ─────────────
    other_clusters = _candidatos_otros_clusters(original_context)
    if other_clusters:
        print(f"  [🔍 Nivel 3] Buscando '{secret_name}' en {len(other_clusters)} cluster(s) adicional(es)...")
        for alt_cluster, alt_ctx in other_clusters.items():
            try:
                cmd = ["kubectl", "--context", alt_ctx,
                       "get", "secrets", "-A",
                       "--field-selector", f"metadata.name={secret_name}",
                       "-o", "json", "--request-timeout=10s"]
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
                if result.returncode == 0:
                    all_secrets = json.loads(result.stdout)
                    items = all_secrets.get("items", [])
                    if items:
                        found = items[0]
                        real_ns = found.get("metadata", {}).get("namespace", "")
                        # Verificar ambiente antes de aceptar
                        if ambiente and not _ambiente_coincide(ambiente, real_ns):
                            print(f"  [🔍] ⚠ Encontrado en {alt_cluster}/{real_ns} pero ambiente no coincide (esperado: {ambiente}) — descartado")
                            continue
                        print(f"  [🔍] ✓ ¡Encontrado en {alt_cluster} / {real_ns}!")
                        return found, real_ns, alt_ctx, alt_cluster
            except (subprocess.TimeoutExpired, RuntimeError, Exception):
                continue
        print(f"  [🔍] ✗ No encontrado en ningún cluster configurado")

    return None


_keytool_cache: Optional[str] = None  # Cache para no buscar cada vez


def _find_keytool() -> str:
    """
    Encuentra la ruta de keytool de forma robusta y multiplataforma.

    Orden de búsqueda:
      1. Cache (si ya se encontró antes)
      2. PATH del sistema (shutil.which)
      3. JAVA_HOME / JDK_HOME / JRE_HOME
      4. Directorios comunes de Java por OS:
         - Windows: Program Files, scoop, chocolatey
         - Linux: /usr/lib/jvm, /usr/java, /opt/java, snap, sdkman
         - macOS: /Library/Java, Homebrew, /usr/libexec
      5. macOS: /usr/libexec/java_home helper
      6. Fallback: "keytool" (deja que el OS lo intente)
    """
    global _keytool_cache
    if _keytool_cache:
        return _keytool_cache

    import shutil
    import platform

    is_windows = platform.system() == "Windows"
    exe_name = "keytool.exe" if is_windows else "keytool"

    # ── 1. PATH del sistema ──────────────────────────────────────────────
    kt = shutil.which("keytool")
    if kt:
        _keytool_cache = kt
        return kt

    # ── 2. JAVA_HOME / JDK_HOME / JRE_HOME ──────────────────────────────
    for env_var in ["JAVA_HOME", "JDK_HOME", "JRE_HOME"]:
        java_dir = os.environ.get(env_var)
        if java_dir:
            candidate = os.path.join(java_dir, "bin", exe_name)
            if os.path.isfile(candidate):
                print(f"  [K8s] 🔧 keytool via ${env_var}: {candidate}")
                _keytool_cache = candidate
                return candidate

    # ── 3. Directorios comunes por OS ────────────────────────────────────
    search_dirs: list[str] = []
    user_home = os.path.expanduser("~")

    if is_windows:
        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        search_dirs = [
            os.path.join(pf, "Java"),
            os.path.join(pf, "Eclipse Adoptium"),
            os.path.join(pf, "Microsoft"),
            os.path.join(pf, "Amazon Corretto"),
            os.path.join(pf, "Zulu"),
            os.path.join(pf, "BellSoft"),
            os.path.join(pf, "GraalVM"),
            os.path.join(pf, "SapMachine"),
            os.path.join(pf, "Red Hat"),
            os.path.join(pf86, "Java"),
            os.path.join(pf86, "Eclipse Adoptium"),
            os.path.join(user_home, "scoop", "apps"),
            r"C:\tools",
            r"C:\ProgramData\chocolatey\lib",
        ]
    else:
        search_dirs = [
            "/usr/lib/jvm",
            "/usr/java",
            "/usr/local/java",
            "/opt/java",
            "/opt/jdk",
            "/snap/openjdk",
            os.path.join(user_home, ".sdkman", "candidates", "java"),
            "/Library/Java/JavaVirtualMachines",
            "/System/Library/Java/JavaVirtualMachines",
            "/usr/local/opt",
            "/opt/homebrew/opt",
            "/usr/local/Cellar",
            "/opt/homebrew/Cellar",
        ]

    for base_dir in search_dirs:
        if not os.path.isdir(base_dir):
            continue
        for root, dirs, files in os.walk(base_dir):
            if exe_name in files:
                path = os.path.join(root, exe_name)
                print(f"  [K8s] 🔧 keytool encontrado: {path}")
                _keytool_cache = path
                return path
            # Limitar profundidad (máx 4 niveles) para no escanear infinitamente
            if root.count(os.sep) - base_dir.count(os.sep) > 4:
                dirs.clear()

    # ── 4. macOS: /usr/libexec/java_home helper ──────────────────────────
    if platform.system() == "Darwin":
        try:
            result = subprocess.run(
                ["/usr/libexec/java_home"], capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                candidate = os.path.join(result.stdout.strip(), "bin", "keytool")
                if os.path.isfile(candidate):
                    print(f"  [K8s] 🔧 keytool via java_home: {candidate}")
                    _keytool_cache = candidate
                    return candidate
        except Exception:
            pass

    print("  [K8s] ⚠ keytool no encontrado — usando fallback")
    return "keytool"


def _parse_jks_certs(jks_bytes: bytes, password: str) -> list[dict]:
    """
    Parsea un keystore JKS usando keytool y retorna info de cada certificado.

    Ejecuta: keytool -list -v -keystore <tempfile> -storepass <password>

    Retorna lista de dicts:
      [{"alias": "...", "cn": "...", "not_before": datetime, "not_after": datetime}, ...]
    """
    # Guardar JKS a archivo temporal
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".jks")
    try:
        os.write(tmp_fd, jks_bytes)
        os.close(tmp_fd)

        keytool_path = _find_keytool()
        cmd = [
            keytool_path, "-list", "-v",
            "-keystore", tmp_path,
            "-storepass", password,
        ]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=KEYTOOL_TIMEOUT,
        )

        if result.returncode != 0:
            err_msg = (result.stderr or result.stdout or "").strip()[:200]
            print(f"  [K8s] ⚠ keytool falló (rc={result.returncode}): {err_msg}")
            return []

        return _parse_keytool_output(result.stdout)

    except FileNotFoundError:
        print("  [K8s] ⚠ keytool no encontrado — instala Java/JDK o agrega keytool al PATH")
        return []
    except subprocess.TimeoutExpired:
        print(f"  [K8s] ⚠ keytool timeout ({KEYTOOL_TIMEOUT}s)")
        return []
    except Exception as exc:
        print(f"  [K8s] ⚠ Error al ejecutar keytool: {exc}")
        return []
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def _parse_keytool_output(output: str) -> list[dict]:
    """
    Parsea la salida de `keytool -list -v` y extrae info de cada certificado.

    Bloques de ejemplo:
        Alias name: digicert-root
        Creation date: Jan 8, 2013
        Entry type: trustedCertEntry

        Owner: CN=DigiCert Global Root G2, OU=www.digicert.com, O=DigiCert Inc, C=US
        Issuer: CN=DigiCert Global Root G2, OU=www.digicert.com, O=DigiCert Inc, C=US
        Valid from: Tue Jan 08 03:00:00 AST 2013 until: Fri Jan 15 04:00:00 AST 2038

    Retorna lista de dicts con alias, cn, not_before, not_after.
    """
    entries: list[dict] = []
    current: dict = {}

    for line in output.splitlines():
        line = line.strip()

        # Nuevo alias
        if line.startswith("Alias name:"):
            if current.get("alias"):
                entries.append(current)
            current = {"alias": line.split(":", 1)[1].strip()}

        # Owner (contiene CN)
        elif line.startswith("Owner:"):
            owner = line.split(":", 1)[1].strip()
            cn_match = re.search(r"CN=([^,]+)", owner, re.IGNORECASE)
            if cn_match:
                current["cn"] = cn_match.group(1).strip()
            else:
                current["cn"] = owner
            
            o_match = re.search(r"O=([^,]+)", owner, re.IGNORECASE)
            if o_match:
                current["organization"] = o_match.group(1).strip()
            
            ou_match = re.search(r"OU=([^,]+)", owner, re.IGNORECASE)
            if ou_match:
                current["organizational_unit"] = ou_match.group(1).strip()
            # Guardar el Subject completo para el Excel
            current["full_subject"] = owner

        # Valid from ... until ...
        elif "Valid from:" in line and "until:" in line:
            not_before, not_after = _parse_keytool_validity(line)
            current["not_before"] = not_before
            current["not_after"] = not_after

    # Último entry
    if current.get("alias"):
        entries.append(current)

    return entries


def _parse_keytool_validity(line: str) -> tuple[Optional[datetime], Optional[datetime]]:
    """
    Parsea la línea de validez de keytool.

    Formato:
      "Valid from: Tue Jan 08 03:00:00 AST 2013 until: Fri Jan 15 04:00:00 AST 2038"
    """
    not_before = None
    not_after = None

    # Extraer las dos partes
    match = re.match(
        r"Valid from:\s*(.+?)\s+until:\s*(.+)",
        line.strip(),
        re.IGNORECASE,
    )
    if not match:
        return None, None

    from_str = match.group(1).strip()
    until_str = match.group(2).strip()

    not_before = _parse_keytool_date(from_str)
    not_after = _parse_keytool_date(until_str)

    return not_before, not_after


def _parse_keytool_date(date_str: str) -> Optional[datetime]:
    """
    Parsea una fecha de keytool.

    Formatos posibles:
      "Tue Jan 08 03:00:00 AST 2013"
      "Fri Jan 15 04:00:00 EST 2038"
      "Mon May 25 20:00:00 UTC 2015"
    """
    # Quitar la zona horaria (varía según locale) y parsear
    # Formato: DOW MON DD HH:MM:SS TZ YYYY
    formats = [
        "%a %b %d %H:%M:%S %Z %Y",   # Con timezone reconocido
        "%a %b %d %H:%M:%S %Y",       # Sin timezone
    ]

    for fmt in formats:
        try:
            dt = datetime.strptime(date_str, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue

    # Fallback: quitar la zona horaria manualmente y reintentar
    # "Tue Jan 08 03:00:00 AST 2013" → "Tue Jan 08 03:00:00 2013"
    parts = date_str.split()
    if len(parts) == 6:
        # Quitar el 5to elemento (timezone)
        cleaned = f"{parts[0]} {parts[1]} {parts[2]} {parts[3]} {parts[5]}"
        try:
            dt = datetime.strptime(cleaned, "%a %b %d %H:%M:%S %Y")
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            pass

    return None


# ─── Servicio principal ───────────────────────────────────────────────────────

class K8sService:
    """
    Interfaz de alto nivel para leer certificados del cluster Kubernetes.
    """

    def get_context(self, cluster_name: str) -> str:
        """Retorna el mismo nombre (ya no hay mapeo estático)."""
        return cluster_name


    # ── get_secret con búsqueda profunda (Self-Healing) ────────────────────────

    def get_secret(self, cluster_name: str, namespace: str, secret_name: str,
                   ambiente: Optional[str] = None) -> Optional[K8sCert]:
        """
        Lee un secret específico del cluster y retorna un K8sCert.
        Si no lo encuentra, activa búsqueda profunda (namespace global + multi-cluster).
        Retorna None si no existe en ningún lugar.
        """
        context = self.get_context(cluster_name)

        # ── Búsqueda profunda (3 niveles) ─────────────────────────────────
        result = _deep_search_secret(secret_name, namespace, context, cluster_name,
                                     ambiente=ambiente)
        if not result:
            return None

        secret_data, real_ns, real_ctx, real_cluster = result

        pem = _extract_cert_pem(secret_data)
        if not pem:
            print(f"  [K8s] ⚠ Secret '{secret_name}' no contiene campos de certificado conocidos.")
            return None

        cert_obj = _parse_x509(pem)
        cn = secret_name
        san: list[str] = []
        org: str = ""
        ou: str = ""
        not_before = None
        not_after = None

        if cert_obj:
            cn, san, org, ou = _extract_cn_and_san(cert_obj)
            cn = cn or (san[0] if san else secret_name)

            try:
                not_after = cert_obj.not_valid_after_utc
                not_before = cert_obj.not_valid_before_utc
            except AttributeError:
                not_after = cert_obj.not_valid_after
                not_before = cert_obj.not_valid_before

        secret_type_map = {"kubernetes.io/tls": "CRT", "Opaque": "Opaque"}
        raw_type = secret_data.get("type", "Opaque")
        secret_type = secret_type_map.get(raw_type, raw_type)

        return K8sCert(
            cluster=real_cluster,
            namespace=real_ns,
            secret_name=secret_name,
            common_name=cn,
            organization=org,
            organizational_unit=ou,
            not_before=not_before,
            not_after=not_after,
            secret_type=secret_type,
            san=san,
            cert_pem=pem,
        )

    # ── get_secret_all_certs con búsqueda profunda (Self-Healing) ──────────────

    def get_secret_all_certs(
        self, cluster_name: str, namespace: str, secret_name: str,
        ambiente: Optional[str] = None,
    ) -> list[K8sCert]:
        """
        Lee un secret y retorna TODOS los certificados que contiene.
        Usa búsqueda profunda (namespace global + multi-cluster) si no lo encuentra.

        Para CRT:  retorna lista de 1 elemento
        Para JKS:  retorna N elementos (uno por alias del keystore)
        Si no hay cert reconocible: retorna lista vacía

        Útil cuando se conoce el (cluster, namespace, secret_name) pero podría
        haber cambiado de ubicación (ej. desde un ticket o el portal).
        """
        context = self.get_context(cluster_name)

        # ── Búsqueda profunda (3 niveles) ─────────────────────────────────
        result = _deep_search_secret(secret_name, namespace, context, cluster_name,
                                     ambiente=ambiente)
        if not result:
            return []

        secret_data, real_ns, real_ctx, real_cluster = result
        pod_map = self._get_secret_to_pod_map(real_cluster, real_ns, real_ctx)
        pods_info = pod_map.get(secret_name, {"pods": set(), "nodes": set()})
        pods_str = ", ".join(sorted(pods_info["pods"]))
        nodes_str = ", ".join(sorted(pods_info["nodes"]))
        
        return _build_certs_from_secret_data(
            secret_data, real_cluster, real_ns, secret_name, real_ctx,
            pods=pods_str, nodes=nodes_str
        )

    # ── list_namespaces (NUEVO — exploración masiva) ────────────────────────

    def list_namespaces(self, cluster_name: str) -> list[str]:
        """
        Lista TODOS los namespaces de un cluster.
        Retorna lista vacía si el cluster no es accesible (Forbidden, timeout,
        contexto inexistente, etc.) — el caller debe interpretar [] como
        "cluster inaccesible", ya que un cluster real siempre tiene al menos
        los namespaces de sistema.
        """
        context = self.get_context(cluster_name)
        try:
            result = _run_kubectl("get", "namespaces", context=context)
        except RuntimeError as exc:
            print(f"  [K8s] ⚠ No se pudo listar namespaces de '{cluster_name}': {exc}")
            return []

        return [
            ns
            for item in result.get("items", [])
            if (ns := item.get("metadata", {}).get("name", ""))
        ]

    def _get_secret_to_pod_map(self, cluster_name: str, namespace: str, context: str) -> dict[str, dict]:
        """
        Retorna un diccionario mapeando el nombre de cada secret con los pods
        y nodos que lo consumen (vía volúmenes o variables de entorno).
        Ejemplo: {"my-secret": {"pods": {"pod-1"}, "nodes": {"node-1"}}}
        """
        mapping = {}
        try:
            result = _run_kubectl("get", "pods", "-n", namespace, "-o", "json", context=context)
            for pod in result.get("items", []):
                pod_name = pod.get("metadata", {}).get("name", "")
                node_name = pod.get("spec", {}).get("nodeName", "")
                if not pod_name:
                    continue
                
                secrets_used = set()
                
                # 1. Buscar en volúmenes
                for vol in pod.get("spec", {}).get("volumes", []):
                    secret = vol.get("secret", {})
                    if "secretName" in secret:
                        secrets_used.add(secret["secretName"])
                
                # 2. Buscar en variables de entorno (env y envFrom)
                for container in pod.get("spec", {}).get("containers", []):
                    for env_from in container.get("envFrom", []):
                        if "secretRef" in env_from and "name" in env_from["secretRef"]:
                            secrets_used.add(env_from["secretRef"]["name"])
                    for env in container.get("env", []):
                        val_from = env.get("valueFrom", {})
                        if "secretKeyRef" in val_from and "name" in val_from["secretKeyRef"]:
                            secrets_used.add(val_from["secretKeyRef"]["name"])
                            
                for s_name in secrets_used:
                    if s_name not in mapping:
                        mapping[s_name] = {"pods": set(), "nodes": set()}
                    mapping[s_name]["pods"].add(pod_name)
                    if node_name:
                        mapping[s_name]["nodes"].add(node_name)
        except Exception as exc:
            pass
            
        return mapping

    # ── list_secrets_with_all_certs (NUEVO — exploración masiva) ───────────

    def list_secrets_with_all_certs(self, cluster_name: str, namespace: str) -> list[K8sCert]:
        """
        Lista TODOS los secrets de un namespace y retorna TODOS los
        certificados que contengan (CRT y CADA alias dentro de cada JKS).

        A diferencia de list_secrets_with_certs (que usa get_secret y solo
        captura el primer cert por secret), este método usa get_secret_all_certs
        para no perder ningún alias dentro de un mismo keystore JKS.

        No usa búsqueda profunda: el secret ya se sabe que existe en esta
        ubicación porque se acaba de listar, así que se evita el costo de
        intentar otros namespaces/clusters innecesariamente durante un
        escaneo masivo.
        """
        context = self.get_context(cluster_name)
        try:
            result = _run_kubectl("get", "secrets", "-n", namespace, context=context)
        except RuntimeError as exc:
            print(f"  [K8s] ⚠ {exc}")
            return []

        pod_map = self._get_secret_to_pod_map(cluster_name, namespace, context)

        all_certs: list[K8sCert] = []
        for item in result.get("items", []):
            secret_name = item.get("metadata", {}).get("name", "")
            if not secret_name:
                continue
            
            pods_info = pod_map.get(secret_name, {"pods": set(), "nodes": set()})
            pods_str = ", ".join(sorted(pods_info["pods"]))
            nodes_str = ", ".join(sorted(pods_info["nodes"]))
            
            certs = _build_certs_from_secret_data(
                item, cluster_name, namespace, secret_name, context,
                pods=pods_str, nodes=nodes_str
            )
            all_certs.extend(certs)
        return all_certs

    # ── Método existente (sin cambios) ────────────────────────────────────────

    def list_secrets_with_certs(self, cluster_name: str, namespace: str) -> list[K8sCert]:
        """
        Lista todos los secrets de un namespace y retorna solo los que
        contienen un certificado X.509 reconocible.
        """
        context = self.get_context(cluster_name)
        print(f"  [K8s] Listando secrets en {namespace} @ {cluster_name}...")
        try:
            result = _run_kubectl("get", "secrets", "-n", namespace, context=context)
        except RuntimeError as exc:
            print(f"  [K8s] ⚠ {exc}")
            return []

        certs: list[K8sCert] = []
        items = result.get("items", [])
        for item in items:
            name = item.get("metadata", {}).get("name", "")
            cert = self.get_secret(cluster_name, namespace, name)
            if cert:
                certs.append(cert)
        return certs

