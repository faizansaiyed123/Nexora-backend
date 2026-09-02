"""
Pydantic schemas for Multi-Tenant Clients, Users, and Authentication DTOs.
"""

import re
import uuid
from datetime import datetime
from typing import Optional
from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from backend.models.enums import ClientStatusEnum, RoleEnum


# ==========================================
# Client (Tenant) DTOs
# ==========================================

class ClientBase(BaseModel):
    name: str = Field(..., min_length=2, max_length=255, description="Tenant organization name")
    slug: str = Field(..., min_length=2, max_length=100, description="URL-safe unique identifier")
    tier: str = Field(default="ENTERPRISE", max_length=50)
    max_competitors: int = Field(default=20, ge=1, le=1000)
    max_tracked_offerings: int = Field(default=5000, ge=1, le=1000000)

    @field_validator("slug")
    @classmethod
    def validate_slug(cls, v: str) -> str:
        slug = v.strip().lower()
        if not re.match(r"^[a-z0-9]+(?:-[a-z0-9]+)*$", slug):
            raise ValueError("Slug must contain only lowercase alphanumeric characters and hyphens")
        return slug


class ClientCreate(ClientBase):
    pass


class ClientUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=2, max_length=255)
    tier: Optional[str] = Field(None, max_length=50)
    status: Optional[ClientStatusEnum] = None
    max_competitors: Optional[int] = Field(None, ge=1, le=1000)
    max_tracked_offerings: Optional[int] = Field(None, ge=1, le=1000000)


class ClientRead(ClientBase):
    id: uuid.UUID
    status: ClientStatusEnum
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


# ==========================================
# User DTOs
# ==========================================

class UserBase(BaseModel):
    email: EmailStr
    full_name: str = Field(..., min_length=2, max_length=255)
    role: RoleEnum = Field(default=RoleEnum.ANALYST)


class UserCreate(UserBase):
    client_id: uuid.UUID
    password: str = Field(..., min_length=8, max_length=128, description="Plaintext password for registration")


class UserUpdate(BaseModel):
    full_name: Optional[str] = Field(None, min_length=2, max_length=255)
    role: Optional[RoleEnum] = None
    is_active: Optional[bool] = None
    password: Optional[str] = Field(None, min_length=8, max_length=128)


class UserRead(UserBase):
    id: uuid.UUID
    client_id: uuid.UUID
    is_active: bool
    last_login_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


# ==========================================
# Authentication & Tenant Onboarding Requests & Responses
# ==========================================

class RegisterRequest(BaseModel):
    organization_name: str = Field(..., min_length=2, max_length=255, description="Tenant organization name")
    organization_slug: Optional[str] = Field(None, min_length=2, max_length=100, description="Optional custom organization slug")
    full_name: str = Field(..., min_length=2, max_length=255, description="Initial Admin full name")
    email: EmailStr = Field(..., description="Corporate email address")
    password: str = Field(..., min_length=8, max_length=128, description="Strong password")


class LoginRequest(BaseModel):
    email: EmailStr = Field(..., description="User email address")
    password: str = Field(..., min_length=1, max_length=128, description="User password")


class RefreshTokenRequest(BaseModel):
    refresh_token: str = Field(..., min_length=10, description="Opaque refresh token string")


class LogoutRequest(BaseModel):
    refresh_token: Optional[str] = Field(None, description="Optional refresh token to revoke immediately")


class ForgotPasswordRequest(BaseModel):
    email: EmailStr = Field(..., description="User email address requesting password reset")


class ResetPasswordRequest(BaseModel):
    token: str = Field(..., min_length=10, description="Password reset verification token")
    new_password: str = Field(..., min_length=8, max_length=128, description="New strong password")


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(..., min_length=1, max_length=128, description="Current plaintext password")
    new_password: str = Field(..., min_length=8, max_length=128, description="New strong password")


class ResendVerificationRequest(BaseModel):
    email: EmailStr = Field(..., description="User email address to resend verification email")


class MessageResponse(BaseModel):
    message: str = Field(..., description="Human-readable status or confirmation message")


class TokenResponse(BaseModel):
    access_token: str = Field(..., description="Signed JWT Bearer access token")
    refresh_token: Optional[str] = Field(None, description="Secure refresh token")
    token_type: str = Field(default="bearer", description="Token scheme (bearer)")
    expires_in: int = Field(..., description="Token lifespan in seconds")
    user: UserRead = Field(..., description="Current user information")


class TokenPayload(BaseModel):
    sub: str
    client_id: str
    role: str
    exp: int


class CurrentUserResponse(UserRead):
    client: Optional[ClientRead] = None
    model_config = ConfigDict(from_attributes=True)
