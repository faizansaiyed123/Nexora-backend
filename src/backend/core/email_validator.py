"""
Layered email validation and disposable domain protection module.
"""

import re
from typing import Set
from backend.core.config import get_settings
from backend.core.exceptions import ValidationException

settings = get_settings()

DISPOSABLE_EMAIL_DOMAINS: Set[str] = {
    "10minutemail.com", "10minutemail.net", "guerrillamail.com", "guerrillamail.net",
    "guerrillamail.org", "sharklasers.com", "grr.la", "mailinator.com",
    "yopmail.com", "yopmail.fr", "yopmail.net", "temp-mail.org",
    "tempmail.com", "tempmail.net", "throwawaymail.com", "getairmail.com",
    "dispostable.com", "trashmail.com", "trashmail.net", "burnermail.io",
}

EMAIL_REGEX = re.compile(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$")


def validate_and_normalize_email(email: str) -> str:
    if not email:
        raise ValidationException("Email address is required.", code="EMAIL_REQUIRED")

    normalized = email.strip().lower()

    if len(normalized) > 255:
        raise ValidationException("Email address is too long (maximum 255 characters).", code="EMAIL_TOO_LONG")

    if not EMAIL_REGEX.match(normalized):
        raise ValidationException("Invalid email format.", code="INVALID_EMAIL_FORMAT")

    domain = normalized.split("@")[-1]

    if settings.block_disposable_emails and domain in DISPOSABLE_EMAIL_DOMAINS:
        raise ValidationException(
            "Registration from disposable/temporary email providers is not permitted.",
            code="DISPOSABLE_EMAIL_BLOCKED",
        )

    return normalized


def validate_password_strength(password: str) -> None:
    if not password or len(password) < 8:
        raise ValidationException("Password must be at least 8 characters long.", code="PASSWORD_TOO_SHORT")

    if len(password) > 128:
        raise ValidationException("Password is too long (maximum 128 characters).", code="PASSWORD_TOO_LONG")

    has_upper = any(c.isupper() for c in password)
    has_lower = any(c.islower() for c in password)
    has_digit_or_special = any(c.isdigit() or not c.isalnum() for c in password)

    if not (has_upper and has_lower and has_digit_or_special):
        raise ValidationException(
            "Password must contain uppercase, lowercase, and at least one number or special character.",
            code="PASSWORD_TOO_WEAK",
        )
