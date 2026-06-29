"""
services/scraper_service.py
────────────────────────────
Servicio de scraping puro: analiza el DOM y extrae datos de tablas.
Recibe un Page de Playwright y devuelve estructuras Python (dicts/lists).
No sabe nada de archivos, reportes ni configuraciones de entorno.
"""

from playwright.async_api import Page


class ScraperService:
    def __init__(self, page: Page):
        self.page = page

    # ── Análisis de estructura ─────────────────────────────────────────────────

    async def analyze_structure(self) -> dict:
        """
        Analiza el DOM de la página actual y retorna un dict con metadatos:
        tablas, botones, inputs, selects y si hay paginación.
        """
        print("[1/3] Analizando estructura de la página...")
        structure = {
            "url": self.page.url,
            "title": await self.page.title(),
            "tables": [],
            "buttons": [],
            "inputs": [],
            "selects": [],
            "pagination": False,
        }

        # Tablas
        tables = await self.page.query_selector_all("table")
        for i, table in enumerate(tables):
            headers = [
                (await th.inner_text()).strip()
                for th in await table.query_selector_all("th")
            ]
            rows = await table.query_selector_all("tr")
            structure["tables"].append({
                "index": i,
                "headers": headers,
                "row_count": len(rows),
            })
            print(f"  Tabla #{i}: {len(rows)} filas | Cols: {headers}")

        # Botones
        buttons = await self.page.query_selector_all("button")
        for btn in buttons:
            txt = (await btn.inner_text()).strip()
            btn_id = await btn.get_attribute("id") or ""
            if txt:
                structure["buttons"].append({"text": txt, "id": btn_id})

        # Inputs
        inputs = await self.page.query_selector_all("input")
        for inp in inputs:
            structure["inputs"].append({
                "type":        await inp.get_attribute("type") or "text",
                "id":          await inp.get_attribute("id") or "",
                "name":        await inp.get_attribute("name") or "",
                "placeholder": await inp.get_attribute("placeholder") or "",
            })

        # Selects
        selects = await self.page.query_selector_all("select")
        for sel in selects:
            options = [
                (await opt.inner_text()).strip()
                for opt in await sel.query_selector_all("option")
            ]
            structure["selects"].append({
                "id":      await sel.get_attribute("id") or "",
                "options": options,
            })

        # Paginación heurística
        pag_hints = ["pagination", "pagina", "page", "next", "anterior", "siguiente"]
        body_text = (await self.page.inner_text("body")).lower()
        structure["pagination"] = any(h in body_text for h in pag_hints)

        return structure

    # ── Extracción de datos ────────────────────────────────────────────────────

    async def extract_table(self, table_index: int = 0) -> list[dict]:
        """
        Extrae TODOS los registros de la tabla (maneja paginación).
        Retorna lista de dicts [{columna: valor, ...}].
        """
        print(f"\n[2/3] Extrayendo datos de tabla #{table_index}...")
        all_data: list[dict] = []
        page_num = 1

        while True:
            tables = await self.page.query_selector_all("table")
            if not tables or table_index >= len(tables):
                print("  [!] No se encontró la tabla en esta página.")
                break

            table = tables[table_index]

            # Headers
            headers = [
                (await th.inner_text()).strip()
                for th in await table.query_selector_all("th")
            ]
            if not headers:
                first_row = await table.query_selector("tr")
                if first_row:
                    headers = [
                        (await td.inner_text()).strip()
                        for td in await first_row.query_selector_all("td")
                    ]

            # Filas
            rows = await table.query_selector_all("tr")
            page_data: list[dict] = []
            for row in rows:
                cells = await row.query_selector_all("td")
                if not cells:
                    continue
                values = [(await cell.inner_text()).strip() for cell in cells]
                if headers and len(values) == len(headers):
                    page_data.append(dict(zip(headers, values)))
                elif values:
                    page_data.append({"_col_" + str(j): v for j, v in enumerate(values)})

            all_data.extend(page_data)
            print(f"  Página {page_num}: {len(page_data)} registros | Total: {len(all_data)}")

            # Siguiente página
            next_btn = await self._find_next_button()
            if not next_btn:
                print(f"  ✓ Sin más páginas — total: {len(all_data)} registros")
                break

            await next_btn.click()
            await self.page.wait_for_load_state("networkidle", timeout=10_000)
            page_num += 1

        return all_data

    # ── Helpers privados ───────────────────────────────────────────────────────

    async def _find_next_button(self):
        """Retorna el botón/enlace 'Siguiente' si existe y no está deshabilitado."""
        selectors = [
            "button:has-text('Siguiente')",
            "button:has-text('Next')",
            "a:has-text('Siguiente')",
            "a:has-text('Next')",
            "[aria-label='Next page']",
            ".pagination .next:not(.disabled)",
        ]
        for selector in selectors:
            try:
                btn = await self.page.query_selector(selector)
                if btn:
                    if await btn.get_attribute("disabled"):
                        return None
                    return btn
            except Exception:
                pass
        return None
