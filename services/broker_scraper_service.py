"""
services/broker_scraper_service.py
────────────────────────────────────
Scraper del tab Brokers del portal devops.cfbhd.com.

Flujo:
  1. Activar tab "Brokers"
  2. Extraer todas las filas de la tabla (paginación completa) → list[BrokerRow]
  3. Para cada fila con certs vencidos:
       a. Clic en botón 👁️ de la fila
       b. Esperar modal #detailsModal
       c. Extraer detalle de cada cert → list[BrokerCertDetail]
       d. Cerrar modal
  4. Retornar mapa: BrokerRow → list[BrokerCertDetail]
"""

import asyncio
from playwright.async_api import Page, TimeoutError as PlaywrightTimeout  # type: ignore[import-untyped]
from models.broker_cert import BrokerRow, BrokerCertDetail

# ─── Selectores ────────────────────────────────────────────────────────────────
TAB_BROKERS      = "#librariesView-tab"
PANE_BROKERS     = "#librariesView"
MODAL_DETAILS    = "#detailsModal"
MODAL_CLOSE_BTN  = f"{MODAL_DETAILS} .btn-close, {MODAL_DETAILS} .btn-secondary"
NEXT_BTN         = f"{PANE_BROKERS} .bh-pagination .next-page:not(.disabled)"
PAGE_SIZE_SELECT = f"{PANE_BROKERS} .bh-pagesize"


