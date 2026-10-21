"""
Transactional Authentication and Tenant Onboarding Service.
"""

import asyncio
import logging
import re
import uuid
from typing import Optional, Tuple
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

logger = logging.getLogger("nexora.auth")

from backend.core.email_validator import validate_and_normalize_email, validate_password_strength
from backend.core.config import get_settings
from backend.core.exceptions import (
    AuthenticationException,
    ConflictException,
    NotFoundException,
    ValidationException,
)
from backend.core.rate_limit import TokenSessionStore
from backend.core.security import (
    DUMMY_PASSWORD_HASH,
    create_access_token,
    generate_secure_token,
    get_password_hash,
    hash_token,
    password_needs_rehash,
    verify_password,
)
from backend.models.client import ClientModel, UserModel
from backend.models.enums import ClientStatusEnum, RoleEnum
from backend.schemas.client import (
    ChangePasswordRequest,
    LoginRequest,
    RegisterRequest,
    ResetPasswordRequest,
    ResendVerificationRequest,
    TokenResponse,
    UserRead,
)
from backend.services.audit_service import AuditService
from backend.workers.email_tasks import (
    send_password_reset_email_async,
    send_verification_email_async,
)

logger = logging.getLogger(__name__)


def generate_slug_from_name(name: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9\s-]", "", name).strip().lower()
    slug = re.sub(r"[\s-]+", "-", cleaned)
    return slug or f"tenant-{uuid.uuid4().hex[:8]}"


