"""
Central models registry for Nexora Price Intelligence Platform.
Imports all models so SQLAlchemy metadata registers every table automatically for Alembic migrations.
"""

from backend.models.alert import AlertLogModel, AlertRuleModel
from backend.models.audit import AuditLogModel
from backend.models.client import ClientModel, UserModel
from backend.models.competitor import (
    CompetitorModel,
    OfferingMatchModel,
    SourceConfigurationModel,
    SourceModel,
)
from backend.models.enums import (
    AlertTypeEnum,
    AvailabilityStatusEnum,
    CircuitStateEnum,
    ClientStatusEnum,
    CollectionMethodEnum,
    CompetitiveEventTypeEnum,
    CompetitorStatusEnum,
    CreatedViaEnum,
    ErrorCategoryEnum,
    HealthStatusEnum,
    JobStatusEnum,
    JobTypeEnum,
    MatchStatusEnum,
    OfferingTypeEnum,
    RoleEnum,
    SourceTypeEnum,
    VerificationStatusEnum,
)
from backend.models.observation import JobModel, ObservationModel, SnapshotModel
from backend.models.notification import NotificationOutboxModel
from backend.models.offering import DynamicFieldDefinitionModel, OfferingModel

__all__ = [
    "RoleEnum",
    "ClientStatusEnum",
    "OfferingTypeEnum",
    "CreatedViaEnum",
    "CompetitorStatusEnum",
    "SourceTypeEnum",
    "VerificationStatusEnum",
    "HealthStatusEnum",
    "CollectionMethodEnum",
    "CircuitStateEnum",
    "JobTypeEnum",
    "JobStatusEnum",
    "ErrorCategoryEnum",
    "AvailabilityStatusEnum",
    "MatchStatusEnum",
    "CompetitiveEventTypeEnum",
    "AlertTypeEnum",
    "ClientModel",
    "UserModel",
    "OfferingModel",
    "DynamicFieldDefinitionModel",
    "CompetitorModel",
    "SourceModel",
    "SourceConfigurationModel",
    "OfferingMatchModel",
    "JobModel",
    "ObservationModel",
    "SnapshotModel",
    "AlertRuleModel",
    "AlertLogModel",
    "NotificationOutboxModel",
    "AuditLogModel",
]
