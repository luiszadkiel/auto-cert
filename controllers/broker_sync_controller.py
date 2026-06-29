"""
controllers/broker_sync_controller.py
───────────────────────────────────────
Controller: sincronización de certificados vencidos en archivos JKS (tab Brokers).

Flujo completo:
  1. Login en el portal
  2. Extraer tabla Brokers + detalle de cada JKS con vencidos
  3. Identificar certs vencidos que tienen un .crt disponible en CERTS_DIR
  4. Crear BrokerUploadJob por cada cert a actualizar
  5. Subir cada cert al portal (formulario del modal)
  6. Registrar toda la actividad en auditoría
"""

import os
import glob
from config.settings import TARGET_URL, OUTPUT_DIR, CERTS_DIR
from services.browser_service import BrowserService
from services.auth_service import AuthService
from services.broker_scraper_service import BrokerScraperService
from services.broker_uploader_service import BrokerUploaderService
from services.server_credentials_service import ServerCredentialsService
from services.audit_service import AuditService
from models.audit_log import EventType
from models.broker_cert import BrokerRow, BrokerCertDetail, BrokerUploadJob
from views.reporter import print_banner, save_json, build_output_paths


def _find_cert_file(issued_to: str, certs_dir: str) -> str | None:
    """
    Busca un archivo .crt o .cert en certs_dir cuyo nombre contenga
    alguna parte significativa del CN del certificado.
    Ej: 'DigiCert Global Root CA' → busca *digicert*global*.crt
    """
    if not os.path.isdir(certs_dir):
        return None

    # Normalizar CN para búsqueda
    cn_lower = issued_to.lower().replace(" ", "*").replace(".", "*")
    patterns = [
        os.path.join(certs_dir, f"*{cn_lower}*.crt"),
        os.path.join(certs_dir, f"*{cn_lower}*.cert"),
    ]

    for pattern in patterns:
        matches = glob.glob(pattern)
        if matches:
            return matches[0]

    # Fallback: buscar por la primera palabra del CN
    first_word = issued_to.split()[0].lower() if issued_to else ""
    if first_word:
        for ext in ("*.crt", "*.cert"):
            matches = glob.glob(os.path.join(certs_dir, f"*{first_word}*{ext[1:]}"))
            if matches:
                return matches[0]

    return None


def _build_jobs(
    details_map: dict[BrokerRow, list[BrokerCertDetail]],
    certs_dir: str,
    email: str,
) -> list[BrokerUploadJob]:
    """
    Por cada cert vencido, intenta encontrar el archivo .crt correspondiente
    y crea un BrokerUploadJob si lo encuentra.
    """
    jobs: list[BrokerUploadJob] = []
    missing: list[str] = []

    for row, details in details_map.items():
        expired = [d for d in details if d.is_expired()]
        for cert in expired:
            cert_file = _find_cert_file(cert.issued_to, certs_dir)
            if cert_file:
                jobs.append(BrokerUploadJob(
                    broker_row=row,
                    cert_detail=cert,
                    cert_file_path=cert_file,
                    alias=cert.issued_to,
                    email=email,
                ))
            else:
                missing.append(
                    f"{row.jks_name}/{cert.issued_to}"
                )

    if missing:
        print(f"\n  [Broker] ⚠ Sin archivo .crt para {len(missing)} cert(s):")
        for m in missing:
            print(f"    • {m}")
        print(f"  → Coloca los .crt en: {os.path.abspath(certs_dir)}")

    return jobs


