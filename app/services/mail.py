import logging
from email.message import EmailMessage

import aiosmtplib

from app.config import settings

log = logging.getLogger(__name__)


async def send_email(to: str, subject: str, body: str) -> None:
    msg = EmailMessage()
    msg["From"] = settings.mail_from
    msg["To"] = to
    msg["Subject"] = f"{settings.app_name}: {subject}"
    msg.set_content(body)
    try:
        await aiosmtplib.send(
            msg,
            hostname=settings.smtp_host,
            port=settings.smtp_port,
            username=settings.smtp_user,
            password=settings.smtp_password,
            start_tls=settings.smtp_tls,
            timeout=10,
        )
    except (aiosmtplib.SMTPException, OSError):
        # Почта не должна ронять запрос; код дублируем в лог для локальной отладки.
        log.exception("Не удалось отправить письмо на %s: %s\n%s", to, subject, body)


async def send_code(to: str, purpose_title: str, code: str) -> None:
    minutes = settings.code_ttl_seconds // 60
    await send_email(
        to,
        purpose_title,
        f"Ваш код подтверждения: {code}\n\nКод действует {minutes} минут. "
        "Если вы не запрашивали код — просто проигнорируйте это письмо.",
    )
