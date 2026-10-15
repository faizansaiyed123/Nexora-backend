"""Small loopback CONNECT/HTTP proxy for Playwright SSRF enforcement.

The browser keeps the original hostname for TLS/SNI while this proxy resolves,
validates, and connects to the exact public IP chosen for that connection.

When a residential proxy URL is configured, outbound connections are tunneled
through that upstream proxy instead of connecting directly. The destination
hostname is still validated by UrlSecurityService before the tunnel is opened,
so SSRF protection is preserved regardless of which egress IP is used.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional
from urllib.parse import urlsplit, urlparse

from backend.services.url_security import SecurityValidationError, UrlSecurityService

logger = logging.getLogger("nexora.browser_proxy")
_MAX_HEADERS = 64 * 1024
_CONNECT_TIMEOUT = 20.0


def _parse_upstream_proxy(proxy_url: str) -> tuple[str, int, Optional[str], Optional[str]]:
    """Parse a residential proxy URL into (host, port, username, password)."""
    parsed = urlparse(proxy_url)
    if not parsed.hostname or not parsed.port:
        raise SecurityValidationError("Invalid residential proxy URL: missing host or port.")
    username = parsed.username
    password = parsed.password
    return parsed.hostname, int(parsed.port), username, password


async def _resolve_and_validate_host_async(hostname: str, port: int) -> str:
    """Run the blocking SSRF DNS resolution in a thread so the event loop is not stalled."""
    return await asyncio.to_thread(UrlSecurityService.resolve_and_validate_host, hostname, port)


class SafeBrowserProxy:
    def __init__(self, residential_proxy_url: Optional[str] = None) -> None:
        self._server: asyncio.AbstractServer | None = None
        self.host = "127.0.0.1"
        self.port = 0
        self._residential_proxy_url = residential_proxy_url
        self._upstream_host: Optional[str] = None
        self._upstream_port: Optional[int] = None
        self._upstream_user: Optional[str] = None
        self._upstream_pass: Optional[str] = None
        if residential_proxy_url:
            (
                self._upstream_host,
                self._upstream_port,
                self._upstream_user,
                self._upstream_pass,
            ) = _parse_upstream_proxy(residential_proxy_url)

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle_client,
            host=self.host,
            port=0,
            limit=_MAX_HEADERS,
        )
        sockets = self._server.sockets or []
        if not sockets:
            raise RuntimeError("Could not allocate browser proxy socket.")
        self.port = int(sockets[0].getsockname()[1])

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        upstream_reader = upstream_writer = None
        try:
            header_block = await reader.readuntil(b"\r\n\r\n")
            if len(header_block) > _MAX_HEADERS:
                raise SecurityValidationError("Proxy request headers exceed the allowed size.")

            header_lines = header_block[:-4].split(b"\r\n")
            if not header_lines:
                raise SecurityValidationError("Malformed proxy request.")

            request_line = header_lines[0].decode("latin-1")
            parts = request_line.split(" ", 2)
            if len(parts) != 3:
                raise SecurityValidationError("Malformed proxy request line.")

            method, target, version = parts
            if method.upper() == "CONNECT":
                host, port = self._parse_connect_target(target)
                # Validate the destination hostname before we tunnel anywhere.
                pinned_ip = await _resolve_and_validate_host_async(host, port)
                if self._upstream_host and self._upstream_port:
                    # Tunnel through the configured residential proxy instead of
                    # connecting directly to the pinned IP. The pinned IP is
                    # still computed so SSRF validation cannot be bypassed.
                    upstream_reader, upstream_writer = await asyncio.wait_for(
                        asyncio.open_connection(self._upstream_host, self._upstream_port),
                        timeout=_CONNECT_TIMEOUT,
                    )
                    await self._send_proxy_connect(
                        upstream_writer, host, port, self._upstream_user, self._upstream_pass
                    )
                    response_line = await upstream_reader.readuntil(b"\r\n\r\n")
                    if not response_line.startswith(b"200"):
                        raise SecurityValidationError(
                            f"Residential proxy rejected CONNECT to {host}:{port}"
                        )
                else:
                    upstream_reader, upstream_writer = await asyncio.wait_for(
                        asyncio.open_connection(pinned_ip, port),
                        timeout=_CONNECT_TIMEOUT,
                    )
                writer.write(b"HTTP/1.1 200 Connection Established\r\nConnection: keep-alive\r\n\r\n")
                await writer.drain()
                await self._tunnel(reader, writer, upstream_reader, upstream_writer)
                return

            parsed = urlsplit(target)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise SecurityValidationError("Only absolute HTTP(S) proxy requests are permitted.")
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            pinned_ip = await _resolve_and_validate_host_async(parsed.hostname, port)
            if parsed.scheme == "https":
                raise SecurityValidationError("HTTPS requests must use CONNECT tunneling.")

            if self._upstream_host and self._upstream_port:
                upstream_reader, upstream_writer = await asyncio.wait_for(
                    asyncio.open_connection(self._upstream_host, self._upstream_port),
                    timeout=_CONNECT_TIMEOUT,
                )
                await self._send_proxy_http_request(
                    upstream_writer,
                    method,
                    target,
                    version,
                    header_lines,
                    self._upstream_user,
                    self._upstream_pass,
                )
                response_line = await upstream_reader.readuntil(b"\r\n\r\n")
                if not response_line.startswith(b"200"):
                    raise SecurityValidationError(
                        f"Residential proxy rejected {method} {target}"
                    )
            else:
                upstream_reader, upstream_writer = await asyncio.wait_for(
                    asyncio.open_connection(pinned_ip, port),
                    timeout=_CONNECT_TIMEOUT,
                )

            origin_target = parsed.path or "/"
            if parsed.query:
                origin_target += f"?{parsed.query}"
            outbound = [f"{method} {origin_target} {version}"]
            outbound.extend(line.decode("latin-1") for line in header_lines[1:])
            outbound.append("")
            outbound.append("")
            upstream_writer.write("\r\n".join(outbound).encode("latin-1"))

            content_length = 0
            for line in header_lines[1:]:
                lower = line.lower()
                if lower.startswith(b"content-length:"):
                    try:
                        content_length = int(line.split(b":", 1)[1].strip())
                    except ValueError:
                        content_length = 0
                    break
            if content_length:
                upstream_writer.write(await reader.readexactly(content_length))
            await upstream_writer.drain()

            while True:
                chunk = await upstream_reader.read(64 * 1024)
                if not chunk:
                    break
                writer.write(chunk)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError, TimeoutError, SecurityValidationError) as exc:
            logger.info("Browser proxy rejected request: %s", exc)
            try:
                writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                await writer.drain()
            except Exception:
                pass
        except Exception:
            logger.exception("Unexpected browser proxy failure.")
        finally:
            if upstream_writer is not None:
                upstream_writer.close()
                try:
                    await upstream_writer.wait_closed()
                except Exception:
                    pass
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    @staticmethod
    def _parse_connect_target(target: str) -> tuple[str, int]:
        if target.startswith("["):
            closing = target.find("]")
            if closing == -1:
                raise SecurityValidationError("Malformed CONNECT target.")
            host = target[1:closing]
            suffix = target[closing + 1 :]
            if not suffix.startswith(":"):
                raise SecurityValidationError("CONNECT target must include a port.")
            port = int(suffix[1:])
        else:
            host, separator, port_text = target.rpartition(":")
            if not separator:
                raise SecurityValidationError("CONNECT target must include a port.")
            port = int(port_text)
        if not (1 <= port <= 65535):
            raise SecurityValidationError("CONNECT port is invalid.")
        return host, port

    @staticmethod
    async def _send_proxy_connect(
        writer: asyncio.StreamWriter,
        host: str,
        port: int,
        username: Optional[str],
        password: Optional[str],
    ) -> None:
        """Send an HTTP CONNECT request to an upstream residential proxy."""
        request = [f"CONNECT {host}:{port} HTTP/1.1", f"Host: {host}:{port}"]
        if username and password:
            import base64

            token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
            request.append(f"Proxy-Authorization: Basic {token}")
        request.append("Proxy-Connection: keep-alive")
        request.append("")
        request.append("")
        writer.write(("\r\n".join(request)).encode("latin-1"))
        await writer.drain()

    @staticmethod
    async def _send_proxy_http_request(
        writer: asyncio.StreamWriter,
        method: str,
        target: str,
        version: str,
        header_lines: list[bytes],
        username: Optional[str],
        password: Optional[str],
    ) -> None:
        """Send an absolute-form HTTP request to an upstream residential proxy."""
        request = [f"{method} {target} {version}", f"Host: {urlsplit(target).hostname}"]
        if username and password:
            import base64

            token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
            request.append(f"Proxy-Authorization: Basic {token}")
        request.extend(line.decode("latin-1") for line in header_lines[1:])
        request.append("")
        request.append("")
        writer.write(("\r\n".join(request)).encode("latin-1"))
        await writer.drain()

    @staticmethod
    async def _tunnel(
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        upstream_reader: asyncio.StreamReader,
        upstream_writer: asyncio.StreamWriter,
    ) -> None:
        async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                while True:
                    data = await reader.read(64 * 1024)
                    if not data:
                        break
                    writer.write(data)
                    await writer.drain()
            except (ConnectionError, asyncio.IncompleteReadError):
                pass

        await asyncio.gather(
            pipe(client_reader, upstream_writer),
            pipe(upstream_reader, client_writer),
        )


@asynccontextmanager
async def browser_proxy(residential_proxy_url: Optional[str] = None) -> AsyncIterator[SafeBrowserProxy]:
    proxy = SafeBrowserProxy(residential_proxy_url=residential_proxy_url)
    await proxy.start()
    try:
        yield proxy
    finally:
        await proxy.close()
