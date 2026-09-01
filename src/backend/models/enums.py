"""
Domain Enums for Nexora Price Intelligence Platform.
Defines all status codes, circuit states, role boundaries, and strategy types.
"""

from enum import Enum


class RoleEnum(str, Enum):
    SUPER_ADMIN = "SUPER_ADMIN"
    ORG_ADMIN = "ORG_ADMIN"
    ANALYST = "ANALYST"
    VIEWER = "VIEWER"


class ClientStatusEnum(str, Enum):
    ACTIVE = "ACTIVE"
    TRIAL = "TRIAL"
    SUSPENDED = "SUSPENDED"
    CANCELLED = "CANCELLED"


class OfferingTypeEnum(str, Enum):
    PRODUCT = "PRODUCT"
    SERVICE = "SERVICE"
    SUBSCRIPTION = "SUBSCRIPTION"
    SKU_BUNDLE = "SKU_BUNDLE"
    CUSTOM = "CUSTOM"


class CreatedViaEnum(str, Enum):
    MANUAL = "MANUAL"
    CSV_IMPORT = "CSV_IMPORT"
    URL_DISCOVERY = "URL_DISCOVERY"
    API_SYNC = "API_SYNC"


class CompetitorStatusEnum(str, Enum):
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    ARCHIVED = "ARCHIVED"


class SourceTypeEnum(str, Enum):
    OFFICIAL_STORE = "OFFICIAL_STORE"
    MARKETPLACE = "MARKETPLACE"
    WHOLESALER = "WHOLESALER"
    AGGREGATOR = "AGGREGATOR"
    RESELLER = "RESELLER"


class VerificationStatusEnum(str, Enum):
    UNVERIFIED = "UNVERIFIED"
    PENDING_VERIFICATION = "PENDING_VERIFICATION"
    VERIFIED = "VERIFIED"
    BLOCKED = "BLOCKED"


class HealthStatusEnum(str, Enum):
    HEALTHY = "HEALTHY"
    WARNING = "WARNING"
    FAILING = "FAILING"
    UNRESPONSIVE = "UNRESPONSIVE"


class CollectionMethodEnum(str, Enum):
    HTTP_FAST = "HTTP_FAST"
    PLAYWRIGHT_BROWSER = "PLAYWRIGHT_BROWSER"
    PROXY_ROTATED = "PROXY_ROTATED"
    API_FEED = "API_FEED"


class CircuitStateEnum(str, Enum):
    CLOSED = "CLOSED"         # Normal operation: all traffic allowed
    OPEN = "OPEN"             # Tripped: collection suspended to protect targets
    HALF_OPEN = "HALF_OPEN"   # Probing: test single request before reopening


class JobTypeEnum(str, Enum):
    SCHEDULED_CRAWL = "SCHEDULED_CRAWL"
    ON_DEMAND_REFRESH = "ON_DEMAND_REFRESH"
    HEALTH_CHECK = "HEALTH_CHECK"
    SCHEMA_DISCOVERY = "SCHEMA_DISCOVERY"


class JobStatusEnum(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class ErrorCategoryEnum(str, Enum):
    NETWORK_TIMEOUT = "NETWORK_TIMEOUT"
    ANTI_BOT_BLOCKED = "ANTI_BOT_BLOCKED"
    DOM_SELECTOR_DRIFT = "DOM_SELECTOR_DRIFT"
    HTTP_4XX = "HTTP_4XX"
    HTTP_5XX = "HTTP_5XX"
    PARSING_ERROR = "PARSING_ERROR"
    CIRCUIT_BREAKER_OPEN = "CIRCUIT_BREAKER_OPEN"


class AvailabilityStatusEnum(str, Enum):
    IN_STOCK = "IN_STOCK"
    OUT_OF_STOCK = "OUT_OF_STOCK"
    PREORDER = "PREORDER"
    BACKORDER = "BACKORDER"
    DISCONTINUED = "DISCONTINUED"
    UNKNOWN = "UNKNOWN"


class MatchStatusEnum(str, Enum):
    SUGGESTED = "SUGGESTED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    ARCHIVED = "ARCHIVED"


class CompetitiveEventTypeEnum(str, Enum):
    PRICE_INCREASE = "PRICE_INCREASE"
    PRICE_DECREASE = "PRICE_DECREASE"
    OUT_OF_STOCK = "OUT_OF_STOCK"
    BACK_IN_STOCK = "BACK_IN_STOCK"
    NEW_OFFERING_DETECTED = "NEW_OFFERING_DETECTED"
    DISCONTINUED = "DISCONTINUED"


class AlertTypeEnum(str, Enum):
    PRICE_CHANGE = "PRICE_CHANGE"
    PERCENTAGE_DROP = "PERCENTAGE_DROP"
    UNDERCUT_THRESHOLD = "UNDERCUT_THRESHOLD"
    STOCK_CHANGE = "STOCK_CHANGE"
    CIRCUIT_BREAKER_TRIPPED = "CIRCUIT_BREAKER_TRIPPED"
