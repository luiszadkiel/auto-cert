"""
services/jks_updater_service.py
────────────────────────────────
Núcleo del modo jks-update: detecta certificados JKS vencidos dentro de un
keystore, descarga el cert fresco del host TLS, reconstruye el .jks y lo
sube de vuelta al Secret de Kubernetes.

Dependencias: solo stdlib + cryptography (ya instalado).
Sin dependencias nuevas.

Supuestos operativos
─────────────────────
• CN == host alcanzable: el CN del alias en el JKS se usa como hostname TLS.
  Si el CN no coincide con el endpoint real → OMITIDO("CN no es host alcanzable").
  Si hace falta, agregar una tabla de override externa.

• getpeercert() solo retorna el leaf del servidor. Si algún alias confiaba
  en un intermediate (no el leaf), la re-descarga no lo captura.

• Egress a host:443 debe estar permitido desde el entorno de ejecución.
  En entornos AKS con network policy restrictiva puede haber falsos OMITIDO.
"""

import base64
import json
import os
import re
import socket
import ssl
import subprocess
import tempfile
from datetime import datetime, timezone
from typing import Optional

from config.settings import (
    JKS_BACKUP_DIR,
    JKS_REFETCH_PORT,
    JKS_REFETCH_TIMEOUT,
    KEYTOOL_TIMEOUT,
    KUBECTL_TIMEOUT,
)

# Reutilizar helpers de keytool del servicio K8s (evita duplicar código)
from services.k8s_service import _find_keytool, _parse_keytool_date


# ─── Estados de salida ────────────────────────────────────────────────────────

ESTADO_ACTUALIZADO       = "ACTUALIZADO"
ESTADO_ACTUALIZARIA      = "ACTUALIZARIA"
ESTADO_DUPLICADO_VIGENTE = "DUPLICADO_VIGENTE"
ESTADO_DUPLICADO_VENCIDO = "DUPLICADO_VENCIDO"
ESTADO_OMITIDO           = "OMITIDO"


# ─── Utilidades de certificado ────────────────────────────────────────────────

def extract_cn(subject: str) -> str:
    """Extrae el CN= de una cadena Distinguished Name estilo keytool."""
    m = re.search(r"CN=([^,]+)", subject, re.IGNORECASE)
    return m.group(1).strip() if m else subject.strip()


def cn_to_host(cn: str) -> Optional[str]:
    """
    Decide si el CN es un hostname TLS alcanzable.

    Descarta (devuelve None):
      • CN con espacios → nombre de organización / CA
      • CN sin punto    → no es FQDN
      • CN wildcard     → *.example.com no se puede conectar literalmente
    """
    if not cn:
        return None
    if " " in cn:
        return None
    if "." not in cn:
        return None
    if cn.startswith("*."):
        return None
    return cn


def cert_not_after(pem_bytes: bytes) -> Optional[datetime]:
    """Lee la fecha de vencimiento de un cert PEM. Devuelve datetime UTC-aware."""
    try:
        from cryptography import x509                              # type: ignore[import-untyped]
        from cryptography.hazmat.backends import default_backend   # type: ignore[import-untyped]
        cert = x509.load_pem_x509_certificate(pem_bytes, default_backend())
        try:
            return cert.not_valid_after_utc
        except AttributeError:
            dt = cert.not_valid_after
            return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
    except Exception as exc:
        print(f"  [jks-update] ⚠ No se pudo parsear PEM (not_after): {exc}")
        return None


# ─── Bajar cert fresco del host (stdlib solo) ─────────────────────────────────

def fetch_fresh_cert_pem(
    host: str,
    port: int = JKS_REFETCH_PORT,
    timeout: int = JKS_REFETCH_TIMEOUT,
) -> Optional[bytes]:
    """
    Abre una conexión TLS a host:port y extrae el certificado leaf del servidor.

    • No verifica la cadena (CERT_NONE) — solo lee el cert público.
    • Solo usa stdlib: ssl + socket.
    • Retorna bytes PEM o None si la conexión falla.
    """
    try:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        with socket.create_connection((host, port), timeout=timeout) as raw_sock:
            with ctx.wrap_socket(raw_sock, server_hostname=host) as tls_sock:
                der_bytes = tls_sock.getpeercert(binary_form=True)

        if not der_bytes:
            print(f"  [jks-update] ⚠ {host}:{port} → getpeercert devolvió vacío")
            return None

        # DER → PEM
        b64 = base64.b64encode(der_bytes).decode("ascii")
        pem_lines = ["-----BEGIN CERTIFICATE-----"]
        pem_lines += [b64[i:i + 64] for i in range(0, len(b64), 64)]
        pem_lines.append("-----END CERTIFICATE-----")
        return "\n".join(pem_lines).encode("ascii")

    except (socket.timeout, TimeoutError):
        print(f"  [jks-update] ⚠ {host}:{port} → timeout ({timeout}s)")
        return None
    except ConnectionRefusedError:
        print(f"  [jks-update] ⚠ {host}:{port} → conexión rechazada")
        return None
    except Exception as exc:
        print(f"  [jks-update] ⚠ {host}:{port} → {exc}")
        return None


