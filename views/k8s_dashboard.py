"""
k8s_dashboard.py
─────────────────
Cuadro de resumen visual para la información extraída de Kubernetes.
Usado tanto por test_browser_sim.py como por run.py.
"""

from datetime import datetime, timezone

# ─── Colores ──────────────────────────────────────────────────────────────────
G = "\033[92m"; R = "\033[91m"; Y = "\033[93m"; C = "\033[96m"
M = "\033[95m"; B = "\033[1m"; D = "\033[2m"; X = "\033[0m"
BG_R = "\033[41m"; BG_G = "\033[42m"; BG_Y = "\033[43m"; BG_C = "\033[46m"

W = 90  # Ancho del cuadro


def _bar(found: int, total: int, width: int = 30) -> str:
    """Genera una barra visual de progreso tipo ████░░░░."""
    if total == 0:
        return f"{D}(sin datos){X}"
    pct = found / total
    filled = int(pct * width)
    empty = width - filled
    color = G if pct >= 0.8 else Y if pct >= 0.5 else R
    return f"{color}{'█' * filled}{D}{'░' * empty}{X} {found}/{total} ({pct:.0%})"


def _status_icon(estado: str) -> str:
    if estado == "VENCIDO":
        return f"{R}✗ VENCIDO{X}"
    elif estado == "ACTIVO":
        return f"{G}✓ ACTIVO {X}"
    return f"{Y}? {estado}{X}"


