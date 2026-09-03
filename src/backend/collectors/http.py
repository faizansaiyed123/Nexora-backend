"""
HTTP-based source collector.

Used for sources that can be collected without JavaScript rendering.
"""

import time

import httpx

from backend.collectors.base import (
    BaseCollector,
    CollectionRequest,
    CollectionResponse,
)


class HTTPCollector(BaseCollector):
    """
    Collects web pages using standard HTTP requests.

    This collector does not perform extraction.
    It only retrieves the source content.
    """

    name = "HTTP"

    async def collect(
        self,
        request: CollectionRequest,
    ) -> CollectionResponse:
        start_time = time.perf_counter()

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            "Accept": (
                "text/html,application/xhtml+xml,"
                "application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        }

        # Source-specific headers override defaults.
        headers.update(request.headers)

        try:
            timeout = httpx.Timeout(
                request.timeout_seconds,
                connect=request.timeout_seconds,
            )

            async with httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=True,
                headers=headers,
            ) as client:

                response = await client.get(request.url)

            elapsed_ms = int(
                (time.perf_counter() - start_time) * 1000
            )

            return CollectionResponse(
                url=str(response.url),
                status_code=response.status_code,
                content=response.text,
                response_time_ms=elapsed_ms,
                collection_method=self.name,
                success=response.is_success,
                metadata={
                    "content_type": response.headers.get("content-type"),
                    "final_url": str(response.url),
                },
            )

        except httpx.TimeoutException as exc:
            elapsed_ms = int(
                (time.perf_counter() - start_time) * 1000
            )

            return CollectionResponse(
                url=request.url,
                status_code=None,
                content="",
                response_time_ms=elapsed_ms,
                collection_method=self.name,
                success=False,
                error=f"TIMEOUT: {exc}",
            )

        except httpx.RequestError as exc:
            elapsed_ms = int(
                (time.perf_counter() - start_time) * 1000
            )

            return CollectionResponse(
                url=request.url,
                status_code=None,
                content="",
                response_time_ms=elapsed_ms,
                collection_method=self.name,
                success=False,
                error=f"REQUEST_ERROR: {exc}",
            )

        except Exception as exc:
            elapsed_ms = int(
                (time.perf_counter() - start_time) * 1000
            )

            return CollectionResponse(
                url=request.url,
                status_code=None,
                content="",
                response_time_ms=elapsed_ms,
                collection_method=self.name,
                success=False,
                error=f"UNEXPECTED_ERROR: {exc}",
            )
