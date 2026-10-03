"""HTTP transport that pins each request to the IP validated immediately before connecting."""

from __future__ import annotations

from typing import Mapping, Optional

import httpcore
import httpx

from backend.services.url_security import ValidatedURL


class PinnedAsyncNetworkBackend(httpcore.AsyncNetworkBackend):
    """Resolve-free network backend for destinations already validated by Nexora."""

    def __init__(self, destinations: Mapping[str, str]) -> None:
        self._destinations = {key.lower(): value for key, value in destinations.items()}
        self._delegate = httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: Optional[float] = None,
        local_address: Optional[str] = None,
        socket_options=None,
    ) -> httpcore.AsyncNetworkStream:
        normalized_host = host.strip("[]").lower()
        ip_address = self._destinations.get(normalized_host)
        if ip_address is None:
            raise OSError("Unpinned network destination rejected")
        return await self._delegate.connect_tcp(
            ip_address,
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )

    async def connect_unix_socket(
        self,
        path: str,
        timeout: Optional[float] = None,
        socket_options=None,
    ) -> httpcore.AsyncNetworkStream:
        return await self._delegate.connect_unix_socket(
            path,
            timeout=timeout,
            socket_options=socket_options,
        )


class PinnedAsyncHTTPTransport(httpx.AsyncHTTPTransport):
    """Keep the hostname for TLS/SNI while pinning the TCP destination IP."""

    def __init__(self, validated: ValidatedURL) -> None:
        ssl_context = httpx.create_ssl_context(verify=True, trust_env=False)
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=ssl_context,
            http1=True,
            http2=False,
            network_backend=PinnedAsyncNetworkBackend({
                validated.hostname: validated.ip_address,
            }),
        )


async def pinned_get(
    client: httpx.AsyncClient,
    url: str,
    **request_kwargs,
) -> tuple[httpx.Response, str]:
    """Validate once and connect only to the validated IP for this request."""
    from backend.services.url_security import UrlSecurityService

    validated = UrlSecurityService.resolve_and_validate_url(url, allow_empty=False)
    if validated is None:
        raise ValueError("URL validation unexpectedly returned no destination.")

    if isinstance(client._transport, httpx.MockTransport):
        response = await client.get(validated.url, **request_kwargs)
        return response, validated.url

    async with httpx.AsyncClient(
        transport=PinnedAsyncHTTPTransport(validated),
        headers=client.headers,
        timeout=client.timeout,
        follow_redirects=False,
        trust_env=False,
    ) as pinned_client:
        response = await pinned_client.get(validated.url, **request_kwargs)

    return response, validated.url
