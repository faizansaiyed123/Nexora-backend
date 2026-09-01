"""
Declarative base class for all SQLAlchemy 2.0 models in Nexora.
"""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Base class that maintains the catalog of mapped tables and metadata."""
    pass
