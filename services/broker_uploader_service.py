"""
services/broker_uploader_service.py
──────────────────────────────────────
Sube un certificado nuevo a un archivo JKS en el tab Brokers del portal.

Flujo en el portal:
  1. Clic en botón 👁️ de la fila JKS → abre #detailsModal
  2. Clic en "Agregar nuevo certificado" → abre #addCertInDetailModal
  3. Llenar formulario:
       - Usuario mqsi (MQSI_USERNAME del .env)
       - Contraseña mqsi (MQSI_PASSWORD del .env)
       - Alias del certificado
       - Correo electrónico interesados
       - Archivo .crt / .cert (upload)
  4. Clic en "Agregar Certificado" → esperar confirmación
"""

import os
from playwright.async_api import Page, TimeoutError as PlaywrightTimeout  # type: ignore[import-untyped]
from config.settings import CERT_EMAIL
from models.broker_cert import BrokerUploadJob, BrokerRow
from services.server_credentials_service import ServerCredentialsService

# ─── Selectores ────────────────────────────────────────────────────────────────
MODAL_DETAILS         = "#detailsModal"
BTN_ADD_CERT_DETAIL   = f"{MODAL_DETAILS} .modal-body button.btn-success"
MODAL_ADD_CERT        = "#addCertInDetailModal"
INPUT_USERNAME        = f"{MODAL_ADD_CERT} #usernameInput"
INPUT_PASSWORD        = f"{MODAL_ADD_CERT} #AuthKeyInput"
INPUT_ALIAS           = f"{MODAL_ADD_CERT} #aliasInputDetail"
INPUT_EMAIL           = f"{MODAL_ADD_CERT} #aliasInput"      # campo email reutiliza id aliasInput
INPUT_CERT_FILE       = f"{MODAL_ADD_CERT} #certFileInputDetail"
BTN_SUBMIT            = f"{MODAL_ADD_CERT} .modal-footer button.btn-primary"
SUBMIT_TIMEOUT        = 30_000


