"""
services/portal_k8s_scraper_service.py
────────────────────────────────────────
Scraper especializado en la tabla Kubernetes del portal.

Maneja:
  - Clic en el tab "Kubernetes"
  - Paginación completa del componente bh-datatable (477+ entradas)
  - Retorna lista de PortalK8sCert
"""

from playwright.async_api import Page, TimeoutError as PlaywrightTimeout  # type: ignore[import-untyped]
from models.k8s_cert import PortalK8sCert

# ─── Selectores del portal ─────────────────────────────────────────────────────
TAB_KUBERNETES   = "#parentLibrariesView-tab"       # botón del tab K8s
TABLE_CONTAINER  = "#parentLibrariesView"           # pane activo K8s
NEXT_BTN         = ".bh-pagination .next-page:not(.disabled)"
PAGE_SIZE_SELECT = ".bh-pagesize"                   # dropdown de tamaño de página


class PortalK8sScraperService:
    def __init__(self, page: Page):
        self.page = page

    async def scrape_all(self) -> list[PortalK8sCert]:
        """
        Navega al tab Kubernetes, pone pageSize=100 y extrae todos los registros
        paginando hasta el final.
        Retorna lista de PortalK8sCert.
        """
        print("\n[Portal K8s] Abriendo tab Kubernetes...")
        await self._activate_k8s_tab()

        # Aumentar page-size a 100 para reducir iteraciones
        await self._set_page_size(100)

        all_certs: list[PortalK8sCert] = []
        page_num = 1

        while True:
            rows = await self._extract_current_page()
            all_certs.extend(rows)
            print(f"  Página {page_num}: {len(rows)} registros | Total: {len(all_certs)}")

            if not await self._go_next_page():
                break
            page_num += 1

        print(f"  [Portal K8s] ✓ Total extraído: {len(all_certs)} certificados K8s")
        return all_certs

    # ── Helpers privados ───────────────────────────────────────────────────────

    async def _activate_k8s_tab(self) -> None:
        """Hace clic en el tab 'Kubernetes' y espera a que esté visible."""
        try:
            await self.page.click(TAB_KUBERNETES)
            await self.page.wait_for_selector(
                f"{TABLE_CONTAINER}.show.active",
                timeout=10_000,
            )
        except PlaywrightTimeout:
            # El tab ya puede estar activo; continuamos
            pass

    async def _set_page_size(self, size: int = 100) -> None:
        """Cambia el dropdown de pageSize dentro del tab K8s."""
        try:
            select = await self.page.query_selector(
                f"{TABLE_CONTAINER} {PAGE_SIZE_SELECT}"
            )
            if select:
                await select.select_option(str(size))
                await self.page.wait_for_load_state("networkidle", timeout=5_000)
        except Exception:
            pass  # Si falla el pageSize, igual continuamos con el default

    async def _extract_current_page(self) -> list[PortalK8sCert]:
        """
        Lee todas las filas visibles de la tabla del tab Kubernetes.
        Retorna lista de PortalK8sCert construidos desde cada fila.
        """
        table = await self.page.query_selector(f"{TABLE_CONTAINER} table")
        if not table:
            return []

        # Headers
        headers = [
            (await th.inner_text()).strip()
            for th in await table.query_selector_all("th")
        ]

        rows = await table.query_selector_all("tbody tr")
        certs: list[PortalK8sCert] = []

        for row in rows:
            cells = await row.query_selector_all("td")
            if not cells:
                continue
            values = [(await cell.inner_text()).strip() for cell in cells]
            if len(values) == len(headers):
                raw = dict(zip(headers, values))
                certs.append(PortalK8sCert.from_row(raw))

        return certs

    async def _go_next_page(self) -> bool:
        """
        Intenta hacer clic en 'Next page'.
        Retorna True si navegó, False si ya era la última página.
        """
        try:
            btn = await self.page.query_selector(
                f"{TABLE_CONTAINER} {NEXT_BTN}"
            )
            if not btn:
                return False
            is_disabled = await btn.get_attribute("class") or ""
            if "disabled" in is_disabled:
                return False
            await btn.click()
            await self.page.wait_for_load_state("networkidle", timeout=8_000)
            return True
        except Exception:
            return False
