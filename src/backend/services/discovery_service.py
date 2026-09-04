"""
Website Discovery Service Layer.
Implements the autonomous crawl and catalog extraction pipeline:
- Validates target website URL with SSRF protection via UrlSecurityService
- Ensures client catalog Source/Job entity exists without data collision
- Crawls client website (homepage, sitemap, category/product links up to max_pages) safely
- Extracts offerings via UniversalExtractor across JSON-LD, OpenGraph, Embedded SPA state, and semantic HTML
- De-duplicates offerings by URL and SKU
- Updates existing offerings or creates new offerings with CreatedViaEnum.WEBSITE_DISCOVERY
- Enforces plan limits and auto-discovers dynamic fields
- Records Job execution audit state in JobModel
"""

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
import logging
import re
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urljoin, urlparse
import uuid

import httpx
from fastapi import HTTPException, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.client import ClientModel
from backend.models.competitor import CompetitorModel, SourceModel
from backend.models.enums import (
    CompetitorStatusEnum,
    CreatedViaEnum,
    ErrorCategoryEnum,
    JobStatusEnum,
    JobTypeEnum,
    OfferingTypeEnum,
    SourceTypeEnum,
)
from backend.models.observation import JobModel
from backend.models.offering import OfferingModel
from backend.schemas.discovery import (
    DiscoveredItemSummary,
    DiscoveryJobResponse,
    DiscoveryRunRequest,
)
from backend.schemas.offering import OfferingCreate, OfferingUpdate
from backend.services.extractor import UniversalExtractor
from backend.services.offering_service import OfferingService
from backend.services.url_security import SecurityValidationError, UrlSecurityService

logger = logging.getLogger("nexora.discovery")


