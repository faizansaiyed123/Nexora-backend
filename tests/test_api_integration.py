"""PostgreSQL-backed API integration coverage for Nexora's core tenant workflows.

These tests exercise the real FastAPI routes and persisted records. The CI workflow
provides disposable PostgreSQL and Redis services and enables verification bypass
only for its isolated test database so onboarding does not require an external inbox.
"""
from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from backend.core.security import get_password_hash
from backend.core.rate_limit import TokenSessionStore
from backend.db.session import AsyncSessionLocal
from backend.main import app
from backend.models.alert import AlertLogModel
from backend.models.client import UserModel
from backend.models.enums import RoleEnum


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
async def test_full_tenant_api_and_database_workflow() -> None:
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
