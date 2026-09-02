"""
Central export registry for all Nexora Pydantic Schemas and DTOs.
"""

from backend.schemas.alert import (
    AlertLogRead,
    AlertRuleCreate,
    AlertRuleRead,
    AlertRuleUpdate,
)
from backend.schemas.audit import AuditLogRead
from backend.schemas.client import (
    ChangePasswordRequest,
    ClientCreate,
    ClientRead,
    ClientUpdate,
    CurrentUserResponse,
    ForgotPasswordRequest,
    LoginRequest,
    LogoutRequest,
    MessageResponse,
    RefreshTokenRequest,
    RegisterRequest,
    ResendVerificationRequest,
    ResetPasswordRequest,
    TokenPayload,
    TokenResponse,
    UserCreate,
    UserRead,
    UserUpdate,
)
from backend.schemas.competitor import (
    CompetitorCreate,
    CompetitorRead,
    CompetitorUpdate,
    OfferingMatchCreate,
    OfferingMatchRead,
    OfferingMatchUpdate,
    SourceConfigurationCreate,
    SourceConfigurationRead,
    SourceConfigurationUpdate,
    SourceCreate,
    SourceRead,
    SourceUpdate,
)
from backend.schemas.errors import ErrorDetail, ErrorResponse
from backend.schemas.observation import (
    JobCreate,
    JobRead,
    ObservationIngest,
    ObservationRead,
    PriceHistoryPoint,
    PriceTrendResponse,
    SnapshotRead,
)
from backend.schemas.offering import (
    DynamicFieldDefinitionCreate,
    DynamicFieldDefinitionRead,
    OfferingCreate,
    OfferingFilterParams,
    OfferingRead,
    OfferingUpdate,
)

__all__ = [
    # Errors
    "ErrorDetail",
    "ErrorResponse",
    # Client & Auth
    "ClientCreate",
    "ClientUpdate",
    "ClientRead",
    "UserCreate",
    "UserUpdate",
    "UserRead",
    "TokenResponse",
    "TokenPayload",
    "RegisterRequest",
    "LoginRequest",
    "RefreshTokenRequest",
    "LogoutRequest",
    "ForgotPasswordRequest",
    "ResetPasswordRequest",
    "ChangePasswordRequest",
    "ResendVerificationRequest",
    "MessageResponse",
    "CurrentUserResponse",
    # Offering Catalog
    "DynamicFieldDefinitionCreate",
    "DynamicFieldDefinitionRead",
    "OfferingCreate",
    "OfferingUpdate",
    "OfferingRead",
    "OfferingFilterParams",
    # Competitor & Sources
    "CompetitorCreate",
    "CompetitorUpdate",
    "CompetitorRead",
    "SourceConfigurationCreate",
    "SourceConfigurationUpdate",
    "SourceConfigurationRead",
    "SourceCreate",
    "SourceUpdate",
    "SourceRead",
    "OfferingMatchCreate",
    "OfferingMatchUpdate",
    "OfferingMatchRead",
    # Observation & Ingestion
    "JobCreate",
    "JobRead",
    "ObservationIngest",
    "ObservationRead",
    "SnapshotRead",
    "PriceHistoryPoint",
    "PriceTrendResponse",
    # Alerts & Audit
    "AlertRuleCreate",
    "AlertRuleUpdate",
    "AlertRuleRead",
    "AlertLogRead",
    "AuditLogRead",
]