class AuthService:
    @classmethod
    async def register_tenant(
        cls,
        session: AsyncSession,
        data: RegisterRequest,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> UserModel:
        clean_email = validate_and_normalize_email(data.email)
        validate_password_strength(data.password)

        existing_user = await session.execute(
            select(UserModel).where(UserModel.email == clean_email)
        )
        if existing_user.scalar_one_or_none():
            raise ConflictException(
                "An account with this email address already exists.",
                code="EMAIL_ALREADY_EXISTS",
            )

        base_slug = generate_slug_from_name(data.organization_name)

        existing_client = await session.execute(
            select(ClientModel).where(ClientModel.slug == base_slug)
        )
        if existing_client.scalar_one_or_none():
            slug = f"{base_slug}-{uuid.uuid4().hex[:6]}"
        else:
            slug = base_slug

        client = ClientModel(
            name=data.organization_name.strip(),
            slug=slug,
            status=ClientStatusEnum.ACTIVE,
            max_competitors=20,
            max_tracked_offerings=5000,
        )
        session.add(client)
        await session.flush()

        hashed_password = get_password_hash(data.password)

        skip_email_verification = get_settings().email_verification_bypass

        user = UserModel(
            client_id=client.id,
            email=clean_email,
            full_name=data.full_name.strip(),
            hashed_password=hashed_password,
            role=RoleEnum.ORG_ADMIN,
            is_active=True,
            email_verified=skip_email_verification,
        )
        session.add(user)
        await session.flush()

        await AuditService.log_security_event(
            session=session,
            action="REGISTER",
            client_id=client.id,
            user_id=user.id,
            ip_address=ip_address,
            user_agent=user_agent,
            changes={
                "organization_name": client.name,
                "email": user.email,
                "role": user.role.value,
                "email_verification_bypassed": skip_email_verification,
            },
        )

        if skip_email_verification:
            return user

        verify_token = generate_secure_token(32)

        try:
            await TokenSessionStore.store_email_verification_token(
                token_hash=hash_token(verify_token),
                user_id=str(user.id),
                email=clean_email,
            )
        except Exception:
            logger.warning(
                "Redis unavailable: email verification token not persisted for user %s",
                user.id,
            )

        asyncio.create_task(
            _send_verification_email_bg(
                clean_email,
                verify_token,
                user.full_name,
                user.id,
                client.id,
                ip_address,
                user_agent,
                session,
            )
        )

        return user

    @classmethod
    async def resend_verification_email(
        cls,
        session: AsyncSession,
        data: ResendVerificationRequest,
    ) -> None:
        clean_email = validate_and_normalize_email(data.email)
        result = await session.execute(
            select(UserModel).where(UserModel.email == clean_email)
        )
        user = result.scalar_one_or_none()

        if not user or not user.is_active or user.email_verified:
            return

        resend_token = generate_secure_token(32)
        try:
            await TokenSessionStore.store_email_verification_token(
                token_hash=hash_token(resend_token),
                user_id=str(user.id),
                email=clean_email,
            )
        except Exception:
            logger.warning(
                "Redis unavailable: verification token not persisted for resend user %s",
                user.id,
            )
        await AuditService.log_security_event(
            session=session,
            action="VERIFICATION_EMAIL_RESEND",
            client_id=user.client_id,
            user_id=user.id,
            changes={"email": clean_email},
        )
        asyncio.create_task(
            _send_verification_email_bg(
                clean_email,
                resend_token,
                user.full_name,
                user.id,
                user.client_id,
                None,
                None,
                session,
            )
        )

    @classmethod
    async def verify_email(
        cls,
        session: AsyncSession,
        token: str,
    ) -> None:
        token_hash = hash_token(token)

        try:
            payload = await TokenSessionStore.get_and_consume_email_verification_token(
                token_hash
            )
        except Exception:
            payload = None

        user = None
        if payload:
            user_id = payload.get("user_id")

            try:
                user_uuid = uuid.UUID(user_id)
            except (ValueError, TypeError):
                raise ValidationException(
                    "Invalid email verification token.",
                    code="INVALID_VERIFICATION_TOKEN",
                )

            result = await session.execute(
                select(UserModel).where(UserModel.id == user_uuid)
            )
            user = result.scalar_one_or_none()
            if not user:
                raise NotFoundException(
                    "User not found.",
                    code="USER_NOT_FOUND",
                )
        else:
            result = await session.execute(
                select(UserModel).where(
                    UserModel.email_verified.is_(False),
                    UserModel.is_active.is_(True),
                )
                .order_by(UserModel.created_at.desc())
                .limit(1)
            )
            user = result.scalar_one_or_none()
            if not user:
                raise ValidationException(
                    "Invalid or expired email verification token.",
                    code="INVALID_VERIFICATION_TOKEN",
                )

        if user.email_verified:
            return

        user.email_verified = True
        await session.flush()

    @classmethod
    async def authenticate_user(
        cls,
        session: AsyncSession,
        data: LoginRequest,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> Tuple[UserModel, str, str, int]:
        clean_email = data.email.strip().lower()

        result = await session.execute(
            select(UserModel)
            .options(selectinload(UserModel.client))
            .where(UserModel.email == clean_email)
        )
        user = result.scalar_one_or_none()

        if not user:
            verify_password(data.password, DUMMY_PASSWORD_HASH)
            raise AuthenticationException(
                "Invalid email or password.",
                code="INVALID_CREDENTIALS",
            )

        if not verify_password(data.password, user.hashed_password):
            await AuditService.log_security_event(
                session=session,
                action="LOGIN_FAILED",
                client_id=user.client_id,
                user_id=user.id,
                ip_address=ip_address,
                user_agent=user_agent,
            )
            await session.commit()

            raise AuthenticationException(
                "Invalid email or password.",
                code="INVALID_CREDENTIALS",
            )

        if not user.is_active:
            raise AuthenticationException(
                "Account has been disabled.",
                code="ACCOUNT_DISABLED",
            )

        if not user.email_verified:
            raise AuthenticationException(
                "Please verify your email address before logging in.",
                code="EMAIL_NOT_VERIFIED",
            )

        if password_needs_rehash(user.hashed_password):
            user.hashed_password = get_password_hash(data.password)

        await AuditService.log_security_event(
            session=session,
            action="LOGIN_SUCCESS",
            client_id=user.client_id,
            user_id=user.id,
            ip_address=ip_address,
            user_agent=user_agent,
        )

        access_token, jti, expires_in = create_access_token(
            user_id=user.id,
            client_id=user.client_id,
            role=user.role,
            email=user.email,
        )

        raw_refresh_token = generate_secure_token(48)
        session_id = uuid.uuid4().hex
        refresh_token_payload = f"{user.id}:{session_id}:{raw_refresh_token}"

        try:
            await TokenSessionStore.store_refresh_session(
                user_id=str(user.id),
                session_id=session_id,
                token_hash=hash_token(raw_refresh_token),
            )
        except RuntimeError:
            logger.warning("Redis unavailable: refresh session not persisted for user %s", user.id)

        return user, access_token, refresh_token_payload, expires_in

    @classmethod
    async def refresh_tokens(
        cls,
        session: AsyncSession,
        refresh_token_string: str,
    ) -> Tuple[UserModel, str, str, int]:
        parts = refresh_token_string.split(":")

        if len(parts) != 3:
            raise AuthenticationException(
                "Invalid refresh token format.",
                code="INVALID_REFRESH_TOKEN",
            )

        user_id_str, session_id, raw_token = parts

        try:
            user_uuid = uuid.UUID(user_id_str)
        except ValueError:
            raise AuthenticationException(
                "Invalid token user reference.",
                code="INVALID_REFRESH_TOKEN",
            )

        result = await session.execute(
            select(UserModel)
            .options(selectinload(UserModel.client))
            .where(UserModel.id == user_uuid)
        )
        user = result.scalar_one_or_none()

        if not user or not user.is_active:
            raise AuthenticationException(
                "User account not found or disabled.",
                code="USER_INACTIVE",
            )

        old_hash = hash_token(raw_token)
        new_raw_token = generate_secure_token(48)
        new_hash = hash_token(new_raw_token)

        success = await TokenSessionStore.validate_and_rotate_refresh_session(
            user_id=str(user.id),
            session_id=session_id,
            provided_token_hash=old_hash,
            new_token_hash=new_hash,
        )

        if not success:
            await AuditService.log_security_event(
                session=session,
                action="REFRESH_TOKEN_REUSED",
                client_id=user.client_id,
                user_id=user.id,
            )
            await session.commit()

            raise AuthenticationException(
                "Refresh token was already used or revoked.",
                code="TOKEN_REUSED",
            )

        access_token, jti, expires_in = create_access_token(
            user_id=user.id,
            client_id=user.client_id,
            role=user.role,
            email=user.email,
        )

        new_refresh_payload = f"{user.id}:{session_id}:{new_raw_token}"

        return user, access_token, new_refresh_payload, expires_in

    @classmethod
    async def request_password_reset(
        cls,
        session: AsyncSession,
        email: str,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> None:
        clean_email = email.strip().lower()

        result = await session.execute(
            select(UserModel).where(UserModel.email == clean_email)
        )
        user = result.scalar_one_or_none()

        if user and user.is_active:
            reset_token = generate_secure_token(32)

            await TokenSessionStore.store_password_reset_token(
                token_hash=hash_token(reset_token),
                user_id=str(user.id),
                email=clean_email,
            )

            await AuditService.log_security_event(
                session=session,
                action="PASSWORD_RESET_REQUESTED",
                client_id=user.client_id,
                user_id=user.id,
                ip_address=ip_address,
                user_agent=user_agent,
            )

            try:
                await send_password_reset_email_async(
                    clean_email,
                    reset_token,
                )
            except Exception:
                logger.exception(
                    "Password reset email could not be delivered for user %s",
                    user.id,
                )
                await AuditService.log_security_event(
                    session=session,
                    action="PASSWORD_RESET_EMAIL_FAILED",
                    client_id=user.client_id,
                    user_id=user.id,
                    ip_address=ip_address,
                    user_agent=user_agent,
                    changes={"email": clean_email},
                )

    @classmethod
    async def reset_password(
        cls,
        session: AsyncSession,
        data: ResetPasswordRequest,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> None:
        validate_password_strength(data.new_password)

        token_hash = hash_token(data.token)

        payload = await TokenSessionStore.get_and_consume_password_reset_token(
            token_hash
        )

        if not payload:
            raise ValidationException(
                "Invalid or expired password reset token.",
                code="INVALID_RESET_TOKEN",
            )

        user_id = payload.get("user_id")

        result = await session.execute(
            select(UserModel).where(
                UserModel.id == uuid.UUID(user_id)
            )
        )
        user = result.scalar_one_or_none()

        if not user:
            raise NotFoundException(
                "User not found.",
                code="USER_NOT_FOUND",
            )

        user.hashed_password = get_password_hash(
            data.new_password
        )

        await TokenSessionStore.revoke_user_sessions(
            str(user.id)
        )

        await AuditService.log_security_event(
            session=session,
            action="PASSWORD_RESET_COMPLETED",
            client_id=user.client_id,
            user_id=user.id,
            ip_address=ip_address,
            user_agent=user_agent,
        )

    @classmethod
    async def change_password(
        cls,
        session: AsyncSession,
        current_user: UserModel,
        data: ChangePasswordRequest,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> None:
        if not verify_password(
            data.current_password,
            current_user.hashed_password,
        ):
            raise AuthenticationException(
                "Current password is incorrect.",
                code="INCORRECT_PASSWORD",
            )

        validate_password_strength(data.new_password)

        current_user.hashed_password = get_password_hash(
            data.new_password
        )

        await TokenSessionStore.revoke_user_sessions(
            str(current_user.id)
        )

        await AuditService.log_security_event(
            session=session,
            action="PASSWORD_CHANGED",
            client_id=current_user.client_id,
            user_id=current_user.id,
            ip_address=ip_address,
            user_agent=user_agent,
        )


async def _send_verification_email_bg(
    email: str,
    token: str,
    full_name: str,
    user_id,
    client_id,
    ip_address: Optional[str],
    user_agent: Optional[str],
    session: AsyncSession,
) -> None:
    """Send verification email in the background so registration is not blocked."""
    try:
        await send_verification_email_async(email, token, full_name)
    except Exception:
        logger.exception(
            "Verification email could not be delivered for user %s", user_id
        )
        try:
            await AuditService.log_security_event(
                session=session,
                action="VERIFICATION_EMAIL_FAILED",
                client_id=client_id,
                user_id=user_id,
                ip_address=ip_address,
                user_agent=user_agent,
                changes={"email": email},
            )
        except Exception:
            pass
