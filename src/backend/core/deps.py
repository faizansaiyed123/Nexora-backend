"""
FastAPI Security & Multi-Tenant Dependencies.
"""

import uuid
from typing import Annotated, Callable, List, Optional
from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.core.exceptions import AuthenticationException, ForbiddenException
from backend.core.security import decode_and_validate_access_token
from backend.db.session import get_db
from backend.models.client import ClientModel, UserModel
from backend.models.enums import RoleEnum

http_bearer = HTTPBearer(auto_error=False)


class AuthenticatedUserContext:
    def __init__(self, user_id: uuid.UUID, client_id: uuid.UUID, role: RoleEnum, email: str, jti: str):
        self.id = user_id
        self.client_id = client_id
        self.role = role
        self.email = email
        self.jti = jti


async def get_current_user_claims(
    credentials: Annotated[Optional[HTTPAuthorizationCredentials], Depends(http_bearer)],
) -> AuthenticatedUserContext:
    if not credentials or not credentials.credentials:
        raise AuthenticationException(
            message="Not authenticated. Bearer token required.",
            code="NOT_AUTHENTICATED",
        )

    payload = decode_and_validate_access_token(credentials.credentials)

    try:
        user_id = uuid.UUID(payload["sub"])
        client_id = uuid.UUID(payload["client_id"])
        role = RoleEnum(payload["role"])
        email = payload["email"]
        jti = payload["jti"]
    except Exception:
        raise AuthenticationException("Malformed token claims.", code="INVALID_TOKEN_CLAIMS")

    return AuthenticatedUserContext(
        user_id=user_id,
        client_id=client_id,
        role=role,
        email=email,
        jti=jti,
    )


async def get_current_user_db(
    auth_ctx: Annotated[AuthenticatedUserContext, Depends(get_current_user_claims)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> UserModel:
    result = await db.execute(
        select(UserModel)
        .options(selectinload(UserModel.client))
        .where(UserModel.id == auth_ctx.id, UserModel.client_id == auth_ctx.client_id)
    )
    user = result.scalar_one_or_none()
    if not user or not user.is_active:
        raise AuthenticationException("User account not found or disabled.", code="USER_DISABLED")
    return user


async def get_current_tenant_id(
    auth_ctx: Annotated[AuthenticatedUserContext, Depends(get_current_user_claims)],
) -> uuid.UUID:
    return auth_ctx.client_id


def require_role(allowed_roles: List[RoleEnum]) -> Callable:
    async def role_checker(
        auth_ctx: Annotated[AuthenticatedUserContext, Depends(get_current_user_claims)],
    ) -> AuthenticatedUserContext:
        if auth_ctx.role not in allowed_roles and auth_ctx.role != RoleEnum.SUPER_ADMIN:
            raise ForbiddenException(
                f"Access forbidden: requires one of {[r.value for r in allowed_roles]}",
                code="INSUFFICIENT_PERMISSIONS",
            )
        return auth_ctx

    return role_checker


require_admin = require_role([RoleEnum.ORG_ADMIN, RoleEnum.SUPER_ADMIN])
require_analyst = require_role([RoleEnum.ORG_ADMIN, RoleEnum.ANALYST, RoleEnum.VIEWER, RoleEnum.SUPER_ADMIN])
