"""Small loopback CONNECT/HTTP proxy for Playwright SSRF enforcement.

The browser keeps the original hostname for TLS/SNI while this proxy resolves,
validates, and connects to the exact public IP chosen for that connection.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator
from urllib.parse import urlsplit

from backend.services.url_security import SecurityValidationError, UrlSecurityService

logger = logging.getLogger("nexora.browser_proxy")
_MAX_HEADERS = 64 * 1024
_CONNECT_TIMEOUT = 20.0


class SafeBrowserProxy:
    def __init__(self) -> None:
        self._server: asyncio.AbstractServer | None = None
        self.host = "127.0.0.1"
        self.port = 0

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
                pinned_ip = UrlSecurityService.resolve_and_validate_host(host, port)
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
            pinned_ip = UrlSecurityService.resolve_and_validate_host(parsed.hostname, port)
            if parsed.scheme == "https":
                raise SecurityValidationError("HTTPS requests must use CONNECT tunneling.")

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
async def browser_proxy() -> AsyncIterator[SafeBrowserProxy]:
    proxy = SafeBrowserProxy()
    await proxy.start()
    try:
        yield proxy
    finally:
        await proxy.close()
