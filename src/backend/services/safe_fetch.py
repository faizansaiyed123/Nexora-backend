"""SSRF-safe HTTP fetching.

UrlSecurityService.validate_url only checks the URL it is given. If the HTTP
client follows redirects on its own, a public page can bounce the request to an
internal address. safe_get follows redirects manually and re-validates every
hop, and it caps the response size so a hostile server cannot exhaust memory.

Known residual risk: DNS is resolved once for validation and again by the HTTP
client (DNS rebinding). Closing that fully needs connection-level IP pinning or
an egress proxy/firewall; deploy workers on a network that cannot reach private
ranges as defense in depth.
"""

import asyncio
from dataclasses import dataclass
from urllib.parse import urljoin

import httpx

from backend.services.url_security import SecurityValidationError, UrlSecurityService

MAX_REDIRECTS = 5
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}

@dataclass
class SafeResponse:
    status_code: int
    headers: httpx.Headers
    text: str
    url: str

async def safe_get(client: httpx.AsyncClient, url: str, *, max_redirects: int = MAX_REDIRECTS, max_bytes: int = MAX_RESPONSE_BYTES) -> SafeResponse:
    current = url
    for _ in range(max_redirects + 1):
        current = await asyncio.to_thread(UrlSecurityService.validate_url, current, False)
        async with client.stream("GET", current) as response:
            if response.status_code in _REDIRECT_STATUSES and response.headers.get("location"):
                current = urljoin(current, response.headers["location"])
                continue
            declared = response.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > max_bytes:
                raise SecurityValidationError("Response body exceeds the maximum allowed size.")
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > max_bytes:
                    raise SecurityValidationError("Response body exceeds the maximum allowed size.")
            encoding = response.encoding or "utf-8"
            return SafeResponse(status_code=response.status_code, headers=response.headers, text=body.decode(encoding, errors="replace"), url=current)
    raise SecurityValidationError("Too many redirects.")
