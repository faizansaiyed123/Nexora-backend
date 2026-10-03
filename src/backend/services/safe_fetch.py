"""SSRF-safe HTTP fetching with per-hop destination pinning and a response-size cap."""

from urllib.parse import urljoin

import httpx

from backend.services.pinned_http import pinned_get
from backend.services.url_security import SecurityValidationError

MAX_REDIRECTS = 5
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}


class SafeResponse:
    def __init__(self, *, response: httpx.Response, url: str):
        self.status_code = response.status_code
        self.headers = response.headers
        self.text = response.text if isinstance(response.text, str) else str(response.text)
        self.content = response.content
        self.url = url


async def safe_get(
    client: httpx.AsyncClient,
    url: str,
    *,
    max_redirects: int = MAX_REDIRECTS,
    max_bytes: int = MAX_RESPONSE_BYTES,
) -> SafeResponse:
    """GET with per-hop URL validation and connection-time IP pinning."""
    current = url
    for _ in range(max_redirects + 1):
        response, validated_url = await pinned_get(client, current)
        current = validated_url
        if response.status_code in _REDIRECT_STATUSES and response.headers.get("location"):
            current = urljoin(current, response.headers["location"])
            continue
        declared = response.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > max_bytes:
            raise SecurityValidationError("Response body exceeds the maximum allowed size.")
        if len(response.content) > max_bytes:
            raise SecurityValidationError("Response body exceeds the maximum allowed size.")
        return SafeResponse(response=response, url=current)

    raise SecurityValidationError("Too many redirects.")
