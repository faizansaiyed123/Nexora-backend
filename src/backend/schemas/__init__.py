"""
Central schemas registry for Nexora Price Intelligence Platform.
Exports all Pydantic v2 DTOs for request/response serialization across API endpoints.
"""

from backend.schemas.alert import (
    AlertLogRead,
    AlertRuleCreate,
    AlertRuleRead,
    AlertRuleUpdate,
)
from backend.schemas.audit import AuditLogRead
from backend.schemas.client import (
    ClientCreate,
    ClientRead,
    ClientUpdate,
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
    # Offering & Dynamic Fields
    "DynamicFieldDefinitionCreate",
    "DynamicFieldDefinitionRead",
    "OfferingCreate",
    "OfferingUpdate",
    "OfferingRead",
    "OfferingFilterParams",
    # Competitor, Source & Match
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
    # Observation, Jobs & Snapshots
    "JobCreate",
    "JobRead",
    "ObservationIngest",
    "ObservationRead",
    "SnapshotRead",
    "PriceHistoryPoint",
    "PriceTrendResponse",
    # Alerts
    "AlertRuleCreate",
    "AlertRuleUpdate",
    "AlertRuleRead",
    "AlertLogRead",
    # Audit
    "AuditLogRead",
]
