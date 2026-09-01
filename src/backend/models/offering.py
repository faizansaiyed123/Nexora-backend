"""
Offering models implementing the Fixed Core + JSONB Extensible Schema pattern.
Supports physical products, services, subscriptions, and custom SKU bundles.
"""

import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.db.base import Base
from backend.models.enums import CreatedViaEnum, OfferingTypeEnum

if TYPE_CHECKING:
    from backend.models.client import ClientModel
    from backend.models.competitor import OfferingMatchModel


class DynamicFieldDefinitionModel(Base):
    """
    Client-defined schema definitions for dynamic attributes.
    Enforces dynamic schema types (string, number, boolean, select, etc.).
    """
    __tablename__ = "dynamic_field_definitions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    client_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("clients.id", ondelete="CASCADE"), nullable=False, index=True
    )
    field_name: Mapped[str] = mapped_column(String(100), nullable=False)
    display_label: Mapped[str] = mapped_column(String(255), nullable=False)
    data_type: Mapped[str] = mapped_column(String(50), nullable=False)  # string, number, boolean, select, date
    unit_of_measure: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    is_required: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_comparable: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    options: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    # Relationships
    client: Mapped["ClientModel"] = relationship("ClientModel", back_populates="dynamic_fields")

    __table_args__ = (
        UniqueConstraint("client_id", "field_name", name="uq_client_field_name"),
    )


class OfferingModel(Base):
    """
    Core catalog offering (client product/service).
    Uses Fixed columns for critical indexed metadata + JSONB for flexible attributes.
    """
    __tablename__ = "offerings"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    client_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("clients.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(500), nullable=False, index=True)
    sku: Mapped[Optional[str]] = mapped_column(String(100), nullable=True, index=True)
    offering_type: Mapped[OfferingTypeEnum] = mapped_column(
        Enum(OfferingTypeEnum, name="offering_type_enum", native_enum=False),
        default=OfferingTypeEnum.PRODUCT,
        nullable=False,
        index=True,
    )
    brand: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, index=True)
    category_path: Mapped[Optional[str]] = mapped_column(String(500), nullable=True, index=True)
    base_price: Mapped[Optional[float]] = mapped_column(Numeric(14, 4), nullable=True)
    currency: Mapped[str] = mapped_column(String(3), default="USD", nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    source_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_via: Mapped[CreatedViaEnum] = mapped_column(
        Enum(CreatedViaEnum, name="created_via_enum", native_enum=False),
        default=CreatedViaEnum.MANUAL,
        nullable=False,
    )
    dynamic_attributes: Mapped[Dict[str, Any]] = mapped_column(
        JSONB, default=dict, nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)
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
    client: Mapped["ClientModel"] = relationship("ClientModel", back_populates="offerings")
    matches: Mapped[List["OfferingMatchModel"]] = relationship(
        "OfferingMatchModel", back_populates="offering", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("idx_offerings_client_sku", "client_id", "sku"),
        Index("idx_offerings_dynamic_attrs_gin", "dynamic_attributes", postgresql_using="gin"),
    )
