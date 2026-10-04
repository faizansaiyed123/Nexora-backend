import pytest
import uuid
from unittest.mock import patch

from backend.services.auth_service import AuthService
from backend.schemas.client import RegisterRequest, LoginRequest
from backend.db.session import AsyncSessionLocal
from backend.core.exceptions import AuthenticationException, ValidationException


@pytest.fixture(autouse=True)
def disable_rate_limiting():
    from backend.core.config import Settings
    s = Settings(
        rate_limit_enabled=False,
        block_disposable_emails=True,
        email_verification_bypass=False,
    )
    # auth_service imports get_settings by name, so patching only
    # backend.core.config.get_settings would not reach it.
    with patch("backend.core.config.get_settings", return_value=s), \
         patch("backend.services.auth_service.get_settings", return_value=s):
        yield


@pytest.mark.asyncio
async def test_email_verification_flow():
    unique_suffix = uuid.uuid4().hex[:8]
    org_name = f"Test Tenant {unique_suffix}"
    full_name = "Test Admin"
    real_email = f"user_{unique_suffix}@nexora-corp.com"
    password = "SecurePassword123!"

    captured_tokens = []

    async def mock_send_verification(email, token, name):
        captured_tokens.append(token)

    in_memory_store = {}

    async def mock_store_token(token_hash, user_id, email, ttl_seconds=86400):
        in_memory_store[token_hash] = {"user_id": user_id, "email": email}

    async def mock_consume_token(token_hash):
        return in_memory_store.pop(token_hash, None)

    with patch("backend.services.auth_service.send_verification_email_async", side_effect=mock_send_verification), \
         patch("backend.services.auth_service.TokenSessionStore.store_email_verification_token", side_effect=mock_store_token), \
         patch("backend.services.auth_service.TokenSessionStore.get_and_consume_email_verification_token", side_effect=mock_consume_token):

        # ----------------------------------------------------
        # Test 5 — disposable email rejection
        # ----------------------------------------------------
        async with AsyncSessionLocal() as session:
            with pytest.raises(ValidationException) as exc_info:
                await AuthService.register_tenant(
                    session=session,
                    data=RegisterRequest(
                        organization_name="Disposable Org",
                        full_name="Temp User",
                        email="baduser@10minutemail.com",
                        password="SecurePassword123!",
                    )
                )
            assert exc_info.value.code == "DISPOSABLE_EMAIL_BLOCKED"

        # ----------------------------------------------------
        # Test 1 & 6 — normal registration with legitimate email
        # ----------------------------------------------------
        async with AsyncSessionLocal() as session:
            user = await AuthService.register_tenant(
                session=session,
                data=RegisterRequest(
                    organization_name=org_name,
                    full_name=full_name,
                    email=real_email,
                    password=password,
                )
            )
            await session.commit()
            assert user.email == real_email
            assert user.email_verified is False
            assert len(captured_tokens) == 1
            verification_token = captured_tokens[0]

        # ----------------------------------------------------
        # Test 2 — login before verification rejected
        # ----------------------------------------------------
        async with AsyncSessionLocal() as session:
            with pytest.raises(AuthenticationException) as exc_info:
                await AuthService.authenticate_user(
                    session=session,
                    data=LoginRequest(
                        email=real_email,
                        password=password,
                    )
                )
            assert exc_info.value.code == "EMAIL_NOT_VERIFIED"

        # ----------------------------------------------------
        # Test 3 — email verification
        # ----------------------------------------------------
        async with AsyncSessionLocal() as session:
            await AuthService.verify_email(
                session=session,
                token=verification_token,
            )
            await session.commit()

        # Check consumed token fails on replay
        async with AsyncSessionLocal() as session:
            with pytest.raises(ValidationException) as exc_info:
                await AuthService.verify_email(
                    session=session,
                    token=verification_token,
                )
            assert exc_info.value.code == "INVALID_VERIFICATION_TOKEN"

        # ----------------------------------------------------
        # Test 4 — login after verification
        # ----------------------------------------------------
        async with AsyncSessionLocal() as session:
            authed_user, access_token, refresh_token, expires_in = await AuthService.authenticate_user(
                session=session,
                data=LoginRequest(
                    email=real_email,
                    password=password,
                )
            )
            assert authed_user.email_verified is True
            assert access_token is not None
            assert len(access_token) > 20
            assert refresh_token is not None
            assert expires_in > 0


@pytest.mark.asyncio
async def test_email_verification_bypass_skips_token_and_allows_login():
    unique_suffix = uuid.uuid4().hex[:8]
    real_email = f"bypass_{unique_suffix}@nexora-corp.com"
    password = "SecurePassword123!"

    from backend.core.config import Settings
    bypassed = Settings(
        rate_limit_enabled=False,
        block_disposable_emails=True,
        email_verification_bypass=True,
    )

    with patch("backend.core.config.get_settings", return_value=bypassed), \
         patch("backend.services.auth_service.get_settings", return_value=bypassed), \
         patch("backend.services.auth_service.send_verification_email_async") as mock_send, \
         patch("backend.services.auth_service.TokenSessionStore.store_email_verification_token") as mock_store:

        async with AsyncSessionLocal() as session:
            user = await AuthService.register_tenant(
                session=session,
                data=RegisterRequest(
                    organization_name=f"Bypass Tenant {unique_suffix}",
                    full_name="Bypass Admin",
                    email=real_email,
                    password=password,
                )
            )
            await session.commit()
            assert user.email_verified is True

        mock_send.assert_not_called()
        mock_store.assert_not_called()

        async with AsyncSessionLocal() as session:
            authed_user, access_token, _, _ = await AuthService.authenticate_user(
                session=session,
                data=LoginRequest(
                    email=real_email,
                    password=password,
                )
            )
            assert authed_user.email_verified is True
            assert access_token is not None


def test_email_verification_bypass_rejected_in_production():
    from backend.core.config import Settings

    with pytest.raises(ValueError, match="EMAIL_VERIFICATION_BYPASS"):
        Settings(
            environment="production",
            allowed_hosts=["api.nexora.example"],
            email_verification_bypass=True,
        )