class WebsiteDiscoveryService:
    """Service handling client website analysis and catalog auto-discovery."""

    DEFAULT_USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36 (compatible; NexoraBot/2.0)"
    )

    @classmethod
    async def _get_or_create_client_source(
        cls,
        db: AsyncSession,
        client_id: uuid.UUID,
        target_url: str,
    ) -> SourceModel:
        """
        Retrieves or creates an internal client catalog SourceModel to associate JobModel with.
        Uses a designated client catalog competitor container per tenant.
        """
        parsed = urlparse(target_url)
        domain = parsed.netloc.lower() or "client-website"

        # Check if an internal or catalog competitor container exists for this client
        comp_stmt = select(CompetitorModel).where(
            CompetitorModel.client_id == client_id,
            CompetitorModel.domain == domain,
        )
        comp_res = await db.execute(comp_stmt)
        competitor = comp_res.scalar_one_or_none()

        if not competitor:
            competitor = CompetitorModel(
                client_id=client_id,
                name=f"Internal Catalog ({domain})",
                domain=domain,
                status=CompetitorStatusEnum.ACTIVE,
            )
            db.add(competitor)
            await db.flush()

        # Check for source
        source_stmt = select(SourceModel).where(
            SourceModel.competitor_id == competitor.id,
            SourceModel.base_url == target_url,
        )
        source_res = await db.execute(source_stmt)
        source = source_res.scalar_one_or_none()

        if not source:
            source = SourceModel(
                competitor_id=competitor.id,
                name=f"Website Discovery Source ({domain})",
                base_url=target_url,
                source_type=SourceTypeEnum.OFFICIAL_STORE,
                is_active=True,
            )
            db.add(source)
            await db.flush()

        return source

    @classmethod
    def _extract_page_links(cls, html_text: str, current_url: str, base_domain: str) -> List[str]:
        """
        Extracts candidate product, service, or catalog page links belonging to the same host domain.
        """
        links: List[str] = []
        if not html_text:
            return links

        # Match href attribute in tags
        pattern = re.compile(r'''href\s*=\s*['"]([^'"]+)['"]''', re.IGNORECASE)
        for match in pattern.finditer(html_text):
            raw_href = match.group(1).strip()
            if not raw_href or raw_href.startswith(("#", "javascript:", "mailto:", "tel:")):
                continue

            resolved = urljoin(current_url, raw_href)
            parsed = urlparse(resolved)

            # Restrict crawl to same host domain and http/https scheme
            if parsed.scheme in ("http", "https") and parsed.netloc.lower() == base_domain:
                # Filter out obvious non-HTML media assets
                path_lower = parsed.path.lower()
                if any(path_lower.endswith(ext) for ext in (
                    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp",
                    ".css", ".js", ".ico", ".pdf", ".zip", ".woff", ".woff2"
                )):
                    continue
                # Normalize by stripping fragment
                cleaned = parsed._replace(fragment="").geturl()
                links.append(cleaned)

        return links

    @classmethod
    async def run_discovery(
        cls,
        db: AsyncSession,
        client_id: uuid.UUID,
        request: DiscoveryRunRequest,
    ) -> DiscoveryJobResponse:
        """
        Executes complete website discovery workflow:
        1. SSRF URL validation
        2. Job creation in RUNNING status
        3. Web crawl of client website up to max_pages
        4. Multi-tier extraction of offerings
        5. De-duplication and database persistence (create or update)
        6. Dynamic attribute discovery and plan quota enforcement
        7. Job completion state recording
        """
        # 1. SSRF URL Validation
        try:
            validated_base_url = UrlSecurityService.validate_url(request.website_url)
        except SecurityValidationError as e:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Invalid or prohibited website URL: {str(e)}",
            )

        parsed_base = urlparse(validated_base_url)
        base_domain = parsed_base.netloc.lower()

        # 2. Setup Source and Job record
        source = await cls._get_or_create_client_source(db, client_id, validated_base_url)

        job = JobModel(
            source_id=source.id,
            job_type=JobTypeEnum.SCHEMA_DISCOVERY,
            status=JobStatusEnum.RUNNING,
            started_at=datetime.now(timezone.utc),
            meta_info={
                "client_id": str(client_id),
                "target_url": validated_base_url,
                "max_pages": request.max_pages,
                "default_offering_type": request.default_offering_type.value,
            },
        )
        db.add(job)
        await db.commit()
        await db.refresh(job)

        # 3. Safe HTTP Crawling & Extraction
        visited_urls: Set[str] = set()
        queue: List[str] = [validated_base_url]
        discovered_items_raw: List[Dict[str, Any]] = []

        headers = {
            "User-Agent": cls.DEFAULT_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }

        async with httpx.AsyncClient(
            headers=headers,
            timeout=15.0,
            follow_redirects=True,
            verify=True,
        ) as http_client:
            while queue and len(visited_urls) < request.max_pages:
                current_url = queue.pop(0)
                if current_url in visited_urls:
                    continue

                # Re-validate every crawled link against SSRF rules before making network calls
                try:
                    safe_url = UrlSecurityService.validate_url(current_url)
                except SecurityValidationError:
                    continue

                visited_urls.add(current_url)

                try:
                    response = await http_client.get(safe_url)
                    if response.status_code != 200:
                        continue

                    content_type = response.headers.get("content-type", "").lower()
                    if "text/html" not in content_type and "application/json" not in content_type:
                        continue

                    html_text = response.text

                    # Extract candidate offerings from page
                    extracted = UniversalExtractor.extract_from_html_or_json(
                        raw_text=html_text,
                        target_url=safe_url,
                    )

                    # If page yielded a valid name or price, consider it an offering
                    if extracted.get("name") or extracted.get("price") is not None:
                        extracted["url"] = safe_url
                        discovered_items_raw.append(extracted)

                    # Extract outgoing links if we haven't reached max_pages
                    if len(visited_urls) < request.max_pages:
                        page_links = cls._extract_page_links(html_text, safe_url, base_domain)
                        for link in page_links:
                            if link not in visited_urls and link not in queue:
                                queue.append(link)

                except Exception as crawl_err:
                    logger.warning(f"Error fetching {current_url} during discovery: {crawl_err}")
                    continue

        # 4. De-duplicate raw discovered items by URL or SKU
        unique_items: Dict[str, Dict[str, Any]] = {}
        for raw in discovered_items_raw:
            dedup_key = raw.get("url") or raw.get("sku") or raw.get("name")
            if dedup_key and dedup_key not in unique_items:
                unique_items[dedup_key] = raw

        # 5. Persist offerings to database (Create or Update)
        created_count = 0
        updated_count = 0
        failed_count = 0
        summaries: List[DiscoveredItemSummary] = []

        for item_data in unique_items.values():
            name = item_data.get("name") or "Discovered Offering"
            item_url = item_data.get("url")
            sku = item_data.get("sku")
            price_val = item_data.get("price")
            currency = item_data.get("currency") or "USD"
            category = item_data.get("category")
            image_url = item_data.get("image_url")
            brand = item_data.get("brand")

            current_price: Optional[Decimal] = None
            if price_val is not None:
                try:
                    current_price = Decimal(str(price_val)).quantize(Decimal("0.01"))
                except Exception:
                    current_price = None

            attributes: Dict[str, Any] = item_data.get("attributes") or {}
            if brand and "brand" not in attributes:
                attributes["brand"] = brand
            if item_data.get("strategy_used"):
                attributes["discovery_strategy"] = item_data.get("strategy_used")

            # Check if offering already exists for this client (matched by URL or SKU)
            existing_offering: Optional[OfferingModel] = None
            match_clauses = []
            if item_url:
                match_clauses.append(OfferingModel.url == item_url)
            if sku:
                match_clauses.append(OfferingModel.sku == sku)

            if match_clauses:
                existing_stmt = select(OfferingModel).where(
                    OfferingModel.client_id == client_id,
                    or_(*match_clauses),
                )
                existing_res = await db.execute(existing_stmt)
                existing_offering = existing_res.scalar_one_or_none()

            try:
                if existing_offering:
                    # Update existing offering
                    update_data = OfferingUpdate(
                        name=name if name != "Discovered Offering" else existing_offering.name,
                        current_price=current_price if current_price is not None else existing_offering.current_price,
                        currency=currency if currency else existing_offering.currency,
                        url=item_url if item_url else existing_offering.url,
                        category=category if category else existing_offering.category,
                        image_url=image_url if image_url else existing_offering.image_url,
                        attributes=attributes,
                    )
                    updated_obj = await OfferingService.update(
                        db=db,
                        offering_id=existing_offering.id,
                        client_id=client_id,
                        data=update_data,
                    )
                    updated_count += 1
                    summaries.append(
                        DiscoveredItemSummary(
                            id=updated_obj.id,
                            name=updated_obj.name,
                            sku=updated_obj.sku,
                            price=updated_obj.current_price,
                            currency=updated_obj.currency,
                            url=updated_obj.url,
                            category=updated_obj.category,
                            image_url=updated_obj.image_url,
                            attributes=updated_obj.attributes,
                            action="UPDATED",
                        )
                    )
                else:
                    # Create new offering via OfferingService
                    create_data = OfferingCreate(
                        name=name,
                        offering_type=request.default_offering_type,
                        sku=sku,
                        current_price=current_price,
                        currency=currency,
                        market="US",
                        url=item_url,
                        category=category,
                        image_url=image_url,
                        attributes=attributes,
                        is_monitored=True,
                        created_via=CreatedViaEnum.WEBSITE_DISCOVERY,
                    )
                    new_obj = await OfferingService.create(
                        db=db,
                        client_id=client_id,
                        data=create_data,
                    )
                    created_count += 1
                    summaries.append(
                        DiscoveredItemSummary(
                            id=new_obj.id,
                            name=new_obj.name,
                            sku=new_obj.sku,
                            price=new_obj.current_price,
                            currency=new_obj.currency,
                            url=new_obj.url,
                            category=new_obj.category,
                            image_url=new_obj.image_url,
                            attributes=new_obj.attributes,
                            action="CREATED",
                        )
                    )
            except Exception as persist_err:
                logger.error(f"Error persisting discovered item '{name}': {persist_err}")
                failed_count += 1
                summaries.append(
                    DiscoveredItemSummary(
                        id=None,
                        name=name,
                        sku=sku,
                        price=current_price,
                        currency=currency,
                        url=item_url,
                        category=category,
                        image_url=image_url,
                        attributes=attributes,
                        action="SKIPPED",
                    )
                )

        # 6. Update Job status
        total_items = len(unique_items)
        successful_items = created_count + updated_count

        job.status = JobStatusEnum.COMPLETED if (successful_items > 0 or total_items == 0) else JobStatusEnum.FAILED
        job.total_items_processed = total_items
        job.successful_items = successful_items
        job.failed_items = failed_count
        job.completed_at = datetime.now(timezone.utc)
        job.meta_info = {
            **job.meta_info,
            "pages_crawled": len(visited_urls),
            "created_count": created_count,
            "updated_count": updated_count,
            "failed_count": failed_count,
        }
        if total_items == 0 and len(visited_urls) > 0:
            job.meta_info["notice"] = "No structured offerings detected on crawled pages."

        await db.commit()
        await db.refresh(job)

        return DiscoveryJobResponse(
            job_id=job.id,
            client_id=client_id,
            target_url=validated_base_url,
            status=job.status,
            total_pages_crawled=len(visited_urls),
            total_items_processed=total_items,
            successful_items=successful_items,
            failed_items=failed_count,
            error_message=job.error_message,
            created_offerings_count=created_count,
            updated_offerings_count=updated_count,
            items=summaries,
            started_at=job.started_at,
            completed_at=job.completed_at,
        )

    @classmethod
    async def get_discovery_job(
        cls,
        db: AsyncSession,
        job_id: uuid.UUID,
        client_id: uuid.UUID,
    ) -> DiscoveryJobResponse:
        """
        Retrieves discovery job status with strict client/tenant isolation.
        """
        stmt = (
            select(JobModel)
            .join(SourceModel, JobModel.source_id == SourceModel.id)
            .join(CompetitorModel, SourceModel.competitor_id == CompetitorModel.id)
            .where(
                JobModel.id == job_id,
                CompetitorModel.client_id == client_id,
            )
        )
        res = await db.execute(stmt)
        job = res.scalar_one_or_none()

        if not job:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Discovery job not found.",
            )

        meta = job.meta_info or {}
        return DiscoveryJobResponse(
            job_id=job.id,
            client_id=client_id,
            target_url=meta.get("target_url", ""),
            status=job.status,
            total_pages_crawled=meta.get("pages_crawled", 0),
            total_items_processed=job.total_items_processed,
            successful_items=job.successful_items,
            failed_items=job.failed_items,
            error_message=job.error_message,
            created_offerings_count=meta.get("created_count", 0),
            updated_offerings_count=meta.get("updated_count", 0),
            items=[],
            started_at=job.started_at,
            completed_at=job.completed_at,
        )
