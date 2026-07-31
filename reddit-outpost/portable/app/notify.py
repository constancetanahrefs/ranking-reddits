"""Email delivery — the standalone replacement for Letaido's `letaido_email`.

Letaido sends on the workspace's behalf with no key and restricts recipients to
org members. Standing alone you pick a transport:

  EMAIL_BACKEND=smtp     plain SMTP (works with Gmail app passwords, Fastmail,
                         Postfix, Mailpit for local testing)
  EMAIL_BACKEND=resend   Resend's HTTP API (needs a verified domain)
  EMAIL_BACKEND=console  print to stdout — the default, so a fresh install
                         never silently fails to send

There is no recipient allow-list here, unlike the Letaido build. Be careful what
you point it at.
"""
from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage

BACKEND = os.environ.get("EMAIL_BACKEND", "console").lower()
FROM_ADDR = os.environ.get("EMAIL_FROM", "outpost@localhost")

SMTP_HOST = os.environ.get("SMTP_HOST", "localhost")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
SMTP_TLS = os.environ.get("SMTP_TLS", "true").lower() == "true"

RESEND_KEY = os.environ.get("RESEND_API_KEY", "")


class EmailError(RuntimeError):
    """Raised on any delivery failure, so callers can log and carry on."""


def send_email(*, to: str, subject: str, body: str, body_html: str = "") -> dict:
    if not to or "@" not in to:
        raise EmailError(f"not a usable address: {to!r}")

    if BACKEND == "console":
        print(f"\n[outpost:email] to={to}\n  subject={subject}\n"
              f"  ({len(body)} chars text, {len(body_html)} chars html)\n"
              "  EMAIL_BACKEND=console — nothing was actually sent.\n")
        return {"backend": "console", "to": to}

    if BACKEND == "resend":
        import requests
        if not RESEND_KEY:
            raise EmailError("RESEND_API_KEY is unset")
        r = requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_KEY}"},
            json={"from": FROM_ADDR, "to": [to], "subject": subject,
                  "text": body, **({"html": body_html} if body_html else {})},
            timeout=20)
        if r.status_code >= 300:
            raise EmailError(f"resend {r.status_code}: {r.text[:300]}")
        return {"backend": "resend", "to": to, "id": r.json().get("id")}

    if BACKEND == "smtp":
        msg = EmailMessage()
        msg["From"] = FROM_ADDR
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(body)
        if body_html:
            msg.add_alternative(body_html, subtype="html")
        try:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as sm:
                if SMTP_TLS:
                    sm.starttls()
                if SMTP_USER:
                    sm.login(SMTP_USER, SMTP_PASS)
                sm.send_message(msg)
        except Exception as exc:  # noqa: BLE001
            raise EmailError(f"smtp: {exc}") from exc
        return {"backend": "smtp", "to": to}

    raise EmailError(f"unknown EMAIL_BACKEND={BACKEND!r}")
