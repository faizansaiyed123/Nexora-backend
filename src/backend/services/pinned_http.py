"""Connection-time SSRF enforcement for HTTPX.

The HTTP client's TLS/SNI target remains the original hostname, while the TCP
connection target is pinned to the public IP selected by the security resolver.
This closes the DNS time-of-check/time-of-use gap without weakening certificate
verification.
"""

from __future__ import annotations

import asyncio
import socket
from typing import Iterable

import httpcore
import httpx

from backend.services.url_security import SecurityValidationError, UrlSecurityService


class PinnedDNSBackend(httpcore.AsyncNetworkBackend):
    """Resolve and validate a hostname immediately before opening its socket."""

    def __init__(self) -> None:
        self._backend = httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[tuple] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        if not host:
            raise SecurityValidationError("Outbound request hostname is missing.")

        try:
            pinned_ip = UrlSecurityService.resolve_and_validate_host(host, port)
        except SecurityValidationError:
            raise

        # The origin hostname is retained by httpcore for TLS SNI/certificate
        # verification. Only the actual TCP destination is replaced.
        return await self._backend.connect_tcp(
            pinned_ip,
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )


class PinnedDNSHTTPTransport(httpx.AsyncHTTPTransport):
    """HTTPX transport that binds every new TCP connection to a validated IP."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        pool = self._pool
        if not isinstance(pool, httpcore.AsyncConnectionPool):  # pragma: no cover
            raise RuntimeError("PinnedDNSHTTPTransport requires direct HTTP connections.")
        pool._network_backend = PinnedDNSBackend()