class BrokerSyncController:
    """
    Controlador de sincronización de certificados Brokers.
    Detecta certs vencidos en archivos JKS y los actualiza en el portal.
    """

    async def run(self) -> None:
        print("\n[*] Iniciando sincronización Brokers → Portal BHD\n")

        audit = AuditService(mode="broker-sync")
        jobs_built: list[BrokerUploadJob] = []
        ok_list: list[str] = []
        fail_list: list[str] = []
        paths: dict = {}

        try:
            async with BrowserService() as browser:
                page = browser.page

                # ── 1. Login ───────────────────────────────────────────────────
                await browser.goto(TARGET_URL)
                auth = AuthService(page)
                if auth.needs_login():
                    if auth.has_credentials():
                        audit.log(EventType.AUTH_AUTO, "Auto-login con credenciales del .env")
                    else:
                        audit.log(EventType.AUTH_MANUAL, "Esperando login manual")
                    await auth.login()
                    audit.log(EventType.AUTH_OK, "Login completado")
                else:
                    audit.log(EventType.AUTH_OK, "Sesión ya activa")

                if "inventariodecertificados" not in page.url:
                    await browser.goto(TARGET_URL)
                await browser.wait_for_network_idle()

                # ── 2. Scraping Brokers + detalles ─────────────────────────────
                audit.log(EventType.SCRAPE_START, "Extrayendo tabla Brokers del portal")
                scraper = BrokerScraperService(page)
                details_map = await scraper.scrape_all()

                total_jks = len(details_map)
                total_expired = sum(
                    sum(1 for d in dets if d.is_expired())
                    for dets in details_map.values()
                )

                audit.log(
                    EventType.SCRAPE_OK,
                    f"{total_jks} JKS con certs vencidos leídos del portal",
                    jks_count=total_jks,
                    expired_certs=total_expired,
                )

                if total_expired == 0:
                    print_banner("✓ No hay certs vencidos en Brokers — nada que hacer.")
                    audit.close(status="OK", result="Sin certs vencidos")
                    return

                # ── 3 & 4. Construir jobs de upload ────────────────────────────
                from config.settings import CERT_EMAIL
                jobs_built = _build_jobs(details_map, CERTS_DIR, CERT_EMAIL)

                for job in jobs_built:
                    audit.log(
                        EventType.DIFF_NEEDS_UPDATE,
                        f"Cert vencido encontrado: {job.cert_detail.issued_to}",
                        jks=job.broker_row.jks_name,
                        ambiente=job.broker_row.ambiente,
                        expired_on=job.cert_detail.expires_at.isoformat()
                            if job.cert_detail.expires_at else "N/A",
                        cert_file=os.path.basename(job.cert_file_path),
                    )

                paths = build_output_paths(OUTPUT_DIR, "broker_sync")
                save_json([j.to_dict() for j in jobs_built], paths["json"])
                audit.log(EventType.FILE_SAVED, "Reporte de jobs guardado", path=paths["json"])

                if not jobs_built:
                    print(f"\n  ⚠ {total_expired} certs vencidos pero sin archivos .crt disponibles.")
                    print(f"  → Coloca los archivos .crt en: {os.path.abspath(CERTS_DIR)}")
                    audit.close(
                        status="OK",
                        result="Sin archivos .crt disponibles para subir",
                        expired_certs=total_expired,
                    )
                    return

                # ── 5. Subir certs al portal ───────────────────────────────────
                # Cargar credenciales del Excel (se hace una sola vez aquí)
                creds = ServerCredentialsService().load()
                uploader = BrokerUploaderService(page, credentials=creds)

                for job in jobs_built:
                    label = f"{job.broker_row.jks_name}/{job.alias}"
                    audit.log(
                        EventType.UPLOAD_START,
                        f"Subiendo '{label}' al portal",
                        jks=job.broker_row.jks_name,
                        ambiente=job.broker_row.ambiente,
                        alias=job.alias,
                    )
                    success = await uploader.upload(job)
                    if success:
                        ok_list.append(label)
                        audit.log(
                            EventType.UPLOAD_OK,
                            f"✓ '{label}' subido exitosamente",
                            alias=job.alias,
                        )
                    else:
                        fail_list.append(label)
                        audit.log(
                            EventType.UPLOAD_FAIL,
                            f"✗ Fallo al subir '{label}'",
                            alias=job.alias,
                        )

                # Reporte final
                summary_data = {
                    "jks_with_issues": total_jks,
                    "expired_certs": total_expired,
                    "jobs_created": len(jobs_built),
                    "uploaded_ok": len(ok_list),
                    "uploaded_fail": len(fail_list),
                    "ok": ok_list,
                    "fail": fail_list,
                }
                save_json(summary_data, paths["structure"])
                audit.log(EventType.FILE_SAVED, "Reporte final guardado", path=paths["structure"])

            # ── 6. Cerrar auditoría ────────────────────────────────────────────
            audit.close(
                status="OK" if not fail_list else "PARCIAL",
                jks_with_issues=total_jks,
                expired_certs=total_expired,
                uploaded_ok=len(ok_list),
                uploaded_fail=len(fail_list),
                ok=", ".join(ok_list) or "ninguno",
                fail=", ".join(fail_list) or "ninguno",
            )

        except Exception as exc:
            audit.log(EventType.ERROR, f"Error: {exc}", error=str(exc))
            audit.close(status="ERROR", error=str(exc))
            raise

        print_banner(
            f"✓ Broker Sync completado\n"
            f"  Actualizados: {len(ok_list)} | Fallidos: {len(fail_list)}\n"
            f"  Reporte: {os.path.abspath(paths.get('json', OUTPUT_DIR))}"
        )