# ─── Leer entradas del JKS ────────────────────────────────────────────────────

def _parse_list_output(output: str) -> list[dict]:
    """
    Parsea la salida de `keytool -list -v`.

    Extiende _parse_keytool_output de k8s_service añadiendo captura de:
      • Entry type: trustedCertEntry | PrivateKeyEntry
      • Subject completo (Owner:)

    Cada entry devuelta: {alias, entry_type, cn, subject, not_before, not_after}
    """
    entries: list[dict] = []
    current: dict = {}

    for line in output.splitlines():
        line = line.strip()

        if line.startswith("Alias name:"):
            if current.get("alias"):
                entries.append(current)
            current = {
                "alias": line.split(":", 1)[1].strip(),
                "entry_type": "trustedCertEntry",  # default
            }

        elif line.startswith("Entry type:"):
            current["entry_type"] = line.split(":", 1)[1].strip()

        elif line.startswith("Owner:"):
            subject = line.split(":", 1)[1].strip()
            current["subject"] = subject
            cn_m = re.search(r"CN=([^,]+)", subject, re.IGNORECASE)
            current["cn"] = cn_m.group(1).strip() if cn_m else subject

        elif "Valid from:" in line and "until:" in line:
            m = re.match(r"Valid from:\s*(.+?)\s+until:\s*(.+)", line, re.IGNORECASE)
            if m:
                current["not_before"] = _parse_keytool_date(m.group(1).strip())
                current["not_after"]  = _parse_keytool_date(m.group(2).strip())

    if current.get("alias"):
        entries.append(current)

    return entries


def parse_entries(jks_bytes: bytes, password: str) -> list[dict]:
    """
    Escribe el JKS a un temporal y ejecuta keytool -list -v.

    Retorna lista de dicts con alias, entry_type, cn, subject, not_before, not_after.
    Lista vacía si password incorrecta, JKS ilegible, o keytool no disponible.
    """
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".jks")
    try:
        os.write(tmp_fd, jks_bytes)
        os.close(tmp_fd)

        keytool = _find_keytool()
        result = subprocess.run(
            [keytool, "-list", "-v", "-keystore", tmp_path, "-storepass", password],
            capture_output=True,
            text=True,
            timeout=KEYTOOL_TIMEOUT,
        )
        if result.returncode != 0:
            err = (result.stderr or result.stdout or "").strip()[:200]
            print(f"  [jks-update] ⚠ keytool -list falló (rc={result.returncode}): {err}")
            return []

        return _parse_list_output(result.stdout)

    except FileNotFoundError:
        print("  [jks-update] ⚠ keytool no encontrado — instala Java/JDK")
        return []
    except subprocess.TimeoutExpired:
        print(f"  [jks-update] ⚠ keytool -list timeout ({KEYTOOL_TIMEOUT}s)")
        return []
    except Exception as exc:
        print(f"  [jks-update] ⚠ parse_entries: {exc}")
        return []
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


# ─── Backup ───────────────────────────────────────────────────────────────────

def backup_jks(
    jks_bytes: bytes,
    secret_name: str,
    jks_key: str,
    backup_dir: str = JKS_BACKUP_DIR,
) -> str:
    """
    Guarda el .jks original con timestamp antes de modificarlo.
    Retorna la ruta absoluta del backup creado.
    """
    os.makedirs(backup_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_key = jks_key.replace("/", "_").replace("\\", "_")
    filename = f"{secret_name}__{safe_key}__{stamp}.jks"
    path = os.path.join(backup_dir, filename)
    with open(path, "wb") as f:
        f.write(jks_bytes)
    print(f"  [jks-update] 💾 Backup → {os.path.abspath(path)}")
    return path


# ─── Reconstruir JKS ─────────────────────────────────────────────────────────

def _keytool_delete(keytool: str, jks_path: str, alias: str, password: str) -> bool:
    """Elimina un alias del keystore. Retorna True si tuvo éxito."""
    result = subprocess.run(
        [keytool, "-delete", "-alias", alias,
         "-keystore", jks_path, "-storepass", password],
        capture_output=True, text=True, timeout=KEYTOOL_TIMEOUT,
    )
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "").strip()[:150]
        print(f"  [jks-update] ⚠ keytool -delete alias='{alias}' falló: {err}")
        return False
    return True


