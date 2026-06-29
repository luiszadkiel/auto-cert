"""
services/browser_service.py
────────────────────────────
Servicio de bajo nivel: maneja el ciclo de vida del browser Playwright.
No sabe nada de modelos ni de negocio — solo abre/cierra el browser
y expone métodos de navegación básicos.

Soporte de sesión persistente:
  Si STORAGE_STATE_PATH apunta a un archivo existente, se reutilizan
  cookies y localStorage del contexto anterior (evita repetir MFA).
  Después de un login exitoso, llamar save_session() para persistir.
"""

import os
from playwright.async_api import async_playwright, Browser, BrowserContext, Page  # type: ignore[import-untyped]
from config.settings import BROWSER_ARGS, VIEWPORT, HEADLESS, STORAGE_STATE_PATH


class BrowserService:
    def __init__(self, storage_state_path: str = STORAGE_STATE_PATH):
        self._playwright = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self.page: Page | None = None
        self._storage_state_path: str = storage_state_path
        self.session_loaded: bool = False

    async def start(self) -> "BrowserService":
        self._playwright = await async_playwright().start()

        # Si no hay sesión guardada → forzar browser visible para MFA
        has_session = self._storage_state_path and os.path.isfile(self._storage_state_path)
        use_headless = HEADLESS if has_session else False

        if not has_session and HEADLESS:
            print("  [Browser] ⚠ Sin sesión guardada → abriendo browser visible para MFA")

        launch_args = list(BROWSER_ARGS)
        if not use_headless:
            launch_args.append("--auto-open-devtools-for-tabs=false")

        self._browser = await self._playwright.chromium.launch(
            headless=use_headless,
            args=launch_args,
            slow_mo=50 if not use_headless else 0,
        )

        # Reutilizar sesión si el archivo existe
        context_kwargs: dict = {
            "ignore_https_errors": True,
            "viewport": VIEWPORT,
        }
        if has_session:
            context_kwargs["storage_state"] = self._storage_state_path
            self.session_loaded = True

        self._context = await self._browser.new_context(**context_kwargs)
        self.page = await self._context.new_page()

        # Traer ventana al frente si es visible
        if not use_headless:
            await self.page.bring_to_front()

        return self

    async def save_session(self) -> None:
        """Persiste cookies + localStorage del contexto actual a disco."""
        if self._context and self._storage_state_path:
            os.makedirs(os.path.dirname(self._storage_state_path) or ".", exist_ok=True)
            await self._context.storage_state(path=self._storage_state_path)
            print(f"  ✓ Sesión guardada → {self._storage_state_path}")

    async def goto(self, url: str, timeout: int = 60_000) -> None:
        await self.page.goto(url, timeout=timeout, wait_until="domcontentloaded")
        await self.page.wait_for_load_state("domcontentloaded")

    async def wait_for_network_idle(self, timeout: int = 20_000) -> None:
        await self.page.wait_for_load_state("networkidle", timeout=timeout)

    async def screenshot(self, path: str) -> None:
        import os
        os.makedirs(os.path.dirname(path) if os.path.dirname(path) else ".", exist_ok=True)
        await self.page.screenshot(path=path, full_page=True)
        print(f"  ✓ Screenshot → {path}")

    async def stop(self) -> None:
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()

    # ── Context manager support ────────────────────────────────────────────────
    async def __aenter__(self):
        return await self.start()

    async def __aexit__(self, *_):
        await self.stop()
