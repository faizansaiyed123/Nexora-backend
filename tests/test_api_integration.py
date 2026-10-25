"""PostgreSQL-backed API integration coverage for Nexora's core tenant workflows.

These tests exercise the real FastAPI routes and persisted records. The CI workflow
provides disposable PostgreSQL and Redis services and enables verification bypass
only for its isolated test database so onboarding does not require an external inbox.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from backend.core.config import get_settings
from backend.core.security import (
    generate_secure_token,
    get_password_hash,
    hash_token,
    verify_password,
)
from backend.core.rate_limit import TokenSessionStore
from backend.api.v1 import collections as collections_route
from backend.services.collection_service import CollectionResult, CollectionService
from backend.db.session import AsyncSessionLocal
from backend.main import app
from backend.models.alert import AlertLogModel
from backend.models.client import UserModel
from backend.models.enums import JobStatusEnum, JobTypeEnum, RoleEnum
from backend.models.observation import JobModel


INITIAL_PASSWORD = "Nexora-Strong!2026"
UPDATED_PASSWORD = "Nexora-Changed!2026"


async def _register(client: AsyncClient, suffix: str, organization: str) -> tuple[str, str]:
    email = f"nexora-{suffix}-{uuid.uuid4().hex[:10]}@example.com"
    result = await client.post(
        "/v1/auth/register",
        json={
            "organization_name": organization,
            "full_name": "Nexora Test Operator",
            "email": email,
            "password": INITIAL_PASSWORD,
        },
    )
    assert result.status_code == 201, result.text
    assert result.json()["email_verification_required"] is False, result.text
    return email, INITIAL_PASSWORD


async def _login(client: AsyncClient, email: str, password: str = INITIAL_PASSWORD) -> dict:
    result = await client.post(
        "/v1/auth/login",
        json={"email": email, "password": password},
    )
    assert result.status_code == 200, result.text
    payload = result.json()
    assert payload["access_token"]
    assert payload["refresh_token"]
    return payload


async def _create_role_user(client_id: uuid.UUID, role: RoleEnum, suffix: str) -> str:
    email = f"nexora-{role.value.lower()}-{suffix}-{uuid.uuid4().hex[:8]}@example.com"
    async with AsyncSessionLocal() as session:
        session.add(
            UserModel(
                client_id=client_id,
                email=email,
                full_name=f"Nexora {role.value.title()}",
                hashed_password=get_password_hash("Nexora-Role!2026"),
                role=role,
                is_active=True,
                email_verified=True,
            )
        )
        await session.commit()
    return email


@pytest.mark.asyncio
async def test_full_tenant_api_and_database_workflow(monkeypatch: pytest.MonkeyPatch) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://nexora.test",
    ) as client:
        # The API health check reaches both real services; browser E2E runs use a
        # separately launched Uvicorn process rather than ASGI transport.
        health = await client.get("/v1/health")
        assert health.status_code == 200, health.text
        assert health.json()["checks"] == {"database": "ok", "redis": "ok"}

        cors = await client.options(
            "/v1/auth/login",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )
        assert cors.status_code == 200, cors.text
        assert cors.headers.get("access-control-allow-origin") == "http://localhost:5173"

        assert (await client.get("/v1/competitors")).status_code == 401

        # If the one-time verification token cannot be stored, registration must fail
        # and roll back its tenant/user records instead of emailing a dead link.
        unavailable_email = f"nexora-unavailable-{uuid.uuid4().hex[:12]}@example.com"

        async def fail_verification_store(*args, **kwargs):
            raise RuntimeError("Redis unavailable for regression test")

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(get_settings(), "email_verification_bypass", False)
            patch.setattr(TokenSessionStore, "store_email_verification_token", fail_verification_store)
            failed_registration = await client.post(
                "/v1/auth/register",
                json={
                    "organization_name": "Uncommitted Workspace",
                    "full_name": "Uncommitted User",
                    "email": unavailable_email,
                    "password": INITIAL_PASSWORD,
                },
            )
        assert failed_registration.status_code == 503, failed_registration.text
        async with AsyncSessionLocal() as session:
            assert await session.scalar(
                select(UserModel).where(UserModel.email == unavailable_email)
            ) is None

        suffix = uuid.uuid4().hex[:10]
        admin_email, _ = await _register(
            client, suffix, f"Northstar Intelligence {suffix}"
        )
        bad_login = await client.post(
            "/v1/auth/login",
            json={"email": admin_email, "password": "Wrong-Password!2026"},
        )
        assert bad_login.status_code == 401, bad_login.text

        auth = await _login(client, admin_email)
        headers = {"Authorization": f"Bearer {auth['access_token']}"}
        me = await client.get("/v1/auth/me", headers=headers)
        assert me.status_code == 200, me.text
        assert me.json()["role"] == "ORG_ADMIN"
        client_id = uuid.UUID(me.json()["client_id"])

        # An invalid one-time token must never verify an unrelated pending account.
        pending_email = f"pending-{suffix}-{uuid.uuid4().hex[:8]}@example.com"
        async with AsyncSessionLocal() as session:
            session.add(
                UserModel(
                    client_id=client_id,
                    email=pending_email,
                    full_name="Pending Verification",
                    hashed_password=get_password_hash(INITIAL_PASSWORD),
                    role=RoleEnum.VIEWER,
                    is_active=True,
                    email_verified=False,
                )
            )
            await session.commit()
        invalid_verification = await client.get(
            "/v1/auth/verify-email",
            params={"token": f"invalid-{uuid.uuid4().hex}"},
        )
        assert invalid_verification.status_code == 422, invalid_verification.text
        async with AsyncSessionLocal() as session:
            pending_user = await session.scalar(
                select(UserModel).where(UserModel.email == pending_email)
            )
            assert pending_user is not None and pending_user.email_verified is False

        profile = await client.get("/v1/auth/profile", headers=headers)
        assert profile.status_code == 200, profile.text
        profile_update = await client.patch(
            "/v1/auth/profile",
            headers=headers,
            json={
                "company_name": "Northstar Intelligence Labs",
                "country": "US",
                "default_currency": "USD",
                "default_market": "US",
                "industry": "Enterprise Software",
                "timezone": "America/New_York",
            },
        )
        assert profile_update.status_code == 200, profile_update.text
        assert profile_update.json()["company_name"] == "Northstar Intelligence Labs"

        # Competitor lifecycle, duplicate-domain guard, source lifecycle and config.
        competitor_response = await client.post(
            "/v1/competitors",
            headers=headers,
            json={"name": "Datadog", "domain": "www.datadoghq.com"},
        )
        assert competitor_response.status_code == 201, competitor_response.text
        competitor = competitor_response.json()
        competitor_id = competitor["id"]

        # Source URLs are untrusted input even when attached to a valid competitor.
        blocked_source = await client.post(
            "/v1/sources",
            headers=headers,
            json={
                "competitor_id": competitor_id,
                "name": f"Blocked internal source {suffix}",
                "base_url": "http://127.0.0.1:8000/v1/health",
                "source_type": "OFFICIAL_STORE",
                "collection_method": "HTTP_FAST",
            },
        )
        assert blocked_source.status_code == 422, blocked_source.text
        current_sources = await client.get("/v1/sources", headers=headers)
        assert current_sources.status_code == 200, current_sources.text
        assert all("Blocked internal source" not in row["name"] for row in current_sources.json())

        duplicate = await client.post(
            "/v1/competitors",
            headers=headers,
            json={"name": "Duplicate Datadog", "domain": "www.datadoghq.com"},
        )
        assert duplicate.status_code == 403, duplicate.text

        competitor_detail = await client.get(
            f"/v1/competitors/{competitor_id}", headers=headers
        )
        assert competitor_detail.status_code == 200, competitor_detail.text
        competitor_update = await client.patch(
            f"/v1/competitors/{competitor_id}",
            headers=headers,
            json={"name": "Datadog Enterprise"},
        )
        assert competitor_update.status_code == 200, competitor_update.text
        assert competitor_update.json()["name"] == "Datadog Enterprise"

        source_response = await client.post(
            "/v1/sources",
            headers=headers,
            json={
                "competitor_id": competitor_id,
                "name": "Datadog Pricing",
                "base_url": "https://www.datadoghq.com/pricing/",
                "source_type": "OFFICIAL_STORE",
                "collection_method": "HTTP_FAST",
            },
        )
        assert source_response.status_code == 201, source_response.text
        source_id = source_response.json()["id"]

        # A source without an explicit configuration still needs a working
        # browser-rendering configuration when its visible collection method changes.
        browser_method_update = await client.patch(
            f"/v1/sources/{source_id}",
            headers=headers,
            json={"collection_method": "PLAYWRIGHT_BROWSER"},
        )
        assert browser_method_update.status_code == 200, browser_method_update.text
        assert browser_method_update.json()["collection_method"] == "PLAYWRIGHT_BROWSER"
        assert browser_method_update.json()["configuration"]["requires_javascript"] is True

        http_method_update = await client.patch(
            f"/v1/sources/{source_id}",
            headers=headers,
            json={"collection_method": "HTTP_FAST"},
        )
        assert http_method_update.status_code == 200, http_method_update.text
        assert http_method_update.json()["collection_method"] == "HTTP_FAST"
        assert http_method_update.json()["configuration"]["requires_javascript"] is False

        source_update = await client.patch(
            f"/v1/sources/{source_id}",
            headers=headers,
            json={"name": "Datadog Official Pricing"},
        )
        assert source_update.status_code == 200, source_update.text
        config_update = await client.patch(
            f"/v1/sources/{source_id}/configuration",
            headers=headers,
            json={
                "rate_limit_rpm": 45,
                "request_delay_seconds": 0.1,
                "timeout_seconds": 20,
                "max_retries": 2,
                "requires_javascript": True,
                "custom_headers": {"Accept-Language": "en-US,en;q=0.9"},
                "extraction_selectors": {
                    "title_selector": "h1",
                    "price_selector": "[itemprop='price']",
                },
            },
        )
        assert config_update.status_code == 200, config_update.text
        assert config_update.json()["rate_limit_rpm"] == 45
        source_read_back = await client.get(
            f"/v1/sources/{source_id}", headers=headers
        )
        assert source_read_back.status_code == 200, source_read_back.text
        assert source_read_back.json()["configuration"]["max_retries"] == 2

        unsupported_proxy = await client.patch(
            f"/v1/sources/{source_id}/configuration",
            headers=headers,
            json={"requires_proxy": True},
        )
        assert unsupported_proxy.status_code == 422, unsupported_proxy.text
        proxy_read_back = await client.get(
            f"/v1/sources/{source_id}", headers=headers
        )
        assert proxy_read_back.status_code == 200, proxy_read_back.text
        assert proxy_read_back.json()["configuration"]["requires_proxy"] is False

        source_filter = await client.get(
            "/v1/sources",
            headers=headers,
            params={"competitor_id": competitor_id},
        )
        assert source_filter.status_code == 200, source_filter.text
        assert [row["id"] for row in source_filter.json()] == [source_id]

        # Manual catalog, dynamic attributes, detail/history, filtering and archive.
        offering_response = await client.post(
            "/v1/offerings",
            headers=headers,
            json={
                "name": "Sentinel Observability Enterprise",
                "offering_type": "SAAS_PLAN",
                "sku": f"NXR-SNT-{suffix}",
                "current_price": "2499.00",
                "currency": "USD",
                "market": "US",
                "url": "https://www.datadoghq.com/pricing/",
                "category": "Software > Observability",
                "description": "Enterprise observability workspace for integration coverage.",
                "attributes": {
                    "vendor": "Datadog",
                    "included_hosts": 100,
                    "support_tier": "Enterprise",
                },
                "is_monitored": True,
            },
        )
        assert offering_response.status_code == 201, offering_response.text
        offering_id = offering_response.json()["id"]

        blocked_offering = await client.post(
            "/v1/offerings",
            headers=headers,
            json={
                "name": f"Blocked internal offering {suffix}",
                "sku": f"NXR-BLOCKED-{suffix}",
                "url": "http://127.0.0.1:8000/v1/health",
            },
        )
        assert blocked_offering.status_code == 422, blocked_offering.text
        blocked_offering_lookup = await client.get(
            "/v1/offerings",
            headers=headers,
            params={"q": f"Blocked internal offering {suffix}"},
        )
        assert blocked_offering_lookup.status_code == 200, blocked_offering_lookup.text
        assert blocked_offering_lookup.json()["total_count"] == 0

        listing = await client.get(
            "/v1/offerings",
            headers=headers,
            params={"q": "Sentinel Observability", "page": 1, "page_size": 10},
        )
        assert listing.status_code == 200, listing.text
        assert listing.json()["total_count"] >= 1
        assert any(item["id"] == offering_id for item in listing.json()["items"])

        detail = await client.get(f"/v1/offerings/{offering_id}", headers=headers)
        assert detail.status_code == 200, detail.text
        assert detail.json()["attributes"]["included_hosts"] == 100
        history = await client.get(
            f"/v1/offerings/{offering_id}/history", headers=headers
        )
        assert history.status_code == 200, history.text
        assert history.json() == []

        edited = await client.patch(
            f"/v1/offerings/{offering_id}",
            headers=headers,
            json={
                "current_price": "2299.00",
                "attributes": {"included_hosts": 150, "support_tier": "Premium"},
            },
        )
        assert edited.status_code == 200, edited.text
        assert float(edited.json()["current_price"]) == 2299.0
        updated_detail = await client.get(
            f"/v1/offerings/{offering_id}", headers=headers
        )
        assert updated_detail.status_code == 200, updated_detail.text
        assert updated_detail.json()["attributes"]["included_hosts"] == 150
        assert updated_detail.json()["attributes"]["support_tier"] == "Premium"

        # Search, market/category/currency/price filters and pagination must agree.
        filtered_range = await client.get(
            "/v1/offerings",
            headers=headers,
            params={
                "category": "Observability",
                "market": "US",
                "currency": "USD",
                "min_price": "2000",
                "max_price": "2300",
                "page": 1,
                "page_size": 1,
            },
        )
        assert filtered_range.status_code == 200, filtered_range.text
        filtered_payload = filtered_range.json()
        assert filtered_payload["total_count"] == 1, filtered_payload
        assert len(filtered_payload["items"]) == 1
        assert filtered_payload["items"][0]["id"] == offering_id
        assert filtered_payload["has_more"] is False, filtered_payload

        monitor = await client.post(
            f"/v1/offerings/{offering_id}/toggle-monitoring",
            headers=headers,
        )
        assert monitor.status_code == 200, monitor.text
        assert monitor.json()["is_monitored"] is False
        filtered = await client.get(
            "/v1/offerings",
            headers=headers,
            params={"is_monitored": False, "q": "Sentinel Observability"},
        )
        assert filtered.status_code == 200, filtered.text
        assert any(item["id"] == offering_id for item in filtered.json()["items"])

        fields = await client.get("/v1/offerings/fields", headers=headers)
        assert fields.status_code == 200, fields.text
        assert any(item["field_name"] == "included_hosts" for item in fields.json())
        field_response = await client.post(
            "/v1/offerings/fields",
            headers=headers,
            json={
                "field_name": f"renewal_term_{suffix}",
                "display_name": "Renewal Term",
                "data_type": "NUMBER",
                "unit": "months",
                "category": "Commercial",
                "description": "Contract renewal interval.",
                "is_selected": True,
                "is_required": False,
                "is_comparable": True,
            },
        )
        assert field_response.status_code == 201, field_response.text
        field_id = field_response.json()["id"]
        delete_field = await client.delete(
            f"/v1/offerings/fields/{field_id}", headers=headers
        )
        assert delete_field.status_code == 204, delete_field.text

        imported = await client.post(
            "/v1/offerings/bulk",
            headers=headers,
            json={
                "items": [
                    {
                        "name": f"AWS Compute Optimized {suffix}",
                        "offering_type": "PLAN",
                        "sku": f"AWS-EC2-{suffix}",
                        "current_price": "1200.00",
                        "currency": "USD",
                        "market": "US",
                        "url": "https://aws.amazon.com/ec2/pricing/",
                        "attributes": {"vcpu": 16, "memory_gib": 64},
                    },
                    {
                        "name": f"Snowflake Enterprise {suffix}",
                        "offering_type": "SAAS_PLAN",
                        "sku": f"SNOW-ENT-{suffix}",
                        "current_price": "850.00",
                        "currency": "USD",
                        "market": "US",
                        "url": "https://www.snowflake.com/en/pricing-options/",
                    },
                ]
            },
        )
        assert imported.status_code == 200, imported.text
        assert imported.json()["successful_count"] == 2, imported.text
        bulk_items = await client.get(
            "/v1/offerings",
            headers=headers,
            params={"q": suffix, "page": 1, "page_size": 20},
        )
        assert bulk_items.status_code == 200, bulk_items.text
        bulk_ids = [
            row["id"] for row in bulk_items.json()["items"]
            if row["id"] != offering_id
        ]
        assert len(bulk_ids) == 2, bulk_items.text

        archive_bulk = await client.post(
            "/v1/offerings/bulk-archive",
            headers=headers,
            json={"offering_ids": bulk_ids},
        )
        assert archive_bulk.status_code == 200, archive_bulk.text
        assert archive_bulk.json()["archived_count"] == 2, archive_bulk.text

        # Mixed bulk import reports row-level failures without discarding valid rows.
        edge_suffix = uuid.uuid4().hex[:10]
        edge_name = f"Bulk Partial Recovery {edge_suffix}"
        partial_import = await client.post(
            "/v1/offerings/bulk",
            headers=headers,
            json={
                "items": [
                    {
                        "name": edge_name,
                        "offering_type": "PRODUCT",
                        "sku": f"EDGE-OK-{edge_suffix}",
                        "current_price": "19.95",
                        "currency": "USD",
                        "market": "US",
                        "url": "https://example.com/products/edge-case",
                        "category": "Integration > Bulk Import",
                    },
                    {
                        "name": f"Blocked Bulk Row {edge_suffix}",
                        "offering_type": "PRODUCT",
                        "sku": f"EDGE-BLOCKED-{edge_suffix}",
                        "current_price": "29.95",
                        "currency": "USD",
                        "market": "US",
                        "url": "http://127.0.0.1:8000/v1/health",
                    },
                ]
            },
        )
        assert partial_import.status_code == 200, partial_import.text
        assert partial_import.json()["total_processed"] == 2, partial_import.text
        assert partial_import.json()["successful_count"] == 1, partial_import.text
        assert partial_import.json()["failed_count"] == 1, partial_import.text
        assert partial_import.json()["errors"][0]["index"] == 1, partial_import.text
        edge_readback = await client.get(
            "/v1/offerings",
            headers=headers,
            params={"q": edge_name, "page": 1, "page_size": 5},
        )
        assert edge_readback.status_code == 200, edge_readback.text
        assert edge_readback.json()["total_count"] == 1, edge_readback.text
        edge_id = edge_readback.json()["items"][0]["id"]
        edge_archive = await client.post(
            "/v1/offerings/bulk-archive",
            headers=headers,
            json={"offering_ids": [edge_id]},
        )
        assert edge_archive.status_code == 200, edge_archive.text
        assert edge_archive.json()["archived_count"] == 1, edge_archive.text

        exported_json = await client.get(
            "/v1/offerings/export", headers=headers, params={"format": "json"}
        )
        assert exported_json.status_code == 200, exported_json.text
        assert "Sentinel Observability Enterprise" in exported_json.text
        exported_csv = await client.get(
            "/v1/offerings/export", headers=headers, params={"format": "csv"}
        )
        assert exported_csv.status_code == 200, exported_csv.text
        assert "Sentinel Observability Enterprise" in exported_csv.text
        unsupported_export = await client.get(
            "/v1/offerings/export", headers=headers, params={"format": "xml"}
        )
        assert unsupported_export.status_code == 422, unsupported_export.text

        # Link catalog item to a competitor source and verify match CRUD.
        match_response = await client.post(
            "/v1/offering-matches",
            headers=headers,
            json={
                "offering_id": offering_id,
                "source_id": source_id,
                "target_url": "https://www.datadoghq.com/pricing/",
                "match_status": "APPROVED",
                "confidence_score": 0.98,
                "extraction_selectors": {
                    "title_selector": "h1",
                    "price_selector": "[itemprop='price']",
                },
            },
        )
        assert match_response.status_code == 201, match_response.text
        match_id = match_response.json()["id"]

        blocked_match = await client.post(
            "/v1/offering-matches",
            headers=headers,
            json={
                "offering_id": offering_id,
                "source_id": source_id,
                "target_url": "http://127.0.0.1:8000/v1/health",
                "match_status": "APPROVED",
                "confidence_score": 0.98,
            },
        )
        assert blocked_match.status_code == 422, blocked_match.text

        matches = await client.get("/v1/offering-matches", headers=headers)
        assert matches.status_code == 200, matches.text
        assert any(item["id"] == match_id for item in matches.json())
        match_update = await client.patch(
            f"/v1/offering-matches/{match_id}",
            headers=headers,
            json={"confidence_score": 0.91, "match_status": "SUGGESTED"},
        )
        assert match_update.status_code == 200, match_update.text
        assert match_update.json()["match_status"] == "SUGGESTED"

        # An inactive source must not start a collection job or fetch its target.
        inactive_source = await client.patch(
            f"/v1/sources/{source_id}",
            headers=headers,
            json={"is_active": False},
        )
        assert inactive_source.status_code == 200, inactive_source.text
        blocked_collection = await client.post(
            "/v1/collections/run",
            headers=headers,
            params={"offering_match_id": match_id},
        )
        assert blocked_collection.status_code == 503, blocked_collection.text
        inactive_read_back = await client.get(
            f"/v1/sources/{source_id}", headers=headers
        )
        assert inactive_read_back.status_code == 200, inactive_read_back.text
        assert inactive_read_back.json()["is_active"] is False
        active_source = await client.patch(
            f"/v1/sources/{source_id}",
            headers=headers,
            json={"is_active": True},
        )
        assert active_source.status_code == 200, active_source.text

        match_detail = await client.get(
            f"/v1/offering-matches/{match_id}", headers=headers
        )
        assert match_detail.status_code == 200, match_detail.text
        assert match_detail.json()["match_status"] == "SUGGESTED"
        duplicate_match = await client.post(
            "/v1/offering-matches",
            headers=headers,
            json={
                "offering_id": offering_id,
                "source_id": source_id,
                "target_url": "https://www.datadoghq.com/pricing/",
                "match_status": "APPROVED",
                "confidence_score": 0.98,
            },
        )
        assert duplicate_match.status_code == 400, duplicate_match.text

        # Exercise the individual job-detail read route with a persisted job row.
        finished_at = datetime.now(timezone.utc)
        async with AsyncSessionLocal() as session:
            job_row = JobModel(
                source_id=uuid.UUID(source_id),
                job_type=JobTypeEnum.ON_DEMAND_REFRESH,
                status=JobStatusEnum.COMPLETED,
                started_at=finished_at,
                completed_at=finished_at,
                total_items_processed=1,
                successful_items=1,
                failed_items=0,
                meta_info={"origin": "postgresql-api-integration"},
            )
            session.add(job_row)
            await session.commit()
            await session.refresh(job_row)
            job_id = str(job_row.id)
        job_detail = await client.get(f"/v1/jobs/{job_id}", headers=headers)
        assert job_detail.status_code == 200, job_detail.text
        assert job_detail.json()["id"] == job_id
        assert job_detail.json()["status"] == "COMPLETED"
        job_list = await client.get("/v1/jobs", headers=headers)
        assert job_list.status_code == 200, job_list.text
        assert any(row["id"] == job_id for row in job_list.json())

        # Alert rules and persistent event-read state.
        rule_response = await client.post(
            "/v1/alerts/rules",
            headers=headers,
            json={
                "name": f"Price drop monitor {suffix}",
                "alert_type": "PERCENTAGE_DROP",
                "offering_id": offering_id,
                "threshold_value": 10,
                "target_channels": {},
                "cooldown_minutes": 60,
                "is_active": True,
            },
        )
        assert rule_response.status_code == 201, rule_response.text
        rule_id = rule_response.json()["id"]
        rule_update = await client.patch(
            f"/v1/alerts/rules/{rule_id}",
            headers=headers,
            json={"name": f"Enterprise price drop {suffix}", "is_active": False},
        )
        assert rule_update.status_code == 200, rule_update.text
        assert rule_update.json()["is_active"] is False
        rule_update = await client.patch(
            f"/v1/alerts/rules/{rule_id}",
            headers=headers,
            json={"is_active": True},
        )
        assert rule_update.status_code == 200, rule_update.text

        async with AsyncSessionLocal() as session:
            log_row = AlertLogModel(
                alert_rule_id=uuid.UUID(rule_id),
                offering_match_id=uuid.UUID(match_id),
                title="Competitor price decreased",
                message="A monitored competitor crossed the configured threshold.",
                triggered_value=-12.5,
                payload_snapshot={"event": "PERCENTAGE_DROP", "source": "integration"},
                is_read=False,
            )
            session.add(log_row)
            await session.commit()
            await session.refresh(log_row)
            log_id = str(log_row.id)

        logs = await client.get("/v1/alerts/logs", headers=headers)
        assert logs.status_code == 200, logs.text
        assert any(row["id"] == log_id and not row["is_read"] for row in logs.json())
        read_log = await client.patch(
            f"/v1/alerts/logs/{log_id}/read", headers=headers
        )
        assert read_log.status_code == 200, read_log.text
        assert read_log.json()["is_read"] is True
        await_read_back = await client.get("/v1/alerts/logs", headers=headers)
        assert any(
            row["id"] == log_id and row["is_read"]
            for row in await_read_back.json()
        )

        # Tenant boundary: other organizations cannot list or fetch these rows.
        other_email, _ = await _register(
            client, suffix, f"Contoso Intelligence {suffix}"
        )
        other_auth = await _login(client, other_email)
        other_headers = {"Authorization": f"Bearer {other_auth['access_token']}"}

        # Attempts to create linked records with another tenant's IDs must fail.
        foreign_source_create = await client.post(
            "/v1/sources",
            headers=other_headers,
            json={
                "competitor_id": competitor_id,
                "name": f"Foreign source attempt {suffix}",
                "base_url": "https://www.datadoghq.com/pricing/",
                "source_type": "OFFICIAL_STORE",
                "collection_method": "HTTP_FAST",
            },
        )
        assert foreign_source_create.status_code == 403, foreign_source_create.text
        foreign_match_create = await client.post(
            "/v1/offering-matches",
            headers=other_headers,
            json={
                "offering_id": offering_id,
                "source_id": source_id,
                "target_url": "https://www.datadoghq.com/pricing/",
                "match_status": "APPROVED",
                "confidence_score": 0.98,
            },
        )
        assert foreign_match_create.status_code == 400, foreign_match_create.text

        foreign_source_update = await client.patch(
            f"/v1/sources/{source_id}",
            headers=other_headers,
            json={"name": "Foreign tenant rename attempt"},
        )
        assert foreign_source_update.status_code == 404, foreign_source_update.text
        foreign_match_update = await client.patch(
            f"/v1/offering-matches/{match_id}",
            headers=other_headers,
            json={"match_status": "REJECTED"},
        )
        assert foreign_match_update.status_code == 404, foreign_match_update.text
        foreign_offering_update = await client.patch(
            f"/v1/offerings/{offering_id}",
            headers=other_headers,
            json={"current_price": "1.00"},
        )
        assert foreign_offering_update.status_code == 404, foreign_offering_update.text
        foreign_offering_delete = await client.delete(
            f"/v1/offerings/{offering_id}", headers=other_headers
        )
        assert foreign_offering_delete.status_code == 404, foreign_offering_delete.text
        foreign_competitor_delete = await client.delete(
            f"/v1/competitors/{competitor_id}", headers=other_headers
        )
        assert foreign_competitor_delete.status_code == 404, foreign_competitor_delete.text

        other_competitors = await client.get("/v1/competitors", headers=other_headers)
        assert other_competitors.status_code == 200, other_competitors.text
        assert all(row["id"] != competitor_id for row in other_competitors.json())
        foreign_competitor = await client.get(
            f"/v1/competitors/{competitor_id}", headers=other_headers
        )
        assert foreign_competitor.status_code == 404, foreign_competitor.text
        other_offerings = await client.get("/v1/offerings", headers=other_headers)
        assert other_offerings.status_code == 200, other_offerings.text
        assert other_offerings.json()["total_count"] == 0
        for route in (
            f"/v1/offerings/{offering_id}",
            f"/v1/sources/{source_id}",
            f"/v1/offering-matches/{match_id}",
            f"/v1/jobs/{job_id}",
        ):
            foreign_read = await client.get(route, headers=other_headers)
            assert foreign_read.status_code == 404, (
                f"Cross-tenant read was not hidden for {route}: "
                f"HTTP {foreign_read.status_code} {foreign_read.text}"
            )
        foreign_bulk_archive = await client.post(
            "/v1/offerings/bulk-archive",
            headers=other_headers,
            json={"offering_ids": [offering_id]},
        )
        assert foreign_bulk_archive.status_code == 200, foreign_bulk_archive.text
        assert foreign_bulk_archive.json()["archived_count"] == 0, foreign_bulk_archive.text
        assert foreign_bulk_archive.json()["archived_ids"] == [], foreign_bulk_archive.text
        still_active_in_owner = await client.get(
            f"/v1/offerings/{offering_id}", headers=headers
        )
        assert still_active_in_owner.status_code == 200, still_active_in_owner.text
        assert still_active_in_owner.json()["is_archived"] is False
        owner_source_read_back = await client.get(
            f"/v1/sources/{source_id}", headers=headers
        )
        assert owner_source_read_back.status_code == 200, owner_source_read_back.text
        assert owner_source_read_back.json()["name"] == "Datadog Official Pricing"
        owner_match_read_back = await client.get(
            f"/v1/offering-matches/{match_id}", headers=headers
        )
        assert owner_match_read_back.status_code == 200, owner_match_read_back.text
        assert owner_match_read_back.json()["match_status"] == "SUGGESTED"
        owner_offering_read_back = await client.get(
            f"/v1/offerings/{offering_id}", headers=headers
        )
        assert owner_offering_read_back.status_code == 200, owner_offering_read_back.text
        assert float(owner_offering_read_back.json()["current_price"]) == 2299.0

        # The API enforces the same private-target/SSRF boundary for discovery.
        blocked_target = await client.post(
            "/v1/discovery/run",
            headers=headers,
            json={
                "website_url": "http://127.0.0.1:8000/v1/health",
                "max_pages": 1,
                "default_offering_type": "PRODUCT",
            },
        )
        assert blocked_target.status_code == 422, blocked_target.text

        # Role matrix is exercised using persistent users, not forged JWT claims.
        viewer_email = await _create_role_user(client_id, RoleEnum.VIEWER, suffix)
        viewer_auth = await _login(client, viewer_email, "Nexora-Role!2026")
        viewer_headers = {"Authorization": f"Bearer {viewer_auth['access_token']}"}
        viewer_read = await client.get("/v1/competitors", headers=viewer_headers)
        assert viewer_read.status_code == 200, viewer_read.text
        viewer_write = await client.post(
            "/v1/competitors",
            headers=viewer_headers,
            json={"name": "Forbidden Competitor", "domain": f"forbidden-{suffix}.example"},
        )
        assert viewer_write.status_code == 403, viewer_write.text
        viewer_detail = await client.get(
            f"/v1/offerings/{offering_id}", headers=viewer_headers
        )
        assert viewer_detail.status_code == 403, viewer_detail.text
        viewer_discovery = await client.post(
            "/v1/discovery/run",
            headers=viewer_headers,
            json={
                "website_url": "https://www.datadoghq.com/pricing/",
                "max_pages": 1,
                "default_offering_type": "PRODUCT",
            },
        )
        assert viewer_discovery.status_code == 403, viewer_discovery.text

        analyst_email = await _create_role_user(client_id, RoleEnum.ANALYST, suffix)
        analyst_auth = await _login(client, analyst_email, "Nexora-Role!2026")
        analyst_headers = {"Authorization": f"Bearer {analyst_auth['access_token']}"}
        analyst_detail = await client.get(
            f"/v1/offerings/{offering_id}", headers=analyst_headers
        )
        assert analyst_detail.status_code == 200, analyst_detail.text
        analyst_write = await client.post(
            "/v1/competitors",
            headers=analyst_headers,
            json={"name": "Forbidden Analyst Write", "domain": f"analyst-{suffix}.example"},
        )
        assert analyst_write.status_code == 403, analyst_write.text
        analyst_blocked_target = await client.post(
            "/v1/discovery/run",
            headers=analyst_headers,
            json={
                "website_url": "http://127.0.0.1:8000/v1/health",
                "max_pages": 1,
                "default_offering_type": "PRODUCT",
            },
        )
        assert analyst_blocked_target.status_code == 422, analyst_blocked_target.text

        # A refresh token rotates once, cannot be replayed, and logout revokes it.
        rotated = await client.post(
            "/v1/auth/refresh",
            json={"refresh_token": auth["refresh_token"]},
        )
        assert rotated.status_code == 200, rotated.text
        replay = await client.post(
            "/v1/auth/refresh",
            json={"refresh_token": auth["refresh_token"]},
        )
        assert replay.status_code == 401, replay.text
        logout = await client.post(
            "/v1/auth/logout",
            json={"refresh_token": rotated.json()["refresh_token"]},
        )
        assert logout.status_code == 200, logout.text
        after_logout = await client.post(
            "/v1/auth/refresh",
            json={"refresh_token": rotated.json()["refresh_token"]},
        )
        assert after_logout.status_code == 401, after_logout.text

        # Change-password invalidates refresh sessions while preserving account access.
        password_change = await client.post(
            "/v1/auth/change-password",
            headers=headers,
            json={
                "current_password": INITIAL_PASSWORD,
                "new_password": UPDATED_PASSWORD,
            },
        )
        assert password_change.status_code == 200, password_change.text
        old_password = await client.post(
            "/v1/auth/login",
            json={"email": admin_email, "password": INITIAL_PASSWORD},
        )
        assert old_password.status_code == 401, old_password.text
        new_password = await client.post(
            "/v1/auth/login",
            json={"email": admin_email, "password": UPDATED_PASSWORD},
        )
        assert new_password.status_code == 200, new_password.text

        # A repeated collection failure must persist a circuit-breaker alert.
        async def failed_collect(
            self,
            url: str,
            timeout: float = 30.0,
            headers=None,
            custom_selectors=None,
            max_retries: int = 0,
        ) -> CollectionResult:
            return CollectionResult(
                success=False,
                url=url,
                status_code=404,
                response_time_ms=4,
                price=None,
                currency=None,
                availability=None,
                attributes={"http_status": 404},
                extraction_status="HTTP_ERROR",
                error="HTTP 404",
            )

        async def no_render(*args, **kwargs):
            return None

        monkeypatch.setattr(CollectionService, "collect", failed_collect)
        monkeypatch.setattr(collections_route, "render_page", no_render)

        circuit_rule_response = await client.post(
            "/v1/alerts/rules",
            headers=headers,
            json={
                "name": f"Source circuit opened {suffix}",
                "alert_type": "CIRCUIT_BREAKER_TRIPPED",
                "offering_id": offering_id,
                "threshold_value": None,
                "target_channels": {},
                "cooldown_minutes": 60,
                "is_active": True,
            },
        )
        assert circuit_rule_response.status_code == 201, circuit_rule_response.text
        circuit_rule_id = circuit_rule_response.json()["id"]

        for attempt in range(1, 6):
            failed_collection = await client.post(
                "/v1/collections/run",
                headers=headers,
                params={"offering_match_id": match_id},
            )
            assert failed_collection.status_code == 502, (
                f"Collection failure {attempt} should be persisted as a failed run: "
                f"HTTP {failed_collection.status_code}: {failed_collection.text}"
            )

        circuit_source_readback = await client.get(
            f"/v1/sources/{source_id}", headers=headers
        )
        assert circuit_source_readback.status_code == 200, circuit_source_readback.text
        assert circuit_source_readback.json()["failure_count"] == 5
        assert circuit_source_readback.json()["circuit_state"] == "OPEN"
        assert circuit_source_readback.json()["health_status"] == "FAILING"

        circuit_logs_response = await client.get("/v1/alerts/logs", headers=headers)
        assert circuit_logs_response.status_code == 200, circuit_logs_response.text
        circuit_alerts = [
            row for row in circuit_logs_response.json()
            if row["alert_rule_id"] == circuit_rule_id
            and row["title"] == "Competitor source circuit breaker tripped"
        ]
        assert len(circuit_alerts) == 1, circuit_alerts
        assert circuit_alerts[0]["payload_snapshot"]["event"] == "CIRCUIT_BREAKER_TRIPPED"
        assert circuit_alerts[0]["payload_snapshot"]["source_name"] == "Datadog Official Pricing"

        blocked_by_circuit = await client.post(
            "/v1/collections/run",
            headers=headers,
            params={"offering_match_id": match_id},
        )
        assert blocked_by_circuit.status_code == 503, blocked_by_circuit.text
        job_list_after_failures = await client.get("/v1/jobs", headers=headers)
        assert job_list_after_failures.status_code == 200, job_list_after_failures.text
        failed_jobs = [
            row for row in job_list_after_failures.json()
            if row["source_id"] == source_id and row["status"] == "FAILED"
        ]
        assert len(failed_jobs) == 5, failed_jobs

        # Cleanup uses the supported delete/archive lifecycle.
        assert (await client.delete(f"/v1/alerts/rules/{rule_id}", headers=headers)).status_code == 204
        assert (await client.delete(f"/v1/offering-matches/{match_id}", headers=headers)).status_code == 204
        assert (await client.delete(f"/v1/sources/{source_id}", headers=headers)).status_code == 204
        assert (await client.delete(f"/v1/competitors/{competitor_id}", headers=headers)).status_code == 204
        archived = await client.delete(f"/v1/offerings/{offering_id}", headers=headers)
        assert archived.status_code == 204, archived.text
        archived_listing = await client.get(
            "/v1/offerings",
            headers=headers,
            params={"q": "Sentinel Observability", "include_archived": True},
        )
        assert archived_listing.status_code == 200, archived_listing.text
        assert any(row["id"] == offering_id and row["is_archived"] for row in archived_listing.json()["items"])
        archived_toggle = await client.post(
            f"/v1/offerings/{offering_id}/toggle-monitoring", headers=headers
        )
        assert archived_toggle.status_code == 400, archived_toggle.text


@pytest.mark.asyncio
async def test_password_reset_token_is_consumed_once_and_replay_cannot_change_password() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://nexora.test",
    ) as client:
        email, _ = await _register(
            client,
            uuid.uuid4().hex[:10],
            "Northstar Reset Token Regression",
        )

        async with AsyncSessionLocal() as session:
            user = await session.scalar(
                select(UserModel).where(UserModel.email == email)
            )
            assert user is not None
            user_id = str(user.id)

        reset_token = generate_secure_token(32)
        await TokenSessionStore.store_password_reset_token(
            token_hash=hash_token(reset_token),
            user_id=user_id,
            email=email,
        )

        new_password = "Nexora-Reset!2026"
        reset = await client.post(
            "/v1/auth/reset-password",
            json={"token": reset_token, "new_password": new_password},
        )
        assert reset.status_code == 200, reset.text

        replay_password = "Nexora-Replay!2026"
        replay = await client.post(
            "/v1/auth/reset-password",
            json={"token": reset_token, "new_password": replay_password},
        )
        assert replay.status_code == 422, replay.text

        invalid = await client.post(
            "/v1/auth/reset-password",
            json={
                "token": f"invalid-{uuid.uuid4().hex}",
                "new_password": "Nexora-Invalid!2026",
            },
        )
        assert invalid.status_code == 422, invalid.text

        async with AsyncSessionLocal() as session:
            user = await session.scalar(
                select(UserModel).where(UserModel.email == email)
            )
            assert user is not None
            assert verify_password(new_password, user.hashed_password)
            assert not verify_password(replay_password, user.hashed_password)
            assert not verify_password("Nexora-Invalid!2026", user.hashed_password)

        accepted_login = await client.post(
            "/v1/auth/login",
            json={"email": email, "password": new_password},
        )
        assert accepted_login.status_code == 200, accepted_login.text
