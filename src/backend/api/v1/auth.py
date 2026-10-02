"""
Authentication & Multi-Tenant Account REST API endpoints.
"""

from typing import Annotated
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.deps import get_current_user_db, get_db
from backend.core.config import get_settings
from backend.core.rate_limit import DistributedRateLimiter
from backend.models.client import ClientModel, UserModel
from backend.schemas.client import (
    ChangePasswordRequest,
    ClientProfileRead,
    ClientProfileUpdate,
    CurrentUserResponse,
    ForgotPasswordRequest,
    LoginRequest,
    LogoutRequest,
    MessageResponse,
    RefreshTokenRequest,
    RegisterRequest,
    ResetPasswordRequest,
    TokenResponse,
    UserRead,
)
from backend.services.auth_service import AuthService

router = APIRouter(prefix="/auth", tags=["Authentication"])


@router.post("/register", response_model=MessageResponse, status_code=status.HTTP_201_CREATED)
async def register(
    request: Request,
    data: RegisterRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    client_ip = request.client.host if request.client else "127.0.0.1"
    user_agent = request.headers.get("user-agent")

    settings = get_settings()
    await DistributedRateLimiter.check_rate_limit("register", client_ip, max_requests=settings.rate_limit_register_per_hour, window_seconds=3600)

    user = await AuthService.register_tenant(
        session=db,
        data=data,
        ip_address=client_ip,
        user_agent=user_agent,
    )
    await db.commit()

    return MessageResponse(
        message="Registration successful. Email verification required. Please check your inbox to verify your account."
    )

@router.get("/verify-email", response_model=MessageResponse)
async def verify_email(
    token: str,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    await AuthService.verify_email(
        session=db,
        token=token,
    )

    await db.commit()

    return MessageResponse(
        message="Email address successfully verified. You can now log in."
    )


@router.post("/login", response_model=TokenResponse)
async def login(
    request: Request,
    data: LoginRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    client_ip = request.client.host if request.client else "127.0.0.1"
    user_agent = request.headers.get("user-agent")

    settings = get_settings()
    await DistributedRateLimiter.check_rate_limit("login_ip", client_ip, max_requests=settings.rate_limit_login_per_minute, window_seconds=60)
    await DistributedRateLimiter.check_rate_limit("login_email", data.email.lower(), max_requests=settings.rate_limit_login_per_minute, window_seconds=60)

    user, access_token, refresh_token, expires_in = await AuthService.authenticate_user(
        session=db,
        data=data,
        ip_address=client_ip,
        user_agent=user_agent,
    )
    await db.commit()

    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        token_type="bearer",
        expires_in=expires_in,
        user=UserRead.model_validate(user),
    )


@router.post("/refresh", response_model=TokenResponse)
async def refresh_tokens(
    data: RefreshTokenRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    user, access_token, new_refresh_token, expires_in = await AuthService.refresh_tokens(
        session=db,
        refresh_token_string=data.refresh_token,
    )
    await db.commit()

    return TokenResponse(
        access_token=access_token,
        refresh_token=new_refresh_token,
        token_type="bearer",
        expires_in=expires_in,
        user=UserRead.model_validate(user),
    )


@router.post("/logout", response_model=MessageResponse)
async def logout(
    data: LogoutRequest,
):
    if data.refresh_token:
        parts = data.refresh_token.split(":")
        if len(parts) == 3:
            user_id_str, session_id, _ = parts
            from backend.core.rate_limit import get_redis_client
            redis = await get_redis_client()
            if redis:
                await redis.delete(f"refresh:{user_id_str}:{session_id}")

    return MessageResponse(message="Successfully logged out.")


@router.post("/forgot-password", response_model=MessageResponse)
async def forgot_password(
    request: Request,
    data: ForgotPasswordRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    client_ip = request.client.host if request.client else "127.0.0.1"
    user_agent = request.headers.get("user-agent")

    settings = get_settings()
    await DistributedRateLimiter.check_rate_limit("forgot_pwd", client_ip, max_requests=settings.rate_limit_forgot_password_per_hour, window_seconds=3600)

    await AuthService.request_password_reset(
        session=db,
        email=data.email,
        ip_address=client_ip,
        user_agent=user_agent,
    )
    await db.commit()

    return MessageResponse(message="If the account exists, a password reset email has been sent.")


@router.post("/reset-password", response_model=MessageResponse)
async def reset_password(
    request: Request,
    data: ResetPasswordRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    client_ip = request.client.host if request.client else "127.0.0.1"
    user_agent = request.headers.get("user-agent")

    await AuthService.reset_password(
        session=db,
        data=data,
        ip_address=client_ip,
        user_agent=user_agent,
    )
    await db.commit()

    return MessageResponse(message="Password has been successfully reset. Please log in with your new password.")


@router.post("/change-password", response_model=MessageResponse)
async def change_password(
    request: Request,
    data: ChangePasswordRequest,
    current_user: Annotated[UserModel, Depends(get_current_user_db)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    client_ip = request.client.host if request.client else "127.0.0.1"
    user_agent = request.headers.get("user-agent")

    await AuthService.change_password(
        session=db,
        current_user=current_user,
        data=data,
        ip_address=client_ip,
        user_agent=user_agent,
    )
    await db.commit()

    return MessageResponse(message="Password successfully updated.")


@router.get("/me", response_model=CurrentUserResponse)
async def get_current_user_profile(
    current_user: Annotated[UserModel, Depends(get_current_user_db)],
):
    return current_user


@router.get(
    "/profile",
    response_model=ClientProfileRead,
)
async def get_client_profile(
    current_user: Annotated[
        UserModel,
        Depends(get_current_user_db),
    ],
    db: Annotated[
        AsyncSession,
        Depends(get_db),
    ],
):
    client = await db.get(
        ClientModel,
        current_user.client_id,
    )

    if client is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Client profile not found.",
        )

    return client


@router.patch(
    "/profile",
    response_model=ClientProfileRead,
)
async def update_client_profile(
    data: ClientProfileUpdate,
    current_user: Annotated[
        UserModel,
        Depends(get_current_user_db),
    ],
    db: Annotated[
        AsyncSession,
        Depends(get_db),
    ],
):
    client = await db.get(
        ClientModel,
        current_user.client_id,
    )

    if client is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Client profile not found.",
        )

    update_data = data.model_dump(
        exclude_unset=True
    )

    for field, value in update_data.items():
        setattr(client, field, value)

    await db.commit()
    await db.refresh(client)

    return client