class BrokerScraperService:
    def __init__(self, page: Page):
        self.page = page

    async def scrape_all(self) -> dict[BrokerRow, list[BrokerCertDetail]]:
        """
        Extrae toda la tabla Brokers y, para cada JKS con certs vencidos,
        abre el modal y lee el detalle.

        Retorna: {BrokerRow: [BrokerCertDetail, ...]}
        """
        print("\n[Broker] Activando tab Brokers...")
        await self._activate_brokers_tab()
        await self._set_page_size(100)

        all_rows: list[BrokerRow] = []
        page_num = 1

        # ── Extraer todas las filas de la tabla principal ──────────────────────
        while True:
            rows = await self._extract_table_rows()
            all_rows.extend(rows)
            print(f"  Página {page_num}: {len(rows)} filas | Total: {len(all_rows)}")
            if not await self._go_next_page():
                break
            page_num += 1

        print(f"  [Broker] ✓ {len(all_rows)} archivos JKS en tabla principal")

        # ── Para cada fila con vencidos, abrir modal y leer detalle ───────────
        result: dict[BrokerRow, list[BrokerCertDetail]] = {}

        rows_with_issues = [r for r in all_rows if r.needs_attention()]
        print(f"  [Broker] {len(rows_with_issues)} JKS con certs vencidos — leyendo detalles...")

        for row in rows_with_issues:
            details = await self._read_details(row)
            result[row] = details

        return result

    async def scrape_table_only(self) -> list[BrokerRow]:
        """Extrae solo la tabla principal sin abrir modales."""
        await self._activate_brokers_tab()
        await self._set_page_size(100)
        all_rows: list[BrokerRow] = []
        while True:
            rows = await self._extract_table_rows()
            all_rows.extend(rows)
            if not await self._go_next_page():
                break
        return all_rows

    # ── Privados ───────────────────────────────────────────────────────────────

    async def _activate_brokers_tab(self) -> None:
        try:
            tab = await self.page.query_selector(TAB_BROKERS)
            if tab:
                classes = await tab.get_attribute("class") or ""
                if "active" not in classes:
                    await tab.click()
                    await self.page.wait_for_selector(
                        f"{PANE_BROKERS}.show.active", timeout=8_000
                    )
        except PlaywrightTimeout:
            pass

    async def _set_page_size(self, size: int = 100) -> None:
        try:
            sel = await self.page.query_selector(PAGE_SIZE_SELECT)
            if sel:
                await sel.select_option(str(size))
                await self.page.wait_for_load_state("networkidle", timeout=5_000)
        except Exception:
            pass

    async def _extract_table_rows(self) -> list[BrokerRow]:
        table = await self.page.query_selector(f"{PANE_BROKERS} table")
        if not table:
            return []

        headers = [
            (await th.inner_text()).strip()
            for th in await table.query_selector_all("th")
        ]
        rows_el = await table.query_selector_all("tbody tr")
        rows: list[BrokerRow] = []

        for row_el in rows_el:
            cells = await row_el.query_selector_all("td")
            if not cells:
                continue

            # La primera columna es el botón de acciones — la saltamos
            # Headers: Acciones | Nombre Archivo JKS | Ambiente | Cantidad | Estado
            values = [(await c.inner_text()).strip() for c in cells]

            if len(headers) >= 5 and len(values) >= 4:
                # Mapear desde la columna 1 en adelante (0=Acciones)
                raw = dict(zip(headers[1:], values[1:]))
                rows.append(BrokerRow.from_row(raw))

        return rows

    async def _go_next_page(self) -> bool:
        try:
            btn = await self.page.query_selector(NEXT_BTN)
            if not btn:
                return False
            cls = await btn.get_attribute("class") or ""
            if "disabled" in cls:
                return False
            await btn.click()
            await self.page.wait_for_load_state("networkidle", timeout=8_000)
            return True
        except Exception:
            return False

    async def _read_details(self, row: BrokerRow) -> list[BrokerCertDetail]:
        """
        Encuentra el botón 👁️ correspondiente a esta fila, lo clica,
        lee el modal de detalles y retorna los certs encontrados.
        """
        print(f"    → Abriendo detalle: {row.jks_name} [{row.ambiente}]")

        # Re-navegar a la página correcta si hace falta para encontrar la fila
        btn = await self._find_eye_button(row)
        if not btn:
            print(f"    ⚠ No se encontró botón 👁️ para '{row.jks_name}' [{row.ambiente}]")
            return []

        try:
            await btn.click()
            await self.page.wait_for_selector(f"{MODAL_DETAILS}.show", timeout=8_000)
        except PlaywrightTimeout:
            print(f"    ⚠ Modal no apareció para '{row.jks_name}'")
            return []

        # Leer la tabla del modal
        details = await self._extract_modal_certs()

        # Cerrar el modal
        await self._close_modal()
        await asyncio.sleep(0.5)

        return details

    async def _find_eye_button(self, row: BrokerRow):
        """
        Busca el botón 👁️ de la fila que coincide con jks_name y ambiente.
        Itera todas las filas visibles para encontrar la correcta.
        """
        table = await self.page.query_selector(f"{PANE_BROKERS} table")
        if not table:
            return None

        rows_el = await table.query_selector_all("tbody tr")
        for row_el in rows_el:
            cells = await row_el.query_selector_all("td")
            if len(cells) < 3:
                continue
            jks_cell = (await cells[1].inner_text()).strip()
            amb_cell = (await cells[2].inner_text()).strip()
            if jks_cell == row.jks_name and amb_cell == row.ambiente:
                btn = await cells[0].query_selector("button")
                return btn
        return None

    async def _extract_modal_certs(self) -> list[BrokerCertDetail]:
        """Lee las filas de la tabla dentro del modal #detailsModal."""
        table = await self.page.query_selector(f"{MODAL_DETAILS} table")
        if not table:
            return []

        rows_el = await table.query_selector_all("tbody tr")
        details: list[BrokerCertDetail] = []

        for row_el in rows_el:
            cells_el = await row_el.query_selector_all("td")
            cells = [(await c.inner_text()).strip() for c in cells_el]
            if len(cells) >= 4:
                details.append(BrokerCertDetail.from_row(cells))

        return details

    async def _close_modal(self) -> None:
        try:
            btn = await self.page.query_selector(MODAL_CLOSE_BTN)
            if btn:
                await btn.click()
            await self.page.wait_for_selector(
                f"{MODAL_DETAILS}:not(.show)", timeout=5_000
            )
        except Exception:
            pass
