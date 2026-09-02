"""
Asynchronous background email worker for Nexora.
"""

import logging

logger = logging.getLogger(__name__)


async def send_verification_email_async(email: str, token: str, full_name: str) -> None:
    logger.info(f"[EMAIL QUEUED] Verification email for {email} (Token: {token[:8]}...)")


async def send_password_reset_email_async(email: str, token: str) -> None:
    logger.info(f"[EMAIL QUEUED] Password reset email for {email} (Token: {token[:8]}...)")


async def send_security_alert_email_async(email: str, alert_title: str, message: str) -> None:
    logger.info(f"[EMAIL QUEUED] Security alert for {email}: {alert_title}")