class BrokerUploaderService:
    def __init__(self, page: Page, credentials: ServerCredentialsService | None = None):
        self.page = page
        self._creds = credentials or ServerCredentialsService()

    async def upload(self, job: BrokerUploadJob) -> bool:
        """
        Ejecuta el flujo completo para agregar un certificado al JKS indicado.
        Retorna True si fue exitoso.
        """
        row = job.broker_row
        print(
            f"  [BrokerUpload] → '{row.jks_name}' [{row.ambiente}] "
            f"| alias={job.alias} | cert={os.path.basename(job.cert_file_path)}"
        )

        if not os.path.exists(job.cert_file_path):
            print(f"  [BrokerUpload] ✗ Archivo no encontrado: {job.cert_file_path}")
            return False

        try:
            # 1. Abrir el modal de detalle (eye button ya debería estar abierto
            #    o navegamos directamente)
            await self._ensure_details_modal_open(row)

            # 2. Clic en "Agregar nuevo certificado"
            await self._open_add_cert_modal()

            # 3. Rellenar formulario
            await self._fill_form(job)

            # 4. Submit y confirmar
            success = await self._submit_and_confirm()

            if success:
                print(f"  [BrokerUpload] ✓ Certificado agregado a '{row.jks_name}'")
            else:
                print(f"  [BrokerUpload] ✗ No se confirmó el upload para '{row.jks_name}'")

            return success

        except Exception as exc:
            print(f"  [BrokerUpload] ✗ Error: {exc}")
            # Intentar cerrar modales abiertos
            await self._close_all_modals()
            return False

    async def upload_batch(self, jobs: list[BrokerUploadJob]) -> dict:
        """
        Ejecuta múltiples jobs de upload.
        Retorna {"ok": [...], "fail": [...]}
        """
        print(f"\n[BrokerUpload] {len(jobs)} certificados para subir en Brokers...")
        ok: list[str] = []
        fail: list[str] = []

        for job in jobs:
            label = f"{job.broker_row.jks_name}/{job.alias}"
            success = await self.upload(job)
            (ok if success else fail).append(label)

        print(f"  [BrokerUpload] ✓ Exitosos: {len(ok)} | ✗ Fallidos: {len(fail)}")
        return {"ok": ok, "fail": fail}

    # ── Helpers privados ───────────────────────────────────────────────────────

    async def _ensure_details_modal_open(self, row: BrokerRow) -> None:
        """
        Verifica si el modal de detalle ya está abierto para esta fila.
        Si no, busca y clica el botón 👁️ correspondiente.
        """
        # Verificar si el modal ya está abierto con el título correcto
        modal_visible = await self.page.query_selector(f"{MODAL_DETAILS}.show")
        if modal_visible:
            title = await modal_visible.query_selector(".modal-title")
            if title:
                text = await title.inner_text()
                if row.jks_name in text and row.ambiente in text:
                    return  # Ya está el modal correcto abierto

        # Cerrar cualquier modal abierto
        await self._close_all_modals()

        # Buscar y clicar el 👁️ de esta fila
        btn = await self._find_eye_button(row)
        if not btn:
            raise RuntimeError(
                f"No se encontró botón 👁️ para '{row.jks_name}' [{row.ambiente}]"
            )
        await btn.click()
        await self.page.wait_for_selector(f"{MODAL_DETAILS}.show", timeout=8_000)

    async def _find_eye_button(self, row: BrokerRow):
        """Localiza el botón 👁️ de la fila que coincide con jks_name y ambiente."""
        pane = await self.page.query_selector("#librariesView")
        if not pane:
            return None
        table = await pane.query_selector("table")
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
                return await cells[0].query_selector("button")
        return None

    async def _open_add_cert_modal(self) -> None:
        """Clica 'Agregar nuevo certificado' dentro del modal de detalle."""
        btn = await self.page.query_selector(BTN_ADD_CERT_DETAIL)
        if not btn:
            raise RuntimeError("No se encontró el botón 'Agregar nuevo certificado'")
        await btn.click()
        await self.page.wait_for_selector(f"{MODAL_ADD_CERT}.show", timeout=8_000)

    async def _fill_form(self, job: BrokerUploadJob) -> None:
        """Rellena todos los campos del modal #addCertInDetailModal."""
        # Credenciales mqsi: DEBEN venir de servers.xlsx
        username, password = self._creds.get(
            jks_name=job.broker_row.jks_name,
            ambiente=job.broker_row.ambiente,
        )
        if not username or not password:
            raise RuntimeError(
                f"Sin credenciales para '{job.broker_row.jks_name}' "
                f"[{job.broker_row.ambiente}] — agrega la fila en servers.xlsx"
            )
        await self.page.fill(INPUT_USERNAME, username)
        await self.page.fill(INPUT_PASSWORD, password)

        # Alias del certificado
        await self.page.fill(INPUT_ALIAS, job.alias)

        # Correo interesados (usa job.email o el del .env como fallback)
        email = job.email or CERT_EMAIL
        await self.page.fill(INPUT_EMAIL, email)

        # Subir el archivo .crt / .cert
        file_input = await self.page.query_selector(INPUT_CERT_FILE)
        if not file_input:
            raise RuntimeError("No se encontró el input de archivo en el modal")
        await file_input.set_input_files(job.cert_file_path)

    async def _submit_and_confirm(self) -> bool:
        """Clica 'Agregar Certificado' y espera confirmación por SweetAlert2 o cierre."""
        await self.page.click(BTN_SUBMIT)

        # Caso 1: SweetAlert2
        try:
            await self.page.wait_for_selector(".swal2-popup", timeout=SUBMIT_TIMEOUT)
            btn = await self.page.query_selector(".swal2-confirm, .swal2-ok")
            if btn:
                await btn.click()
            return True
        except PlaywrightTimeout:
            pass

        # Caso 2: modal se cierra solo
        try:
            await self.page.wait_for_selector(
                f"{MODAL_ADD_CERT}:not(.show)", timeout=SUBMIT_TIMEOUT
            )
            return True
        except PlaywrightTimeout:
            return False

    async def _close_all_modals(self) -> None:
        """Cierra cualquier modal abierto intentando clicar los botones de cierre."""
        for selector in [
            f"{MODAL_ADD_CERT} .btn-close",
            f"{MODAL_ADD_CERT} .btn-secondary",
            f"{MODAL_DETAILS} .btn-close",
            f"{MODAL_DETAILS} .btn-secondary",
        ]:
            try:
                btn = await self.page.query_selector(selector)
                if btn:
                    visible = await btn.is_visible()
                    if visible:
                        await btn.click()
            except Exception:
                pass