def print_k8s_dashboard(k8s_summary_logs: list, preflight_results: dict | None = None):
    """
    Imprime un dashboard visual completo con toda la información de K8s.

    Args:
        k8s_summary_logs: Lista de dicts con la info extraída. Cada dict tiene:
            - portal: str (cluster/ns/secret del portal)
            - real: str | None (ubicación real encontrada)
            - tipo: str (CRT, JKS, Opaque, —)
            - certs: list[dict] con cn, vence, estado
            - autofix: bool (si se auto-corrigió la ubicación)
            - password_source: str | None (de dónde vino la password)
        preflight_results: dict opcional con resultados del pre-flight check
    """
    if not k8s_summary_logs:
        return

    # ═══════════════════════════════════════════════════════════════════════
    # ENCABEZADO
    # ═══════════════════════════════════════════════════════════════════════
    print(f"\n{B}{C}{'╔' + '═' * (W-2) + '╗'}{X}")
    print(f"{B}{C}║{'CUADRO DE MANDO — KUBERNETES':^{W-2}}║{X}")
    print(f"{B}{C}{'╚' + '═' * (W-2) + '╝'}{X}")

    # ═══════════════════════════════════════════════════════════════════════
    # PRE-FLIGHT (si existe)
    # ═══════════════════════════════════════════════════════════════════════
    if preflight_results:
        print(f"\n  {B}🖥️  ESTADO DE CLÚSTERES{X}")
        print(f"  {'─' * (W-4)}")
        for cluster_name, status_str in preflight_results.items():
            # Detectar el estado desde el texto ANSI
            if "OK" in status_str:
                icon = f"{G}●{X}"
            elif "FORBIDDEN" in status_str:
                icon = f"{Y}●{X}"
            else:
                icon = f"{R}●{X}"
            # Limpiar ANSI para mostrar limpio
            clean = status_str
            for code in ["\033[1;32m", "\033[1;33m", "\033[1;31m", "\033[0m"]:
                clean = clean.replace(code, "")
            print(f"    {icon} {cluster_name:<50} {clean}")

    # ═══════════════════════════════════════════════════════════════════════
    # ESTADÍSTICAS GENERALES
    # ═══════════════════════════════════════════════════════════════════════
    total_secrets = len(k8s_summary_logs)
    secrets_found = sum(1 for e in k8s_summary_logs if e.get("real"))
    secrets_not_found = total_secrets - secrets_found
    autofixed = sum(1 for e in k8s_summary_logs if e.get("autofix"))

    total_certs = sum(len(e.get("certs", [])) for e in k8s_summary_logs)
    certs_activos = sum(
        1 for e in k8s_summary_logs for c in e.get("certs", []) if c["estado"] == "ACTIVO"
    )
    certs_vencidos = sum(
        1 for e in k8s_summary_logs for c in e.get("certs", []) if c["estado"] == "VENCIDO"
    )

    passwords_found = sum(1 for e in k8s_summary_logs if e.get("password_source"))
    passwords_needed = sum(1 for e in k8s_summary_logs if e.get("tipo") == "JKS")

    print(f"\n  {B}📊 RESUMEN GENERAL{X}")
    print(f"  {'─' * (W-4)}")
    print(f"    Secretos buscados  : {total_secrets}")
    print(f"    Secretos encontrados: {_bar(secrets_found, total_secrets)}")
    if autofixed:
        print(f"    Auto-corregidos    : {Y}{autofixed}{X} (portal tenía cluster/ns incorrecto)")
    if secrets_not_found:
        print(f"    No encontrados     : {R}{secrets_not_found}{X}")
    print()
    print(f"    Certificados leídos: {_bar(total_certs, total_certs)}")
    if total_certs > 0:
        print(f"    ├─ Activos         : {G}{certs_activos}{X}")
        print(f"    └─ Vencidos        : {R}{certs_vencidos}{X}")
    if passwords_needed > 0:
        print(f"    Passwords JKS      : {_bar(passwords_found, passwords_needed)}")

    # ═══════════════════════════════════════════════════════════════════════
    # DETALLE POR SECRETO
    # ═══════════════════════════════════════════════════════════════════════
    print(f"\n  {B}🔐 DETALLE POR SECRETO{X}")
    print(f"  {'─' * (W-4)}")

    for idx, entry in enumerate(k8s_summary_logs, 1):
        portal = entry["portal"]
        real = entry.get("real")
        tipo = entry.get("tipo", "—")
        certs = entry.get("certs", [])
        autofix = entry.get("autofix", False)
        pwd_source = entry.get("password_source")

        # Cabecera del secreto
        if real:
            status_bg = f"{G}✓ ENCONTRADO{X}" if not autofix else f"{Y}✓ AUTO-CORREGIDO{X}"
        else:
            status_bg = f"{R}✗ NO ENCONTRADO{X}"

        print(f"\n    {B}┌─ [{idx}] {status_bg}")
        print(f"    │{X}  Portal dice  : {D}{portal}{X}")

        if real:
            if autofix:
                print(f"    {B}│{X}  {Y}Ubicación real: {real} ← ¡auto-corregido!{X}")
            else:
                print(f"    {B}│{X}  Ubicación real: {real}")
            print(f"    {B}│{X}  Tipo          : {B}{tipo}{X}")

            # Password (solo para JKS)
            if tipo == "JKS":
                if pwd_source and "incorrecta" in pwd_source:
                    print(f"    {B}│{X}  Password      : {Y}⚠ Encontrada pero incorrecta{X}")
                elif pwd_source:
                    print(f"    {B}│{X}  Password      : {G}✓{X} {pwd_source}")
                else:
                    print(f"    {B}│{X}  Password      : {R}✗ No encontrada{X}")

            # Certificados
            if certs:
                print(f"    {B}│{X}  Certificados  : {len(certs)}")
                for c in certs:
                    icon = _status_icon(c["estado"])
                    cn = c.get("cn", "—")
                    vence = c.get("vence", "—")
                    alias = c.get("alias", "")
                    # No mostrar el alias marcador de password incorrecta
                    if alias and alias != "⚠ password incorrecta":
                        alias_str = f" ({D}alias: {alias}{X})"
                    else:
                        alias_str = ""
                    print(f"    {B}│{X}    {icon}  {cn}{alias_str}")
                    print(f"    {B}│{X}             Vence: {vence}")
            else:
                print(f"    {B}│{X}  Certificados  : {Y}0 — no se pudieron extraer{X}")
        else:
            print(f"    {B}│{X}  {R}Secreto no existe en ningún cluster configurado{X}")

        print(f"    {B}└{'─' * 60}{X}")

    # ═══════════════════════════════════════════════════════════════════════
    # PIE DEL DASHBOARD
    # ═══════════════════════════════════════════════════════════════════════
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n  {D}Generado: {now}{X}")
    print(f"{B}{C}{'═' * W}{X}\n")
