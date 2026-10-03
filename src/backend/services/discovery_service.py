"""Generic, tenant-safe, multi-product website discovery orchestration."""

from __future__ import annotations

from datetime import datetime, timezone
import logging
import re
from typing import List, Optional, Set
from urllib.parse import parse_qsl, urljoin, urlparse
import uuid

import httpx
from fastapi import HTTPException, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.collectors.browser import render_page
from backend.models.competitor import CompetitorModel, SourceModel
from backend.models.enums import CompetitorStatusEnum, CreatedViaEnum, JobStatusEnum, JobTypeEnum, OfferingTypeEnum, SourceTypeEnum
from backend.models.observation import JobModel
from backend.models.offering import OfferingModel
from backend.schemas.discovery import DiscoveredItemSummary, DiscoveredProduct, DiscoveryJobResponse, DiscoveryRunRequest
from backend.schemas.offering import OfferingCreate, OfferingUpdate
from backend.services.extractor import UniversalExtractor
from backend.services.gemini_discovery_service import GeminiDiscoveryService
from backend.services.offering_service import OfferingService
from backend.services.safe_fetch import safe_get
from backend.services.url_security import SecurityValidationError, UrlSecurityService

logger = logging.getLogger("nexora.discovery")


class WebsiteDiscoveryService:
    """Discovers real offerings; page titles alone are never offerings."""

    DEFAULT_USER_AGENT = "Mozilla/5.0 (compatible; NexoraDiscovery/1.0)"

    @classmethod
    async def _get_or_create_client_source(cls, db: AsyncSession, client_id: uuid.UUID, target_url: str) -> SourceModel:
        domain = urlparse(target_url).netloc.lower() or "client-website"
        client = await db.scalar(
            select(__import__("backend.models.client", fromlist=["ClientModel"]).ClientModel)
            .where(__import__("backend.models.client", fromlist=["ClientModel"]).ClientModel.id == client_id)
            .with_for_update()
        )
        if client is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Client account not found.")
        competitor = (
            await db.execute(
                select(CompetitorModel).where(
                    CompetitorModel.client_id == client_id,
                    CompetitorModel.domain == domain,
                )
            )
        ).scalar_one_or_none()
        if competitor is None:
            current_count = await db.scalar(
                select(__import__("sqlalchemy", fromlist=["func"]).func.count(CompetitorModel.id))
                .where(CompetitorModel.client_id == client_id)
            ) or 0
            if current_count >= client.max_competitors:
                raise HTTPException(
                    status_code=status.HTTP_402_PAYMENT_REQUIRED,
                    detail="Competitor limit reached. Archive an existing competitor before running discovery on a new domain.",
                )
            competitor = CompetitorModel(client_id=client_id, name=f"Internal Catalog ({domain})", domain=domain, status=CompetitorStatusEnum.ACTIVE)
            db.add(competitor)
            await db.flush()
        source = (await db.execute(select(SourceModel).where(SourceModel.competitor_id == competitor.id, SourceModel.base_url == target_url))).scalar_one_or_none()
        if source is None:
            source = SourceModel(competitor_id=competitor.id, name=f"Website Discovery Source ({domain})", base_url=target_url, source_type=SourceTypeEnum.OFFICIAL_STORE, is_active=True)
            db.add(source)
            await db.flush()
        return source

    @staticmethod
    def _candidate_links(html_text: str, current_url: str, base_domain: str) -> List[str]:
        """Find public same-site HTML links without platform or selector assumptions."""
        links: List[str] = []
        for raw_href in re.findall(r'''href\s*=\s*["']([^"']+)["']''', html_text, re.IGNORECASE):
            if raw_href.startswith(("#", "javascript:", "mailto:", "tel:")):
                continue
            resolved = urljoin(current_url, raw_href.strip())
            parsed = urlparse(resolved)
            if parsed.scheme not in {"http", "https"} or parsed.netloc.lower() != base_domain:
                continue
            # oEmbed is a standard representation of a page, not a canonical
            # offering URL. Ignore it generically, not by website/platform.
            query_keys = {key.lower() for key, _ in parse_qsl(parsed.query, keep_blank_values=True)}
            if parsed.path.lower().endswith(".oembed") or "oembed" in query_keys:
                continue
            if parsed.path.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".css", ".js", ".pdf", ".zip", ".woff", ".woff2", ".atom", ".rss", ".xml")):
                continue
            cleaned = parsed._replace(fragment="").geturl()
            if cleaned != current_url:
                links.append(cleaned)
        # Deduplicate while preserving order, prioritizing product/detail paths first
        unique_links = list(dict.fromkeys(links))
        product_like = [l for l in unique_links if any(k in urlparse(l).path.lower() for k in ("/product", "/item", "/plan", "/room", "/course", "/service", "/p/"))]
        other_links = [l for l in unique_links if l not in set(product_like)]
        return product_like + other_links

    @staticmethod
    def _has_explicit_evidence(product: DiscoveredProduct, page_text: str, allowed_urls: Set[str]) -> bool:
        """Reject title-only, invented, or out-of-page product candidates."""
        if not product.name or not product.name.strip() or not product.url:
            return False
        if product.url not in allowed_urls:
            return False
        name_present = product.name.casefold() in page_text.casefold()
        evidence_present = any(evidence and evidence.casefold() in page_text.casefold() for evidence in product.evidence)
        return name_present and (product.price is not None or bool(product.sku) or evidence_present or product.extraction_method == "JSON_LD_SCHEMA")

    @staticmethod
    def _structured_candidates(html_text: str, page_url: str) -> List[DiscoveredProduct]:
        return [DiscoveredProduct.model_validate({**item, "url": urljoin(page_url, item.get("url") or page_url), "extraction_method": "JSON_LD_SCHEMA"}) for item in UniversalExtractor.extract_products_from_html_or_json(html_text, page_url)]

    @classmethod
    async def _analyze_page(cls, *, page_url: str, html_text: str, base_domain: str) -> tuple[List[DiscoveredProduct], List[str], str]:
        links = cls._candidate_links(html_text, page_url, base_domain)
        structured = cls._structured_candidates(html_text, page_url)
        if structured:
            if len(structured) == 1:
                return structured, links, "PRODUCT"
            # A collection can expose several Product records. Use their URLs as
            # leads, but still visit the individual pages before persistence.
            product_urls = [p.url for p in structured if p.url and p.url != page_url]
            return [], list(dict.fromkeys(product_urls + links)), "PRODUCT_LISTING"

        extracted = UniversalExtractor.extract_from_html_or_json(html_text, page_url)
        deterministic: List[DiscoveredProduct] = []
        # Only accept metadata evidence when no candidate links indicate a listing.
        if (
            extracted.get("price") is not None
            and extracted.get("name")
            and not links
            # Arbitrary JSON page representations can contain a title, SKU and
            # price. They are leads for Gemini, never deterministic products.
            and extracted.get("strategy_used") != "DIRECT_JSON_PAYLOAD"
        ):
            deterministic.append(DiscoveredProduct.model_validate({**extracted, "url": page_url, "extraction_method": extracted.get("strategy_used", "STRUCTURED")}))

        ai_result = await GeminiDiscoveryService.analyze_page(page_url=page_url, page_text=html_text, candidate_urls=links)
        if ai_result:
            allowed = set(links) | {page_url}
            safe_urls = [url for url in ai_result.product_urls if url in set(links)]
            ai_products = [p.model_copy(update={"extraction_method": "GEMINI_FLASH"}) for p in ai_result.products if cls._has_explicit_evidence(p, html_text, allowed)]
            if ai_result.page_type == "PRODUCT_LISTING":
                # Listing card data is a lead, not canonical catalog data. Fetch
                # each product page so attributes and variants remain accurate.
                return [], list(dict.fromkeys(safe_urls + links)), ai_result.page_type
            return ai_products or deterministic, list(dict.fromkeys(safe_urls + links)), ai_result.page_type
        return deterministic, links, "PRODUCT" if deterministic else "UNKNOWN"

    @classmethod
    async def _persist_product(cls, db: AsyncSession, client_id: uuid.UUID, product: DiscoveredProduct, offering_type: OfferingTypeEnum) -> tuple[Optional[DiscoveredItemSummary], str]:
        if not product.name or not product.url:
            return None, "SKIPPED"
        try:
            safe_url = UrlSecurityService.validate_url(product.url, allow_empty=False)
            safe_image = UrlSecurityService.validate_url(product.image_url) if product.image_url else None
        except SecurityValidationError:
            return None, "SKIPPED"
        clauses = [OfferingModel.url == safe_url]
        if product.sku:
            clauses.append(OfferingModel.sku == product.sku)
        existing = (await db.execute(select(OfferingModel).where(OfferingModel.client_id == client_id, or_(*clauses)))).scalar_one_or_none()
        attributes = dict(product.attributes or {})
        if product.brand:
            attributes.setdefault("brand", product.brand)
        if product.availability:
            attributes.setdefault("availability", product.availability)
        attributes.setdefault("discovery_method", product.extraction_method)
        try:
            if existing:
                result = await OfferingService.update(db, client_id, existing.id, OfferingUpdate(name=product.name, sku=product.sku or existing.sku, current_price=product.price, currency=product.currency, url=safe_url, category=product.category, image_url=safe_image, attributes=attributes))
                action = "UPDATED"
            else:
                result = await OfferingService.create(db, client_id, OfferingCreate(name=product.name, offering_type=offering_type, sku=product.sku, current_price=product.price, currency=product.currency, market="US", url=safe_url, category=product.category, image_url=safe_image, attributes=attributes, is_monitored=True, created_via=CreatedViaEnum.WEBSITE_DISCOVERY))
                action = "CREATED"
        except Exception as exc:
            logger.warning("Could not persist discovered offering %s: %s", product.name, exc)
            # OfferingService commits internally. A failed flush/commit leaves
            # this request session unusable until rollback; isolate the bad
            # candidate so the job can record its final state and continue.
            await db.rollback()
            return None, "SKIPPED"
        return DiscoveredItemSummary(id=result.id, name=result.name, sku=result.sku, price=result.current_price, currency=product.currency, url=result.url, category=result.category, image_url=result.image_url, attributes=result.attributes, action=action), action

    @classmethod
    async def run_discovery(cls, db: AsyncSession, client_id: uuid.UUID, request: DiscoveryRunRequest) -> DiscoveryJobResponse:
        try:
            target_url = UrlSecurityService.validate_url(request.website_url, allow_empty=False)
        except SecurityValidationError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"Invalid or prohibited website URL: {exc}")
        source = await cls._get_or_create_client_source(db, client_id, target_url)
        job = JobModel(source_id=source.id, job_type=JobTypeEnum.SCHEMA_DISCOVERY, status=JobStatusEnum.RUNNING, started_at=datetime.now(timezone.utc), meta_info={"client_id": str(client_id), "target_url": target_url, "max_pages": request.max_pages})
        db.add(job)
        await db.commit()

        base_domain = urlparse(target_url).netloc.lower()
        queue, visited, candidates = [target_url], set(), []
        seen_product_urls: Set[str] = set()
        created = updated = failed = 0
        diagnostics: List[str] = []
        headers = {"User-Agent": cls.DEFAULT_USER_AGENT, "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8"}
        async with httpx.AsyncClient(headers=headers, timeout=20.0, follow_redirects=False) as client:
            while queue and len(visited) < request.max_pages:
                current = queue.pop(0)
                if current in visited:
                    continue
                try:
                    safe_url = UrlSecurityService.validate_url(current, allow_empty=False)
                except SecurityValidationError:
                    continue
                visited.add(safe_url)
                try:
                    response = await safe_get(client, safe_url)
                    if response.status_code != 200:
                        diagnostics.append(f"Skipped {safe_url}: HTTP {response.status_code}")
                        continue
                    content_type = response.headers.get("content-type", "").lower()
                    if "text/html" not in content_type and "application/json" not in content_type:
                        continue
                    page_text = response.text
                    products, links, page_type = await cls._analyze_page(page_url=safe_url, html_text=page_text, base_domain=base_domain)
                    if not products and not links:
                        rendered = await render_page(safe_url)
                        if rendered:
                            products, links, page_type = await cls._analyze_page(page_url=safe_url, html_text=rendered, base_domain=base_domain)
                            page_text = rendered
                    for product in products:
                        if product.url in seen_product_urls or not cls._has_explicit_evidence(product, page_text, set(links) | {safe_url}):
                            continue
                        seen_product_urls.add(product.url)
                        candidates.append(product)
                    if page_type in {"PRODUCT_LISTING", "OTHER", "UNKNOWN"}:
                        for link in links:
                            if link not in visited and link not in queue:
                                queue.append(link)
                except Exception as exc:
                    logger.warning("Discovery fetch failed for %s: %s", safe_url, exc)
                    diagnostics.append(f"Skipped {safe_url}: fetch failed")

        summaries: List[DiscoveredItemSummary] = []
        for product in candidates:
            summary, action = await cls._persist_product(db, client_id, product, request.default_offering_type)
            created += action == "CREATED"
            updated += action == "UPDATED"
            failed += action == "SKIPPED"
            if summary:
                summaries.append(summary)
        if not candidates:
            diagnostics.append("No valid offerings discovered.")
        job.status = JobStatusEnum.COMPLETED
        job.total_items_processed, job.successful_items, job.failed_items = len(candidates), created + updated, failed
        job.completed_at = datetime.now(timezone.utc)
        job.error_message = None if candidates else diagnostics[-1]
        persisted_items = [item.model_dump(mode="json") for item in summaries[:500]]
        job.meta_info = {
            "client_id": str(client_id),
            "target_url": target_url,
            "max_pages": request.max_pages,
            "pages_crawled": len(visited),
            "created_count": created,
            "updated_count": updated,
            "diagnostics": diagnostics,
            "items": persisted_items,
            "items_truncated": len(summaries) > len(persisted_items),
        }
        await db.commit()
        await db.refresh(job)
        return DiscoveryJobResponse(job_id=job.id, client_id=client_id, target_url=target_url, status=job.status, total_pages_crawled=len(visited), total_items_processed=len(candidates), successful_items=created + updated, failed_items=failed, error_message=job.error_message, created_offerings_count=created, updated_offerings_count=updated, items=summaries, started_at=job.started_at, completed_at=job.completed_at)

    @classmethod
    async def get_discovery_job(cls, db: AsyncSession, job_id: uuid.UUID, client_id: uuid.UUID) -> DiscoveryJobResponse:
        statement = select(JobModel).join(SourceModel).join(CompetitorModel).where(JobModel.id == job_id, CompetitorModel.client_id == client_id)
        job = (await db.execute(statement)).scalar_one_or_none()
        if not job:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Discovery job not found.")
        meta = job.meta_info or {}
        return DiscoveryJobResponse(job_id=job.id, client_id=client_id, target_url=meta.get("target_url", ""), status=job.status, total_pages_crawled=meta.get("pages_crawled", 0), total_items_processed=job.total_items_processed, successful_items=job.successful_items, failed_items=job.failed_items, error_message=job.error_message, created_offerings_count=meta.get("created_count", 0), updated_offerings_count=meta.get("updated_count", 0), items=meta.get("items", []), started_at=job.started_at, completed_at=job.completed_at)
