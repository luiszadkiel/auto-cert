"""
controllers/cert_controller.py
────────────────────────────────
Controller: orquesta el flujo completo de extracción de certificados.
Conecta servicios → modelos → vista (reporter).
Registra toda la actividad en el log de auditoría (output/audit/).
"""

import os
from config.settings import TARGET_URL, OUTPUT_DIR
from models.certificate import Certificate, PageStructure
from services.browser_service import BrowserService
from services.auth_service import AuthService
from services.scraper_service import ScraperService
from services.audit_service import AuditService
from models.audit_log import EventType
from views.reporter import (
    print_banner,
    print_structure_summary,
    print_extraction_summary,
    save_json,
    save_csv,
    build_output_paths,
)


class CertController:
    """
    Controlador principal de la automatización.

    Flujo:
      1. Abrir browser
      2. Navegar al portal
      3. Autenticar (auto con .env o manual si no hay credenciales)
      4. Analizar estructura DOM → PageStructure
      5. Extraer datos de tabla → list[Certificate]
      6. Persistir resultados (JSON + CSV)
      7. Registrar toda la actividad en auditoría
    """

    async def run(self) -> None:
        print("\n[*] Iniciando automatización: Inventario de Certificados BHD\n")

        audit = AuditService(mode="certs")
        total_certs = 0
        paths: dict = {}

        try:
            async with BrowserService() as browser:
                page = browser.page

                # ── 1. Navegar ─────────────────────────────────────────────────
                print(f"→ Navegando a: {TARGET_URL}")
                await browser.goto(TARGET_URL)

                # ── 2. Autenticación ───────────────────────────────────────────
                auth = AuthService(page)
                if auth.needs_login():
                    if auth.has_credentials():
                        audit.log(EventType.AUTH_AUTO, "Auto-login iniciado con credenciales del .env")
                    else:
                        audit.log(EventType.AUTH_MANUAL, "Esperando login manual del usuario")
                    await auth.login()
                    audit.log(EventType.AUTH_OK, "Login completado — sesión activa")
                else:
                    audit.log(EventType.AUTH_OK, "Sesión ya activa en el portal")
                    print("  [i] Sesión ya activa o sin redirección a login.")

                # Garantizar URL correcta post-login
                if "inventariodecertificados" not in page.url:
                    print("→ Navegando a la página de inventario...")
                    await browser.goto(TARGET_URL)

                await browser.wait_for_network_idle()
                title = await page.title()
                print(f"\n✓ Página cargada: '{title}'")
                print(f"  URL: {page.url}\n")

                # ── 3. Screenshot inicial ──────────────────────────────────────
                paths = build_output_paths(OUTPUT_DIR, "certificados")
                screenshot_path = os.path.join(OUTPUT_DIR, "screenshot_inventario.png")
                await browser.screenshot(screenshot_path)

                # ── 4. Análisis de estructura ──────────────────────────────────
                audit.log(EventType.SCRAPE_START, "Analizando estructura DOM del portal")
                scraper = ScraperService(page)
                raw_structure = await scraper.analyze_structure()
                structure = PageStructure(
                    **{k: raw_structure[k] for k in PageStructure.__dataclass_fields__}
                )

                print_structure_summary(raw_structure)
                save_json(structure.to_dict(), paths["structure"])
                audit.log(
                    EventType.FILE_SAVED,
                    "Estructura DOM guardada",
                    path=paths["structure"],
                    tables_found=len(structure.tables),
                    pagination=structure.pagination,
                )

                # ── 5. Extracción de datos ─────────────────────────────────────
                if structure.tables:
                    audit.log(
                        EventType.SCRAPE_START,
                        f"Extrayendo tabla #{0} del portal",
                        table_index=0,
                        headers=structure.tables[0].get("headers", []),
                    )
                    raw_rows = await scraper.extract_table(table_index=0)
                    certificates = [Certificate.from_row(row) for row in raw_rows]
                    total_certs = len(certificates)

                    if certificates:
                        data = [c.to_dict() for c in certificates]
                        save_json(data, paths["json"])
                        save_csv(data, paths["csv"])
                        print_extraction_summary(data)

                        audit.log(
                            EventType.SCRAPE_OK,
                            f"{total_certs} certificados extraídos de la tabla",
                            total=total_certs,
                            columns=list(data[0].keys()) if data else [],
                        )
                        audit.log(EventType.FILE_SAVED, "JSON de certificados guardado", path=paths["json"])
                        audit.log(EventType.FILE_SAVED, "CSV de certificados guardado", path=paths["csv"])
                    else:
                        print("\n  [!] Tabla encontrada pero sin datos.")
                        await browser.screenshot(
                            os.path.join(OUTPUT_DIR, "screenshot_tabla_vacia.png")
                        )
                        audit.log(EventType.SCRAPE_EMPTY, "Tabla encontrada pero sin filas de datos")
                else:
                    print("\n  [!] No se detectaron tablas HTML.")
                    await browser.screenshot(
                        os.path.join(OUTPUT_DIR, "screenshot_sin_tabla.png")
                    )
                    audit.log(
                        EventType.SCRAPE_EMPTY,
                        "No se detectaron tablas HTML en la página",
                        hint="Puede ser un componente JS custom (React/Angular)",
                    )

            # ── 6. Cerrar auditoría ────────────────────────────────────────────
            audit.close(
                status="OK",
                total_certs_extracted=total_certs,
                json_path=paths.get("json", ""),
                csv_path=paths.get("csv", ""),
            )

        except Exception as exc:
            audit.log(EventType.ERROR, f"Error inesperado: {exc}", error=str(exc))
            audit.close(status="ERROR", error=str(exc))
            raise

        print_banner(
            f"✓ Automatización completada\n  Archivos en: {os.path.abspath(OUTPUT_DIR)}"
        )
