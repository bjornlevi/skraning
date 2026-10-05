"""Outgoing email. Plain text only, rendered from templates/emails/*.txt.

MAIL_BACKEND:
  smtp    — send via SMTP_HOST (production)
  console — print to stdout (default, for development)
  memory  — append to `outbox` (tests)
"""

import logging
import smtplib
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

from flask import current_app, render_template

log = logging.getLogger(__name__)

outbox: list[EmailMessage] = []


def send(to: str, subject: str, template: str, **context) -> bool:
    cfg = current_app.config
    body = render_template(f"emails/{template}", **context)

    msg = EmailMessage()
    msg["From"] = formataddr((cfg["MAIL_FROM_NAME"], cfg["MAIL_FROM"]))
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=cfg["MAIL_FROM"].rsplit("@", 1)[-1])
    # Quoted-printable keeps the text readable; the default for non-ASCII is base64
    msg.set_content(body, cte="quoted-printable")

    backend = cfg["MAIL_BACKEND"]
    try:
        if backend == "memory":
            outbox.append(msg)
        elif backend == "console":
            # Decoded, so codes and links can be copied straight from the terminal
            print(f"\n{'=' * 70}\nTo: {to}\nSubject: {subject}\n\n{body}\n{'=' * 70}\n", flush=True)
        else:
            with smtplib.SMTP(cfg["SMTP_HOST"], cfg["SMTP_PORT"], timeout=20) as smtp:
                if cfg["SMTP_STARTTLS"]:
                    smtp.starttls()
                if cfg["SMTP_USER"]:
                    smtp.login(cfg["SMTP_USER"], cfg["SMTP_PASSWORD"])
                smtp.send_message(msg)
        return True
    except (OSError, smtplib.SMTPException):
        log.exception("Failed to send '%s' to %s", subject, to)
        return False
