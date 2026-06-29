"""
services/portal_uploader_service.py
─────────────────────────────────────
Sube/actualiza un certificado K8S en el portal devops.cfbhd.com.

Flujo en el portal:
  1. Clic en "Agregar Certificado K8S"
  2. Seleccionar el cluster en el dropdown
  3. Ingresar Namespace
  4. Ingresar Nombre del secreto
  5. Seleccionar Tipo de secreto (CRT | Opaque)
  6. Clic en "Agregar Certificado"
  7. Esperar confirmación (SweetAlert2 o redireccionamiento)

El portal se encarga de hacer el pull desde el cluster,
por eso no necesitamos enviar el archivo PEM directamente.
"""

from playwright.async_api import Page, TimeoutError as PlaywrightTimeout  # type: ignore[import-untyped]
from models.k8s_cert import CertDiff

# ─── Selectores (basados en el HTML analizado) ─────────────────────────────────
BTN_ADD_K8S        = "button.btn.btn-success"                        # "Agregar Certificado K8S"
MODAL_K8S          = "#addK8SCertificateModal"
CLUSTER_SELECT     = f"{MODAL_K8S} select.form-select"
NAMESPACE_INPUT    = f"{MODAL_K8S} #namespaceInput"
SECRET_INPUT       = f"{MODAL_K8S} #secretNameInput"
SECRET_TYPE_SELECT = f"{MODAL_K8S} select.form-select:last-of-type"
BTN_SUBMIT         = f"{MODAL_K8S} .modal-footer button.btn-primary"

# Tiempo máximo de espera para respuesta del portal tras submit (ms)
SUBMIT_TIMEOUT = 30_000


class PortalUploaderService:
    def __init__(self, page: Page):
        self.page = page

    async def upload(self, diff: CertDiff) -> bool:
        """
        Sube el certificado indicado en el CertDiff al portal.
        Retorna True si el upload fue exitoso, False si hubo error.
        """
        portal = diff.portal_cert
        cluster = diff.cluster_cert

        if cluster is None:
            print(f"  [Upload] ✗ Sin cert del cluster para {portal.secret_name} — omitiendo.")
            return False

        print(
            f"  [Upload] → Subiendo '{portal.secret_name}' "
            f"[{portal.namespace}] a {portal.cluster}..."
        )

        try:
            # 1. Cambiar al tab Kubernetes (si no está activo)
            await self._ensure_k8s_tab()

            # 2. Abrir el modal
            await self._open_modal()

            # 3. Rellenar el formulario
            await self._fill_form(
                cluster_name=portal.cluster,
                namespace=portal.namespace,
                secret_name=portal.secret_name,
                secret_type=cluster.secret_type,
            )

            # 4. Submit y esperar confirmación
            success = await self._submit_and_confirm()

            if success:
                print(f"  [Upload] ✓ '{portal.secret_name}' actualizado en el portal.")
            else:
                print(f"  [Upload] ✗ No se confirmó el upload de '{portal.secret_name}'.")

            return success

        except Exception as exc:
            print(f"  [Upload] ✗ Error al subir '{portal.secret_name}': {exc}")
            return False

    # ── Helpers privados ───────────────────────────────────────────────────────

    async def _ensure_k8s_tab(self) -> None:
        """Activa el tab Kubernetes si no lo está."""
        try:
            tab = await self.page.query_selector("#parentLibrariesView-tab")
            if tab:
                classes = await tab.get_attribute("class") or ""
                if "active" not in classes:
                    await tab.click()
                    await self.page.wait_for_load_state("networkidle", timeout=5_000)
        except Exception:
            pass

    async def _open_modal(self) -> None:
        """Hace clic en 'Agregar Certificado K8S' y espera a que el modal se abra."""
        # El botón está en el tab Kubernetes
        k8s_pane = await self.page.query_selector("#parentLibrariesView")
        if k8s_pane:
            btn = await k8s_pane.query_selector("button.btn-success")
        else:
            btn = None

        if not btn:
            raise RuntimeError("No se encontró el botón 'Agregar Certificado K8S'")

        await btn.click()
        await self.page.wait_for_selector(
            f"{MODAL_K8S}.show",
            timeout=8_000,
        )

    async def _fill_form(
        self,
        cluster_name: str,
        namespace: str,
        secret_name: str,
        secret_type: str,
    ) -> None:
        """Rellena los campos del modal de K8S."""
        # Cluster (select por value o por texto visible)
        try:
            await self.page.select_option(CLUSTER_SELECT, value=cluster_name)
        except Exception:
            await self.page.select_option(CLUSTER_SELECT, label=cluster_name)

        # Namespace
        await self.page.fill(NAMESPACE_INPUT, namespace)

        # Nombre del secreto
        await self.page.fill(SECRET_INPUT, secret_name)

        # Tipo de secreto
        try:
            await self.page.select_option(SECRET_TYPE_SELECT, value=secret_type)
        except Exception:
            # Fallback: seleccionar CRT por defecto
            await self.page.select_option(SECRET_TYPE_SELECT, value="CRT")

    async def _submit_and_confirm(self) -> bool:
        """
        Hace clic en 'Agregar Certificado' y detecta si el portal confirma el éxito.
        Maneja SweetAlert2 (el portal lo usa según el HTML) y cierre del modal.
        """
        await self.page.click(BTN_SUBMIT)

        # Esperar SweetAlert2 de éxito o cierre del modal
        try:
            # Caso 1: SweetAlert2 aparece
            await self.page.wait_for_selector(".swal2-popup", timeout=SUBMIT_TIMEOUT)
            # Aceptar/cerrar el alert
            confirm_btn = await self.page.query_selector(".swal2-confirm, .swal2-ok")
            if confirm_btn:
                await confirm_btn.click()
            return True
        except PlaywrightTimeout:
            pass

        try:
            # Caso 2: el modal se cierra (success sin alert)
            await self.page.wait_for_selector(
                f"{MODAL_K8S}:not(.show)",
                timeout=SUBMIT_TIMEOUT,
            )
            return True
        except PlaywrightTimeout:
            return False

    async def upload_batch(self, diffs: list[CertDiff]) -> dict:
        """
        Sube todos los CertDiff con needs_update=True.
        Retorna un resumen: {"ok": [...], "fail": [...]}
        """
        pending = [d for d in diffs if d.needs_update]
        print(f"\n[Upload] {len(pending)} certificados para actualizar en el portal...")

        ok: list[str] = []
        fail: list[str] = []

        for diff in pending:
            name = f"{diff.portal_cert.namespace}/{diff.portal_cert.secret_name}"
            success = await self.upload(diff)
            (ok if success else fail).append(name)

        print(f"  [Upload] ✓ Exitosos: {len(ok)} | ✗ Fallidos: {len(fail)}")
        if fail:
            print(f"  [Upload] Fallidos: {fail}")

        return {"ok": ok, "fail": fail}