def _keytool_import(
    keytool: str, jks_path: str, alias: str, cert_path: str, password: str
) -> bool:
    """Importa un cert PEM bajo el alias indicado. Retorna True si tuvo éxito."""
    result = subprocess.run(
        [keytool, "-importcert", "-noprompt",
         "-alias", alias,
         "-file", cert_path,
         "-keystore", jks_path,
         "-storepass", password],
        capture_output=True, text=True, timeout=KEYTOOL_TIMEOUT,
    )
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "").strip()[:150]
        print(f"  [jks-update] ⚠ keytool -importcert alias='{alias}' falló: {err}")
        return False
    return True


def prune_expired_aliases(
    jks_bytes: bytes,
    password: str,
    aliases: list[str],
) -> Optional[bytes]:
    """
    Elimina los aliases indicados del keystore.
    Devuelve bytes del JKS modificado, o None si algún delete falla.
    """
    if not aliases:
        return jks_bytes

    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".jks")
    try:
        os.write(tmp_fd, jks_bytes)
        os.close(tmp_fd)

        keytool = _find_keytool()
        for alias in aliases:
            if not _keytool_delete(keytool, tmp_path, alias, password):
                print(f"  [jks-update] ⚠ prune: falló al borrar alias '{alias}' — abortando prune")
                return None

        with open(tmp_path, "rb") as f:
            return f.read()

    except Exception as exc:
        print(f"  [jks-update] ⚠ prune_expired_aliases: {exc}")
        return None
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def rebuild_jks(
    jks_bytes: bytes,
    password: str,
    replacements: list[tuple[str, bytes]],
) -> Optional[bytes]:
    """
    Por cada (alias, cert_pem_bytes) en replacements:
      1. keytool -delete  (elimina el viejo; puede ya no existir si fue prunado)
      2. keytool -importcert -noprompt  (importa el cert fresco)

    Devuelve bytes del JKS modificado o None si algún import falla.
    """
    if not replacements:
        return jks_bytes

    jks_fd, jks_path = tempfile.mkstemp(suffix=".jks")
    cert_paths: list[str] = []
    try:
        os.write(jks_fd, jks_bytes)
        os.close(jks_fd)

        keytool = _find_keytool()
        for alias, pem_bytes in replacements:
            cert_fd, cert_path = tempfile.mkstemp(suffix=".pem")
            cert_paths.append(cert_path)
            os.write(cert_fd, pem_bytes)
            os.close(cert_fd)

            # Borrar viejo (ignora error si ya fue prunado)
            _keytool_delete(keytool, jks_path, alias, password)

            if not _keytool_import(keytool, jks_path, alias, cert_path, password):
                print(f"  [jks-update] ✗ rebuild_jks: falló import alias='{alias}'")
                return None

        with open(jks_path, "rb") as f:
            return f.read()

    except Exception as exc:
        print(f"  [jks-update] ⚠ rebuild_jks: {exc}")
        return None
    finally:
        for cp in cert_paths:
            try:
                os.unlink(cp)
            except OSError:
                pass
        try:
            os.unlink(jks_path)
        except OSError:
            pass


# ─── Escribir en el cluster (atómico) ────────────────────────────────────────

def replace_secret_jks(
    context: str,
    namespace: str,
    secret_name: str,
    jks_key: str,
    new_jks_bytes: bytes,
) -> bool:
    """
    Reemplaza SOLO la key data[jks_key] del Secret usando kubectl patch --type=json.

    Atómico: no toca otras keys del secret ni requiere manejar resourceVersion.
    """
    new_b64 = base64.b64encode(new_jks_bytes).decode("ascii")
    patch = json.dumps([{
        "op": "replace",
        "path": f"/data/{jks_key}",
        "value": new_b64,
    }])

    cmd = [
        "kubectl", "--context", context,
        "patch", "secret", secret_name,
        "-n", namespace,
        "--type=json",
        f"--patch={patch}",
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=KUBECTL_TIMEOUT,
        )
        if result.returncode != 0:
            err = (result.stderr or result.stdout or "").strip()[:200]
            print(f"  [jks-update] ✗ kubectl patch falló (rc={result.returncode}): {err}")
            return False
        print(f"  [jks-update] ✓ Secret '{namespace}/{secret_name}' key='{jks_key}' actualizado")
        return True
    except subprocess.TimeoutExpired:
        print(f"  [jks-update] ✗ kubectl patch timeout ({KUBECTL_TIMEOUT}s)")
        return False
    except Exception as exc:
        print(f"  [jks-update] ✗ replace_secret_jks: {exc}")
        return False


