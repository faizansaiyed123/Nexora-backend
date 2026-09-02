"""
Competitor, Source, and Offering Match models.
Includes Circuit Breaker state tracking and anti-ban scraping parameters per source.
"""

import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.db.base import Base
from backend.models.enums import (
    CircuitStateEnum,
    CollectionMethodEnum,
    CompetitorStatusEnum,
    HealthStatusEnum,
    MatchStatusEnum,
    SourceTypeEnum,
    VerificationStatusEnum,
)

if TYPE_CHECKING:
    from backend.models.client import ClientModel
    from backend.models.offering import OfferingModel
    from backend.models.observation import JobModel, ObservationModel, SnapshotModel


class CompetitorModel(Base):
    """
    Competitor entity tracked under a client/tenant organization.
    """
    __tablename__ = "competitors"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    client_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("clients.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    domain: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    status: Mapped[CompetitorStatusEnum] = mapped_column(
        Enum(CompetitorStatusEnum, name="competitor_status_enum", native_enum=False),
        default=CompetitorStatusEnum.ACTIVE,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    # Relationships
    client: Mapped["ClientModel"] = relationship("ClientModel", back_populates="competitors")
    sources: Mapped[List["SourceModel"]] = relationship(
        "SourceModel", back_populates="competitor", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("client_id", "domain", name="uq_client_competitor_domain"),
    )


class SourceModel(Base):
    """
    Specific data source / channel (e.g. competitor website or marketplace).
    Maintains autonomous Circuit Breaker health state.
    """
    __tablename__ = "sources"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    competitor_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("competitors.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    base_url: Mapped[str] = mapped_column(Text, nullable=False)
    source_type: Mapped[SourceTypeEnum] = mapped_column(
        Enum(SourceTypeEnum, name="source_type_enum", native_enum=False),
        default=SourceTypeEnum.OFFICIAL_STORE,
        nullable=False,
    )
    collection_method: Mapped[CollectionMethodEnum] = mapped_column(
        Enum(CollectionMethodEnum, name="collection_method_enum", native_enum=False),
        default=CollectionMethodEnum.HTTP_FAST,
        nullable=False,
    )
    verification_status: Mapped[VerificationStatusEnum] = mapped_column(
        Enum(VerificationStatusEnum, name="verification_status_enum", native_enum=False),
        default=VerificationStatusEnum.UNVERIFIED,
        nullable=False,
    )

    # Circuit Breaker state
    circuit_state: Mapped[CircuitStateEnum] = mapped_column(
        Enum(CircuitStateEnum, name="circuit_state_enum", native_enum=False),
        default=CircuitStateEnum.CLOSED,
        nullable=False,
    )
    failure_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    consecutive_successes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    circuit_opened_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    circuit_half_opened_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    health_status: Mapped[HealthStatusEnum] = mapped_column(
        Enum(HealthStatusEnum, name="health_status_enum", native_enum=False),
        default=HealthStatusEnum.HEALTHY,
        nullable=False,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    # Relationships
    competitor: Mapped["CompetitorModel"] = relationship("CompetitorModel", back_populates="sources")
    configuration: Mapped[Optional["SourceConfigurationModel"]] = relationship(
        "SourceConfigurationModel", back_populates="source", uselist=False, cascade="all, delete-orphan"
    )
    matches: Mapped[List["OfferingMatchModel"]] = relationship(
        "OfferingMatchModel", back_populates="source", cascade="all, delete-orphan"
    )
    jobs: Mapped[List["JobModel"]] = relationship(
        "JobModel", back_populates="source", cascade="all, delete-orphan"
    )


class SourceConfigurationModel(Base):
    """
    Detailed extraction and scraping configuration per source.
    """
    __tablename__ = "source_configurations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sources.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    rate_limit_rpm: Mapped[int] = mapped_column(Integer, default=30, nullable=False)
    request_delay_seconds: Mapped[float] = mapped_column(Float, default=2.0, nullable=False)
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=30, nullable=False)
    max_retries: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    user_agent_strategy: Mapped[str] = mapped_column(String(50), default="ROTATE_CHROME", nullable=False)
    requires_proxy: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    requires_javascript: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    custom_headers: Mapped[Dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    extraction_selectors: Mapped[Dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    # Relationship
    source: Mapped["SourceModel"] = relationship("SourceModel", back_populates="configuration")


class OfferingMatchModel(Base):
    """
    Mapping between a client Offering and a specific competitor Source offering URL.
    """
    __tablename__ = "offering_matches"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    offering_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("offerings.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sources.id", ondelete="CASCADE"), nullable=False, index=True
    )
    target_url: Mapped[str] = mapped_column(Text, nullable=False)
    match_status: Mapped[MatchStatusEnum] = mapped_column(
        Enum(MatchStatusEnum, name="match_status_enum", native_enum=False),
        default=MatchStatusEnum.APPROVED,
        nullable=False,
    )
    confidence_score: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    extraction_selectors: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    # Relationships
    offering: Mapped["OfferingModel"] = relationship("OfferingModel", back_populates="matches")
    source: Mapped["SourceModel"] = relationship("SourceModel", back_populates="matches")
    observations: Mapped[List["ObservationModel"]] = relationship(
        "ObservationModel", back_populates="offering_match", cascade="all, delete-orphan"
    )
    snapshot: Mapped[Optional["SnapshotModel"]] = relationship(
        "SnapshotModel", back_populates="offering_match", uselist=False, cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("offering_id", "source_id", "target_url", name="uq_offering_source_url"),
    )
