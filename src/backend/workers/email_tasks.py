"""
Asynchronous email service for Nexora.
Sends verification, password reset, and security alert emails via SMTP.
"""

import logging
from email.message import EmailMessage

import aiosmtplib

from backend.core.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


async def _send_email(
    to_email: str,
    subject: str,
    body: str,
) -> None:
    if not settings.smtp_username or not settings.smtp_password:
        logger.error("SMTP credentials are not configured.")
        raise RuntimeError("SMTP email configuration is missing.")

    message = EmailMessage()
    message["From"] = (
        f"{settings.smtp_from_name} <{settings.smtp_from_email}>"
    )
    message["To"] = to_email
    message["Subject"] = subject
    message.set_content(body)

    try:
        await aiosmtplib.send(
            message,
            hostname=settings.smtp_host,
            port=settings.smtp_port,
            username=settings.smtp_username,
            password=settings.smtp_password,
            start_tls=True,
        )

        logger.info(f"Email sent successfully to {to_email}")

    except Exception:
        logger.exception(f"Failed to send email to {to_email}")
        raise


async def send_verification_email_async(
    email: str,
    token: str,
    full_name: str,
) -> None:
    verification_url = (
        f"{settings.frontend_url}/verify-email?token={token}"
    )

    subject = "Verify your Nexora account"

    body = f"""Hello {full_name},

Welcome to Nexora!

Please verify your email address by opening the link below:

{verification_url}

This verification link will expire in 24 hours.

If you did not create this account, you can safely ignore this email.

Regards,
{settings.smtp_from_name}
"""

    await _send_email(
        to_email=email,
        subject=subject,
        body=body,
    )


async def send_password_reset_email_async(
    email: str,
    token: str,
) -> None:
    reset_url = (
        f"{settings.frontend_url}/reset-password?token={token}"
    )

    subject = "Reset your Nexora password"

    body = f"""Hello,

A password reset was requested for your Nexora account.

Open the link below to reset your password:

{reset_url}

This link will expire in 15 minutes.

If you did not request a password reset, you can safely ignore this email.

Regards,
{settings.smtp_from_name}
"""

    await _send_email(
        to_email=email,
        subject=subject,
        body=body,
    )


async def send_security_alert_email_async(
    email: str,
    alert_title: str,
    message: str,
) -> None:
    subject = f"Nexora Security Alert: {alert_title}"

    body = f"""Hello,

Security alert: {alert_title}

{message}

If you do not recognize this activity, please secure your account immediately.

Regards,
{settings.smtp_from_name}
"""

    await _send_email(
        to_email=email,
        subject=subject,
        body=body,
    )


async def send_competitive_alert_email_async(
    email: str,
    alert_title: str,
    message: str,
) -> None:
    subject = f"Nexora Competitive Alert: {alert_title}"
    body = f"""Hello,

Nexora detected a competitive intelligence event:

{alert_title}

{message}

Open your Nexora workspace to review the latest observation and decide whether action is needed.

Regards,
{settings.smtp_from_name}
"""
    await _send_email(
        to_email=email,
        subject=subject,
        body=body,
    )