# ─── Normalización de identidad ───────────────────────────────────────────────

def _identity_key(entry: dict) -> str:
    """
    Clave de agrupación para detectar aliases duplicados dentro de un JKS.
    Usa el subject completo normalizado; cae a cn si no hay subject.
    """
    raw = entry.get("subject") or entry.get("cn") or entry.get("alias", "")
    return raw.strip().lower()


# ─── Helpers de registro ──────────────────────────────────────────────────────

def _record(
    cluster: str,
    namespace: str,
    secret_name: str,
    jks_key: str,
    alias: str,
    estado: str,
    motivo: str = "",
    entry: Optional[dict] = None,
    fresh_not_after: Optional[datetime] = None,
) -> dict:
    """Construye un dict de registro para el reporte."""
    not_after = None
    cn = ""
    entry_type = ""
    if entry:
        na = entry.get("not_after")
        not_after = na.isoformat() if na else None
        cn = entry.get("cn", "")
        entry_type = entry.get("entry_type", "")
    return {
        "cluster":         cluster,
        "namespace":       namespace,
        "secret_name":     secret_name,
        "jks_key":         jks_key,
        "alias":           alias,
        "cn":              cn,
        "entry_type":      entry_type,
        "not_after":       not_after,
        "fresh_not_after": fresh_not_after.isoformat() if fresh_not_after else None,
        "estado":          estado,
        "motivo":          motivo,
    }


def _mark_replacements(records: list[dict], estado: str, motivo: str = "") -> None:
    """Cambia el estado de todos los registros ACTUALIZARIA → estado dado."""
    for r in records:
        if r["estado"] == ESTADO_ACTUALIZARIA:
            r["estado"] = estado
            if motivo:
                r["motivo"] = motivo


# ─── Orquestación central ─────────────────────────────────────────────────────

