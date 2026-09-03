"""
Base interfaces for the collection engine.

The collection engine is industry-independent.
It does not know whether the target is a product, hotel,
service, SaaS plan, rental, marketplace listing, etc.

Collectors are responsible only for retrieving a source.
Extraction and business logic happen in later stages.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class CollectionRequest:
    """
    Describes what needs to be collected from a source.
    """

    url: str

    timeout_seconds: int = 30

    headers: Dict[str, str] = field(default_factory=dict)

    requires_javascript: bool = False

    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class CollectionResponse:
    """
    Standardized result returned by any collector.

    HTTP and Playwright collectors will both return this same
    structure so the rest of the system does not care how the
    page was collected.
    """

    url: str

    status_code: Optional[int]

    content: str

    response_time_ms: int

    collection_method: str

    success: bool

    error: Optional[str] = None

    metadata: Dict[str, Any] = field(default_factory=dict)


class BaseCollector:
    """
    Abstract interface for all collection methods.

    Examples:
        HTTPCollector
        PlaywrightCollector
        Future collectors
    """

    name: str = "base"

    async def collect(
        self,
        request: CollectionRequest,
    ) -> CollectionResponse:
        """
        Collect content from the requested URL.

        Concrete collectors must implement this method.
        """
        raise NotImplementedError
