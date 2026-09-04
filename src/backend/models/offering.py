"""
Offering models for the Price Intelligence Platform.

Universal, vertical-agnostic architecture:
- Fixed Core fields for indexing and fast search/filtering
- Sanitized JSONB dynamic attributes for unlimited domain flexibility
- Dynamic Field Definitions with automatic schema discovery
"""

from decimal import Decimal
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
    Client-defined dynamic field schema definition.
    Tracks discovered and configured dynamic attribute metadata per tenant.
    """
    __tablename__ = "dynamic_field_definitions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    client_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("clients.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    field_name: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )

    display_name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )

    data_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default="STRING",
    )

    unit: Mapped[Optional[str]] = mapped_column(
        String(50),
        nullable=True,
    )

    category: Mapped[Optional[str]] = mapped_column(
        String(50),
        nullable=True,
        default="GENERAL",
    )

    description: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
    )

    is_selected: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
    )

    confidence: Mapped[str] = mapped_column(
        String(20),
        default="HIGH",
        nullable=False,
    )

    is_required: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
    )

    is_comparable: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
    )

    options: Mapped[Optional[Dict[str, Any]]] = mapped_column(
        JSONB,
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    # Relationships
    client: Mapped["ClientModel"] = relationship(
        "ClientModel",
        back_populates="dynamic_fields",
    )

    # Backward-compatible property aliases
    @property
    def display_label(self) -> str:
        return self.display_name

    @display_label.setter
    def display_label(self, val: str) -> None:
        self.display_name = val

    @property
    def unit_of_measure(self) -> Optional[str]:
        return self.unit

    @unit_of_measure.setter
    def unit_of_measure(self, val: Optional[str]) -> None:
        self.unit = val

    __table_args__ = (
        UniqueConstraint(
            "client_id",
            "field_name",
            name="uq_client_field_name",
        ),
    )


class OfferingModel(Base):
    """
    Canonical client offering.
    Represents any product, service, subscription/SaaS plan, room, flight, rental,
    course, bundle, or custom catalog offering.
    """
    __tablename__ = "offerings"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    client_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("clients.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Core identity
    name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        index=True,
    )

    offering_type: Mapped[OfferingTypeEnum] = mapped_column(
        Enum(
            OfferingTypeEnum,
            name="offering_type_enum",
            native_enum=False,
        ),
        default=OfferingTypeEnum.CUSTOM,
        nullable=False,
        index=True,
    )

    custom_type_name: Mapped[Optional[str]] = mapped_column(
        String(100),
        nullable=True,
    )

    sku: Mapped[Optional[str]] = mapped_column(
        String(100),
        nullable=True,
        index=True,
    )

    # Monetary value (Exact Decimal precision)
    current_price: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(12, 4),
        nullable=True,
    )

    currency: Mapped[str] = mapped_column(
        String(3),
        default="USD",
        nullable=False,
    )

    market: Mapped[str] = mapped_column(
        String(2),
        default="US",
        nullable=False,
    )

    # URLs & Images
    url: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
    )

    category: Mapped[Optional[str]] = mapped_column(
        String(100),
        nullable=True,
        index=True,
    )

    image_url: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
    )

    description: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
    )

    # Dynamic JSONB attributes (Sanitized & vertical-agnostic)
    attributes: Mapped[Dict[str, Any]] = mapped_column(
        JSONB,
        default=dict,
        nullable=False,
    )

    # Lifecycle & Monitoring flags
    is_archived: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
        index=True,
    )

    is_monitored: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
        index=True,
    )

    created_via: Mapped[CreatedViaEnum] = mapped_column(
        Enum(
            CreatedViaEnum,
            name="created_via_enum",
            native_enum=False,
        ),
        default=CreatedViaEnum.MANUAL,
        nullable=False,
    )

    # Timestamps
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
    client: Mapped["ClientModel"] = relationship(
        "ClientModel",
        back_populates="offerings",
    )

    matches: Mapped[List["OfferingMatchModel"]] = relationship(
        "OfferingMatchModel",
        back_populates="offering",
        cascade="all, delete-orphan",
    )

    # Backward-compatible property aliases
    @property
    def base_price(self) -> Optional[Decimal]:
        return self.current_price

    @base_price.setter
    def base_price(self, val: Optional[Decimal]) -> None:
        self.current_price = val

    @property
    def source_url(self) -> Optional[str]:
        return self.url

    @source_url.setter
    def source_url(self, val: Optional[str]) -> None:
        self.url = val

    @property
    def dynamic_attributes(self) -> Dict[str, Any]:
        return self.attributes

    @dynamic_attributes.setter
    def dynamic_attributes(self, val: Dict[str, Any]) -> None:
        self.attributes = val if isinstance(val, dict) else {}

    @property
    def identifiers(self) -> Dict[str, Any]:
        if isinstance(self.attributes, dict) and "identifiers" in self.attributes:
            return self.attributes["identifiers"]
        if self.sku:
            return {"sku": self.sku}
        return {}

    @identifiers.setter
    def identifiers(self, val: Dict[str, Any]) -> None:
        if isinstance(val, dict) and val:
            if not isinstance(self.attributes, dict):
                self.attributes = {}
            self.attributes["identifiers"] = val
            if "sku" in val and not self.sku:
                self.sku = str(val["sku"])

    @property
    def meta_info(self) -> Dict[str, Any]:
        if isinstance(self.attributes, dict) and "meta_info" in self.attributes:
            return self.attributes["meta_info"]
        return {}

    @meta_info.setter
    def meta_info(self, val: Dict[str, Any]) -> None:
        if isinstance(val, dict) and val:
            if not isinstance(self.attributes, dict):
                self.attributes = {}
            self.attributes["meta_info"] = val

    @property
    def is_active(self) -> bool:
        return not self.is_archived

    @is_active.setter
    def is_active(self, val: bool) -> None:
        self.is_archived = not val

    # Indexes
    __table_args__ = (
        Index(
            "idx_offerings_client_archived_type",
            "client_id",
            "is_archived",
            "offering_type",
        ),
        Index(
            "idx_offerings_client_monitored",
            "client_id",
            "is_monitored",
        ),
        Index(
            "idx_offerings_attributes_gin",
            "attributes",
            postgresql_using="gin",
        ),
    )
