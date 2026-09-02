"""
Observation, Job, and Real-Time Snapshot models.
Maintains append-only price history, crawler execution audit, and fast dashboard cache.
"""

import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from sqlalchemy import (
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.db.base import Base
from backend.models.enums import (
    AvailabilityStatusEnum,
    ErrorCategoryEnum,
    JobStatusEnum,
    JobTypeEnum,
)

if TYPE_CHECKING:
    from backend.models.competitor import OfferingMatchModel, SourceModel


class JobModel(Base):
    """
    Scraper / crawler batch execution job.
    """
    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sources.id", ondelete="CASCADE"), nullable=False, index=True
    )
    job_type: Mapped[JobTypeEnum] = mapped_column(
        Enum(JobTypeEnum, name="job_type_enum", native_enum=False),
        default=JobTypeEnum.SCHEDULED_CRAWL,
        nullable=False,
    )
    status: Mapped[JobStatusEnum] = mapped_column(
        Enum(JobStatusEnum, name="job_status_enum", native_enum=False),
        default=JobStatusEnum.PENDING,
        nullable=False,
        index=True,
    )
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    total_items_processed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    successful_items: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failed_items: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_category: Mapped[Optional[ErrorCategoryEnum]] = mapped_column(
        Enum(ErrorCategoryEnum, name="error_category_enum", native_enum=False),
        nullable=True,
    )
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    meta_info: Mapped[Dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    # Relationships
    source: Mapped["SourceModel"] = relationship("SourceModel", back_populates="jobs")
    observations: Mapped[List["ObservationModel"]] = relationship(
        "ObservationModel", back_populates="job", cascade="all, delete-orphan"
    )


class ObservationModel(Base):
    """
    Append-only time-series point observation.
    """
    __tablename__ = "observations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    offering_match_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("offering_matches.id", ondelete="CASCADE"), nullable=False, index=True
    )
    job_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    observed_price: Mapped[Optional[float]] = mapped_column(Numeric(14, 4), nullable=True)
    currency: Mapped[str] = mapped_column(String(3), default="USD", nullable=False)
    availability: Mapped[AvailabilityStatusEnum] = mapped_column(
        Enum(AvailabilityStatusEnum, name="availability_status_enum", native_enum=False),
        default=AvailabilityStatusEnum.IN_STOCK,
        nullable=False,
    )
    response_time_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    http_status_code: Mapped[int] = mapped_column(Integer, default=200, nullable=False)
    raw_payload: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    extracted_attributes: Mapped[Dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
        index=True,
    )

    # Relationships
    offering_match: Mapped["OfferingMatchModel"] = relationship("OfferingMatchModel", back_populates="observations")
    job: Mapped[Optional["JobModel"]] = relationship("JobModel", back_populates="observations")

    __table_args__ = (
        Index("idx_observations_match_time", "offering_match_id", "observed_at"),
    )


class SnapshotModel(Base):
    """
    Latest consolidated state per offering match for ultra-fast queries.
    """
    __tablename__ = "snapshots"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    offering_match_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("offering_matches.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    current_price: Mapped[Optional[float]] = mapped_column(Numeric(14, 4), nullable=True)
    previous_price: Mapped[Optional[float]] = mapped_column(Numeric(14, 4), nullable=True)
    price_difference: Mapped[Optional[float]] = mapped_column(Numeric(14, 4), nullable=True)
    percentage_difference: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    current_availability: Mapped[AvailabilityStatusEnum] = mapped_column(
        Enum(AvailabilityStatusEnum, name="availability_status_enum", native_enum=False),
        default=AvailabilityStatusEnum.IN_STOCK,
        nullable=False,
    )
    last_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    # Relationship
    offering_match: Mapped["OfferingMatchModel"] = relationship("OfferingMatchModel", back_populates="snapshot")
