"""
email_service.py — SMTP email delivery for backtest reports.

Configure via environment variables (or config.py):
    EMAIL_ENABLED    = true
    EMAIL_SMTP_HOST  = smtp.gmail.com
    EMAIL_SMTP_PORT  = 587
    EMAIL_USER       = you@gmail.com
    EMAIL_PASSWORD   = app-password
    EMAIL_RECIPIENT  = you@gmail.com

For Gmail, generate an App Password at:
    https://myaccount.google.com/apppasswords
"""
from __future__ import annotations
import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional

import src.config as config

logger = logging.getLogger(__name__)


class EmailService:

    def __init__(self) -> None:
        self.enabled   = getattr(config, "EMAIL_ENABLED",   False)
        self.smtp_host = getattr(config, "EMAIL_SMTP_HOST", "smtp.gmail.com")
        self.smtp_port = getattr(config, "EMAIL_SMTP_PORT", 587)
        self.user      = getattr(config, "EMAIL_USER",      "")
        self.password  = getattr(config, "EMAIL_PASSWORD",  "")
        self.recipient = getattr(config, "EMAIL_RECIPIENT", "")

        if self.enabled:
            missing = [k for k, v in {
                "EMAIL_USER": self.user,
                "EMAIL_PASSWORD": self.password,
                "EMAIL_RECIPIENT": self.recipient,
            }.items() if not v]
            if missing:
                logger.error(
                    "EmailService: EMAIL_ENABLED=True but missing config: %s", missing
                )
                self.enabled = False

    # ── Public ──────────────────────────────────────────────────────

    def send_report(
        self,
        subject:         str,
        body:            str,
        attachment_path: Optional[str] = None,
    ) -> bool:
        """
        Send an email, optionally with a text file attachment.
        Returns True on success, False on failure or when disabled.
        """
        if not self.enabled:
            logger.info("EmailService: disabled — skipping send of '%s'.", subject)
            return False

        try:
            msg = MIMEMultipart()
            msg["From"]    = self.user
            msg["To"]      = self.recipient
            msg["Subject"] = subject

            msg.attach(MIMEText(body, "plain", "utf-8"))

            if attachment_path:
                p = Path(attachment_path)
                if p.exists():
                    content = p.read_text(encoding="utf-8")
                    attachment = MIMEText(content, "plain", "utf-8")
                    attachment.add_header(
                        "Content-Disposition",
                        f'attachment; filename="{p.name}"',
                    )
                    msg.attach(attachment)
                else:
                    logger.warning("EmailService: attachment not found: %s", attachment_path)

            with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=30) as server:
                server.ehlo()
                server.starttls()
                server.login(self.user, self.password)
                server.sendmail(self.user, self.recipient, msg.as_string())

            logger.info("EmailService: sent '%s' → %s", subject, self.recipient)
            return True

        except smtplib.SMTPAuthenticationError:
            logger.error(
                "EmailService: authentication failed. "
                "For Gmail use an App Password, not your account password."
            )
        except smtplib.SMTPException as exc:
            logger.error("EmailService: SMTP error: %s", exc)
        except Exception as exc:
            logger.error("EmailService: unexpected error: %s", exc)

        return False