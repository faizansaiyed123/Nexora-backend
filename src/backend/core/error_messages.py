"""Safe public error messages for external collection operations."""

from __future__ import annotations

import httpx

from backend.services.url_security import SecurityValidationError


def safe_collection_error(exc: BaseException) -> str:
    """Return a stable tenant-visible message without leaking internals."""
    if isinstance(exc, SecurityValidationError):
        return "Target URL was blocked by the security policy."
    if isinstance(exc, httpx.TimeoutException):
        return "Target request timed out."
    if isinstance(exc, (httpx.ConnectError, httpx.ReadError, httpx.WriteError, httpx.RemoteProtocolError)):
        return "Target could not be reached safely."
    return "External collection failed unexpectedly."
