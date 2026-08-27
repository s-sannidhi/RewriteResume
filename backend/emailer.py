"""Manual email sender — personal Gmail over SMTP SSL with an app password from the Keychain.
No drafting, no scheduling; only ever called by an explicit Send click in the panel.
"""
import smtplib
from email.message import EmailMessage
from pathlib import Path

from . import profile_store, secrets

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465

_PW_HINT = ("No Gmail app password. Enable 2-Step Verification, create an App Password at "
            "myaccount.google.com/apppasswords, then store it with:\n"
            "  security add-generic-password -U -s resume-rewriter-gmail -a <your gmail> -w <APP PASSWORD>\n"
            "or run scripts/setup_secrets.py")


class EmailError(Exception):
    def __init__(self, code: str, hint: str):
        super().__init__(hint)
        self.code, self.hint = code, hint


def send(to: str, subject: str, body: str, attachments: list[Path]) -> dict:
    profile = profile_store.load()
    addr = secrets.login_account(profile)
    pw = secrets.get_secret(secrets.SERVICE_GMAIL, addr, "RR_GMAIL_APP_PASSWORD")
    if not pw:
        raise EmailError("no_app_password", _PW_HINT)

    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = addr, to, subject
    msg.set_content(body)
    attached = []
    for p in attachments:
        msg.add_attachment(p.read_bytes(), maintype="application", subtype="pdf",
                           filename=p.name)
        attached.append(p.name)

    try:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30) as s:
            s.login(addr, pw)
            s.send_message(msg)
    except smtplib.SMTPAuthenticationError:
        raise EmailError("auth_failed",
                         "Gmail rejected the login (535). Make sure it's a 16-char APP password "
                         "for " + addr + ", not the account password. " + _PW_HINT)
    except (smtplib.SMTPException, OSError) as e:
        raise EmailError("send_failed", f"SMTP error: {e}")

    return {"from": addr, "to": to, "attached": attached}
