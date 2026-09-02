"""
Client and User models supporting multi-tenant isolation and role-based access.
"""

import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, List

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.db.base import Base
from backend.models.enums import ClientStatusEnum, RoleEnum

if TYPE_CHECKING:
    from backend.models.offering import DynamicFieldDefinitionModel, OfferingModel
    from backend.models.competitor import CompetitorModel


class ClientModel(Base):
    """
    Tenant organization owning offerings, competitors, alert rules, and audit logs.
    """
    __tablename__ = "clients"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    status: Mapped[ClientStatusEnum] = mapped_column(
        Enum(ClientStatusEnum, name="client_status_enum", native_enum=False),
        default=ClientStatusEnum.ACTIVE,
        nullable=False,
    )
    max_competitors: Mapped[int] = mapped_column(Integer, default=10, nullable=False)
    max_tracked_offerings: Mapped[int] = mapped_column(Integer, default=500, nullable=False)
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
    users: Mapped[List["UserModel"]] = relationship(
        "UserModel", back_populates="client", cascade="all, delete-orphan"
    )
    offerings: Mapped[List["OfferingModel"]] = relationship(
        "OfferingModel", back_populates="client", cascade="all, delete-orphan"
    )
    competitors: Mapped[List["CompetitorModel"]] = relationship(
        "CompetitorModel", back_populates="client", cascade="all, delete-orphan"
    )
    dynamic_fields: Mapped[List["DynamicFieldDefinitionModel"]] = relationship(
        "DynamicFieldDefinitionModel", back_populates="client", cascade="all, delete-orphan"
    )


class UserModel(Base):
    """
    User accounts scoped to tenant clients with strict RBAC roles.
    """
    __tablename__ = "users"

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

    email: Mapped[str] = mapped_column(
        String(255),
        unique=True,
        nullable=False,
        index=True,
    )

    hashed_password: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )

    full_name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )

    role: Mapped[RoleEnum] = mapped_column(
        Enum(RoleEnum, name="role_enum", native_enum=False),
        default=RoleEnum.ANALYST,
        nullable=False,
    )

    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
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

    # Relationship
    client: Mapped["ClientModel"] = relationship(
        "ClientModel",
        back_populates="users",
    )
