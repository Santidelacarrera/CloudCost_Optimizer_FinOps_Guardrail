"""Correo transaccional (verificación, restablecimiento, avisos de seguridad e invitaciones).

Con SMTP configurado se envía por SMTP+STARTTLS. Sin SMTP (solo desarrollo) el mensaje se escribe en el log, de modo que el enlace
se puede copiar; en producción la configuración exige SMTP_HOST.
"""
from __future__ import annotations

import logging
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from html import escape

from ..config import Settings

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Mail:
    to: str
    subject: str
    text: str
    html: str | None = None
    link: str | None = None          # enlace principal, útil en modo desarrollo


def send(settings: Settings, mail: Mail) -> bool:
    if not settings.smtp_host:
        if settings.env == "development":              # el enlace lleva un token de un solo uso: solo en desarrollo se deja en el log
            log.warning("CORREO (SMTP no configurado) para=%s asunto=%r enlace=%s", mail.to, mail.subject, mail.link)
        else:
            log.warning("CORREO (SMTP no configurado) no enviado a %s: %r", mail.to, mail.subject)
        return False
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = settings.smtp_from, mail.to, mail.subject
    msg.set_content(mail.text)
    if mail.html:
        msg.add_alternative(mail.html, subtype="html")
    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as smtp:
            if settings.smtp_starttls:
                smtp.starttls(context=ssl.create_default_context())
            if settings.smtp_user and settings.smtp_password:
                smtp.login(settings.smtp_user, settings.smtp_password.get_secret_value())
            smtp.send_message(msg)
        return True
    except Exception:
        log.exception("no se pudo enviar el correo a %s", mail.to)
        return False


def _html(title: str, body: str, button: tuple[str, str] | None = None) -> str:
    btn = (f'<p style="margin:24px 0"><a href="{escape(button[1])}" style="background:#0a7f6b;color:#fff;padding:12px 20px;'
           f'border-radius:10px;text-decoration:none;font-weight:600">{escape(button[0])}</a></p>') if button else ""
    return (f'<div style="font-family:system-ui,sans-serif;max-width:520px;margin:auto;padding:24px;color:#14213d">'
            f'<h2 style="margin:0 0 12px">{escape(title)}</h2><p style="line-height:1.55">{escape(body)}</p>{btn}'
            f'<p style="color:#5b6b7c;font-size:13px">Si no fuiste tú, ignora este mensaje.</p></div>')


def verify_email_mail(to: str, name: str, url: str) -> Mail:
    body = f"Hola {name}, confirma tu correo para activar tu cuenta de CloudCost. El enlace vale 24 horas."
    return Mail(to, "Confirma tu correo en CloudCost", f"{body}\n\n{url}\n\nSi no creaste la cuenta, ignora este mensaje.",
                _html("Confirma tu correo", body, ("Confirmar correo", url)), url)


def reset_mail(to: str, url: str) -> Mail:
    body = "Recibimos una solicitud para cambiar tu contraseña. El enlace vale 1 hora y sirve una sola vez."
    return Mail(to, "Cambia tu contraseña de CloudCost", f"{body}\n\n{url}\n\nSi no fuiste tú, ignora este mensaje: tu contraseña no cambia.",
                _html("Cambia tu contraseña", body, ("Elegir nueva contraseña", url)), url)


def already_registered_mail(to: str, login_url: str, reset_url: str) -> Mail:
    body = "Alguien intentó crear una cuenta con este correo, pero ya tienes una. Si fuiste tú, inicia sesión o restablece tu contraseña."
    return Mail(to, "Ya tienes una cuenta en CloudCost", f"{body}\n\nIniciar sesión: {login_url}\nRestablecer contraseña: {reset_url}",
                _html("Ya tienes una cuenta", body, ("Iniciar sesión", login_url)), login_url)


def invite_mail(to: str, org: str, inviter: str, url: str) -> Mail:
    body = f"{inviter} te invitó a {org} en CloudCost. Crea tu cuenta con este enlace; vale 7 días."
    return Mail(to, f"Te invitaron a {org} en CloudCost", f"{body}\n\n{url}", _html(f"Únete a {org}", body, ("Crear mi cuenta", url)), url)


def security_notice_mail(to: str, what: str) -> Mail:
    body = f"{what} Si no fuiste tú, restablece tu contraseña de inmediato y cierra las sesiones activas."
    return Mail(to, "Aviso de seguridad de CloudCost", body, _html("Aviso de seguridad", body))
