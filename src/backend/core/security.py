"""
Cryptographic security module for Nexora.
Handles:
1. Argon2id / bcrypt password hashing and verification.
2. Short-lived JWT Access Token issuance and stateless local verification.
3. Cryptographically secure random token generation (Refresh, Verify, Reset).
4. SHA-256 token hashing/fingerprinting for zero-plaintext token persistence.
"""

import hashlib
import hmac
import json
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

from backend.core.config import get_settings
from backend.core.exceptions import AuthenticationException
from backend.models.enums import RoleEnum

settings = get_settings()


try:
    from argon2 import PasswordHasher
    from argon2.exceptions import VerifyMismatchError

    _ph = PasswordHasher(
        time_cost=3,
        memory_cost=65536,  # 64 MB
        parallelism=4,
        hash_len=32,
        salt_len=16,
    )

    def get_password_hash(password: str) -> str:
        """Hash password using Argon2id."""
        return _ph.hash(password)

    def verify_password(plain_password: str, hashed_password: str) -> bool:
        """Verify plain password against Argon2id hash."""
        try:
            return _ph.verify(hashed_password, plain_password)
        except VerifyMismatchError:
            return False
        except Exception:
            return False

except ImportError:
    def get_password_hash(password: str) -> str:
        salt = secrets.token_hex(16)
        key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 100000)
        return f"pbkdf2_sha256${salt}${key.hex()}"

    def verify_password(plain_password: str, hashed_password: str) -> bool:
        try:
            algorithm, salt, key_hex = hashed_password.split("$")
            if algorithm != "pbkdf2_sha256":
                return False
            key = hashlib.pbkdf2_hmac("sha256", plain_password.encode("utf-8"), salt.encode("utf-8"), 100000)
            return hmac.compare_digest(key.hex(), key_hex)
        except Exception:
            return False


def generate_secure_token(nbytes: int = 48) -> str:
    """Generate a cryptographically secure URL-safe random string."""
    return secrets.token_urlsafe(nbytes)


def hash_token(raw_token: str) -> str:
    """Hash a raw secret token using SHA-256."""
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def create_access_token(
    user_id: uuid.UUID,
    client_id: uuid.UUID,
    role: RoleEnum,
    email: str,
    expires_delta: Optional[timedelta] = None,
) -> Tuple[str, str, int]:
    now = datetime.now(timezone.utc)
    if expires_delta:
        expire = now + expires_delta
    else:
        expire = now + timedelta(minutes=settings.access_token_expire_minutes)

    jti = str(uuid.uuid4())
    payload: Dict[str, Any] = {
        "sub": str(user_id),
        "client_id": str(client_id),
        "role": role.value if hasattr(role, "value") else str(role),
        "email": email,
        "jti": jti,
        "type": "access",
        "iat": int(now.timestamp()),
        "exp": int(expire.timestamp()),
        "iss": "nexora-platform",
    }

    try:
        import jwt
        token = jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)
    except ImportError:
        import base64
        header = {"alg": "HS256", "typ": "JWT"}
        header_b64 = base64.urlsafe_b64encode(json.dumps(header).encode()).decode().rstrip("=")
        payload_b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
        signing_input = f"{header_b64}.{payload_b64}"
        signature = hmac.new(settings.jwt_secret_key.encode(), signing_input.encode(), hashlib.sha256).digest()
        sig_b64 = base64.urlsafe_b64encode(signature).decode().rstrip("=")
        token = f"{signing_input}.{sig_b64}"

    expires_in_seconds = int((expire - now).total_seconds())
    return token, jti, expires_in_seconds


def decode_and_validate_access_token(token: str) -> Dict[str, Any]:
    try:
        import jwt
        payload = jwt.decode(
            token,
            settings.jwt_secret_key,
            algorithms=[settings.jwt_algorithm],
            issuer="nexora-platform",
            options={"require": ["sub", "client_id", "role", "exp", "iat", "jti", "type"]},
        )
    except ImportError:
        import base64
        parts = token.split(".")
        if len(parts) != 3:
            raise AuthenticationException("Invalid token format.", code="INVALID_TOKEN")
        header_b64, payload_b64, sig_b64 = parts
        signing_input = f"{header_b64}.{payload_b64}"
        expected_sig = hmac.new(settings.jwt_secret_key.encode(), signing_input.encode(), hashlib.sha256).digest()
        sig_padding = len(sig_b64) % 4
        sig_b64_padded = sig_b64 + "=" * (4 - sig_padding) if sig_padding else sig_b64
        given_sig = base64.urlsafe_b64decode(sig_b64_padded.encode())
        if not hmac.compare_digest(expected_sig, given_sig):
            raise AuthenticationException("Invalid token signature.", code="INVALID_SIGNATURE")
        payload_padding = len(payload_b64) % 4
        payload_b64_padded = payload_b64 + "=" * (4 - payload_padding) if payload_padding else payload_b64
        payload = json.loads(base64.urlsafe_b64decode(payload_b64_padded.encode()).decode())
    except Exception as e:
        raise AuthenticationException("Token verification failed: " + str(e), code="TOKEN_EXPIRED_OR_INVALID")

    if payload.get("type") != "access":
        raise AuthenticationException("Invalid token type.", code="INVALID_TOKEN_TYPE")

    exp = payload.get("exp")
    if not exp or datetime.fromtimestamp(exp, timezone.utc) < datetime.now(timezone.utc):
        raise AuthenticationException("Token has expired.", code="TOKEN_EXPIRED")

    return payload