def process_jks_secret(
    cluster: str,
    namespace: str,
    secret_name: str,
    jks_key: str,
    jks_bytes: bytes,
    password: str,
    context: str,
    now: datetime,
    apply_changes: bool,
    prune: bool,
    port: int = JKS_REFETCH_PORT,
    timeout: int = JKS_REFETCH_TIMEOUT,
) -> list[dict]:
    """
    Orquesta la actualización de un único keystore JKS.

    Emite por alias uno de estos estados:
      ACTUALIZADO       — cert nuevo descargado y subido al cluster
      ACTUALIZARIA      — dry-run: se actualizaría si apply_changes=True
      DUPLICADO_VIGENTE — alias vigente en un grupo con otro vigente (no se toca)
      DUPLICADO_VENCIDO — alias vencido redundante (candidato a prune)
      OMITIDO           — no se pudo actualizar; campo 'motivo' explica por qué
    """
    prefix = f"  [{cluster}/{namespace}/{secret_name}#{jks_key}]"
    print(f"{prefix} Procesando keystore ({len(jks_bytes)} bytes)...")

    # ── 1. Parsear ──────────────────────────────────────────────────────────
    entries = parse_entries(jks_bytes, password)
    if not entries:
        return [_record(
            cluster, namespace, secret_name, jks_key, alias="?",
            estado=ESTADO_OMITIDO,
            motivo="password incorrecta o JKS ilegible / keytool no disponible",
        )]

    print(f"{prefix} {len(entries)} alias(es) encontrados")

    # ── 2. Agrupar por identidad normalizada ────────────────────────────────
    groups: dict[str, list[dict]] = {}
    for entry in entries:
        key = _identity_key(entry)
        groups.setdefault(key, []).append(entry)

    # ── 3. Analizar cada grupo ───────────────────────────────────────────────
    records: list[dict] = []
    replacements: list[tuple[str, bytes]] = []   # (alias, fresh_pem)
    to_prune: list[str] = []                      # aliases duplicados a borrar

    for identity, group in groups.items():
        # Ordenar por not_after desc (None al final)
        def _sort_key(e: dict):
            na = e.get("not_after")
            if na is None:
                return datetime.min.replace(tzinfo=timezone.utc)
            return na if na.tzinfo else na.replace(tzinfo=timezone.utc)

        group_sorted = sorted(group, key=_sort_key, reverse=True)
        latest = group_sorted[0]
        duplicates = group_sorted[1:]  # aliases no-latest del grupo

        # Normalizar not_after del latest para comparación
        latest_na = latest.get("not_after")
        if latest_na and latest_na.tzinfo is None:
            latest_na = latest_na.replace(tzinfo=timezone.utc)
        latest_vigente = bool(latest_na and latest_na > now)

        # Todos los no-latest → DUPLICADO_VENCIDO (candidatos a prune siempre)
        for dup in duplicates:
            dup_alias = dup.get("alias", "?")
            records.append(_record(
                cluster, namespace, secret_name, jks_key,
                alias=dup_alias,
                estado=ESTADO_DUPLICADO_VENCIDO,
                entry=dup,
                motivo="Alias duplicado vencido (candidato a prune)",
            ))
            to_prune.append(dup_alias)

        # ── Caso A: latest vigente ──────────────────────────────────────────
        if latest_vigente:
            records.append(_record(
                cluster, namespace, secret_name, jks_key,
                alias=latest.get("alias", "?"),
                estado=ESTADO_DUPLICADO_VIGENTE,
                entry=latest,
                motivo="Certificado vigente — no requiere actualización",
            ))
            continue

        # ── Caso B: latest vencido → intentar actualizar ────────────────────
        alias = latest.get("alias", "?")
        entry_type = latest.get("entry_type", "trustedCertEntry")
        cn = latest.get("cn", "")

        if entry_type != "trustedCertEntry":
            records.append(_record(
                cluster, namespace, secret_name, jks_key,
                alias=alias, entry=latest,
                estado=ESTADO_OMITIDO,
                motivo="PrivateKeyEntry — requiere reemisión por CA (no auto-renovable)",
            ))
            continue

        host = cn_to_host(cn)
        if host is None:
            records.append(_record(
                cluster, namespace, secret_name, jks_key,
                alias=alias, entry=latest,
                estado=ESTADO_OMITIDO,
                motivo=f"CN='{cn}' no es un hostname TLS alcanzable (probable CA/trust anchor)",
            ))
            continue

        print(f"{prefix} Descargando cert de '{host}:{port}'...")
        fresh_pem = fetch_fresh_cert_pem(host, port=port, timeout=timeout)
        if fresh_pem is None:
            records.append(_record(
                cluster, namespace, secret_name, jks_key,
                alias=alias, entry=latest,
                estado=ESTADO_OMITIDO,
                motivo=f"Host '{host}:{port}' no alcanzable — verificar egress/network policy",
            ))
            continue

        fresh_exp = cert_not_after(fresh_pem)
        if fresh_exp is None or fresh_exp <= now:
            records.append(_record(
                cluster, namespace, secret_name, jks_key,
                alias=alias, entry=latest,
                estado=ESTADO_OMITIDO,
                motivo=f"Cert descargado de '{host}' también vencido — renovar en origen",
            ))
            continue

        # ✓ Cert fresco y vigente — candidato a actualizar
        replacements.append((alias, fresh_pem))
        records.append(_record(
            cluster, namespace, secret_name, jks_key,
            alias=alias, entry=latest,
            estado=ESTADO_ACTUALIZARIA,
            motivo=f"Cert fresco descargado de '{host}' (vence {fresh_exp.date()})",
            fresh_not_after=fresh_exp,
        ))

    # ── 4. Aplicar o dejar en dry-run ────────────────────────────────────────
    if not replacements:
        return records

    if not apply_changes:
        print(f"{prefix} 🔵 DRY-RUN — {len(replacements)} alias(es) se actualizarían")
        return records

    # ── Apply real ───────────────────────────────────────────────────────────
    print(f"{prefix} 🟢 APPLY — {len(replacements)} alias(es) a actualizar")

    backup_jks(jks_bytes, secret_name, jks_key)

    working_bytes = jks_bytes

    # Prune opcional (sobre los bytes actuales, antes del rebuild)
    if prune and to_prune:
        print(f"{prefix} ✂ Prunando {len(to_prune)} alias(es): {to_prune}")
        pruned = prune_expired_aliases(working_bytes, password, to_prune)
        if pruned is not None:
            working_bytes = pruned
        else:
            print(f"{prefix} ⚠ Prune falló — continuando sin prune")

    # Rebuild
    new_bytes = rebuild_jks(working_bytes, password, replacements)
    if new_bytes is None:
        _mark_replacements(records, ESTADO_OMITIDO, "Error reconstruyendo JKS (ver logs)")
        return records

    # Subir al cluster
    if not replace_secret_jks(context, namespace, secret_name, jks_key, new_bytes):
        _mark_replacements(records, ESTADO_OMITIDO, "Error subiendo el Secret al cluster (ver logs)")
        return records

    # Éxito total
    _mark_replacements(records, ESTADO_ACTUALIZADO)
    print(f"{prefix} ✅ {len(replacements)} alias(es) actualizados correctamente")
    return records
