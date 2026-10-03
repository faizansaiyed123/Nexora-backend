"""SSRF-safe HTTP fetching with per-hop URL validation and a response-size cap."""

from urllib.parse import urljoin

import httpx

from backend.services.url_security import SecurityValidationError, UrlSecurityService

MAX_REDIRECTS = 5
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}

class SafeResponse:
    def __init__(self, *, response: httpx.Response, url: str):
        self.status_code = response.status_code
        self.headers = response.headers
        self.text = response.text
        self.url = url

async def safe_get(
    client: httpx.AsyncClient,
    url: str,
    *,
    max_redirects: int = MAX_REDIRECTS,
    max_bytes: int = MAX_RESPONSE_BYTES,
) -> SafeResponse:
    """GET a URL with manual, per-hop redirect validation.

    The caller must construct the client with ``follow_redirects=False``.
    ``AsyncClient.get`` is intentionally used so existing collection tests and
    HTTP transport adapters continue to observe the same request seam.
    """
    current = url
    for _ in range(max_redirects + 1):
        current = UrlSecurityService.validate_url(current, allow_empty=False)
        response = await client.get(current)

        if response.status_code in _REDIRECT_STATUSES and response.headers.get("location"):
            current = urljoin(current, response.headers["location"])
            continue

        declared = response.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > max_bytes:
            raise SecurityValidationError("Response body exceeds the maximum allowed size.")

        body = response.content
        if len(body) > max_bytes:
            raise SecurityValidationError("Response body exceeds the maximum allowed size.")

        return SafeResponse(response=response, url=current)

    raise SecurityValidationError("Too many redirects.")