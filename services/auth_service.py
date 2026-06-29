"""
services/auth_service.py
─────────────────────────
Servicio de autenticación: detecta y maneja el login de Microsoft Entra ID.

Modos de operación:
  1. Auto-login  → si BHD_USERNAME y BHD_PASSWORD están en .env, rellena el
                   formulario de Microsoft automáticamente.
  2. Manual      → si no hay credenciales, espera a que el usuario haga login.
"""

import asyncio
from playwright.async_api import Page, TimeoutError as PlaywrightTimeout  # type: ignore[import-untyped]
from config.settings import LOGIN_WAIT, BHD_USERNAME, BHD_PASSWORD


class AuthService:
    def __init__(self, page: Page):
        self.page = page

    def needs_login(self) -> bool:
        """Retorna True si la URL actual es la pantalla de login de Microsoft."""
        return (
            "login.microsoftonline.com" in self.page.url
            or "microsoftonline" in self.page.url
        )

    def has_credentials(self) -> bool:
        """Retorna True si hay credenciales configuradas en .env."""
        return bool(BHD_USERNAME and BHD_PASSWORD)

    async def login(self) -> None:
        """
        Punto de entrada principal.
        - Si hay credenciales en .env → intenta auto-login.
        - Si no hay credenciales       → espera login manual del usuario.
        """
        if self.has_credentials():
            print("  [Auth] Credenciales encontradas — iniciando auto-login...")
            await self._auto_login()
        else:
            print("  [Auth] Sin credenciales en .env — esperando login manual...")
            await self.wait_for_login()

    async def _auto_login(self) -> None:
        """
        Rellena automáticamente el formulario de login de Microsoft Entra ID.
        Flujo estándar:
          1. Escribe el usuario → click Siguiente
          2. Escribe la contraseña → click Iniciar sesión
          3. Maneja el prompt "¿Mantener sesión iniciada?" si aparece
          4. Espera MFA con countdown visual → auto-continúa al aprobar
        """
        try:
            # ── Paso 1: campo de usuario ───────────────────────────────────────
            await self.page.wait_for_selector("input[type='email']", timeout=15_000)
            await self.page.fill("input[type='email']", BHD_USERNAME)
            await self.page.click("input[type='submit']")
            print(f"  [Auth] Usuario ingresado: {BHD_USERNAME}")

            # ── Paso 2: campo de contraseña ────────────────────────────────────
            await self.page.wait_for_selector("input[type='password']", timeout=15_000)
            await self.page.fill("input[type='password']", BHD_PASSWORD)
            await self.page.click("input[type='submit']")
            print("  [Auth] Contraseña ingresada")

            # ── Paso 3: prompt "¿Mantener sesión?" (opcional) ──────────────────
            try:
                await self.page.wait_for_selector("#KmsiCheckboxField", timeout=5_000)
                await self.page.click("input[type='submit']")   # "Sí" / "Yes"
                print("  [Auth] Prompt de sesión confirmado.")
            except PlaywrightTimeout:
                pass  # El prompt no apareció — continuar

            # ── Paso 4: MFA — countdown visible, auto-continúa al aprobar ──────
            await self.wait_for_login()

        except PlaywrightTimeout as e:
            raise TimeoutError(
                f"[Auth] Fallo en auto-login: {e}\n"
                "Verificá que las credenciales en .env sean correctas."
            )

    async def wait_for_login(self) -> None:
        """
        Espera a que la URL cambie al portal BHD con countdown visible.
        Detecta MFA inmediatamente y continúa sin demora.
        """
        Y = "\033[93m"; G = "\033[92m"; R = "\033[91m"; X = "\033[0m"; B = "\033[1m"

        # Si ya estamos en el portal, continuar de una vez
        if "devops.cfbhd.com" in self.page.url:
            print(f"  [Auth] {G}✓{X} Sesión activa — continuando.")
            return

        print(f"  [Auth] ⏳ Esperando MFA — aprueba en tu celular ({LOGIN_WAIT}s)...")

        loop = asyncio.get_event_loop()
        deadline = loop.time() + LOGIN_WAIT

        while loop.time() < deadline:
            # Verificar si ya pasó al portal
            if "devops.cfbhd.com" in self.page.url:
                print(f"\r  [Auth] {G}{B}✓ MFA aprobado — sesión activa.{X}                    ")
                return

            remaining = int(deadline - loop.time())
            print(f"\r  [Auth] {Y}⏳ MFA pendiente... {remaining:3d}s restantes{X}   ", end="", flush=True)
            await asyncio.sleep(2)

        # Timeout
        print(f"\r  [Auth] {R}✗ Timeout MFA ({LOGIN_WAIT}s){X}                              ")
        raise TimeoutError(
            f"[Auth] Tiempo agotado ({LOGIN_WAIT}s). No se completó el login/MFA."
        )

