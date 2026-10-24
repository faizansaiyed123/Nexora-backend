"""Regression coverage for alert delivery when collection opens a source circuit."""
from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from backend.api.v1 import collections as collections_route
from backend.core.config import get_settings
from backend.main import app
from backend.services.collection_service import CollectionResult, CollectionService


PASSWORD = "Nexora-Circuit!2026"


@pytest.mark.asyncio
async def test_circuit_breaker_trip_creates_a_persistent_alert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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

    # This test targets circuit behavior, not Redis connection-pool lifecycle; keep the
    # request limiter disabled so each pytest event loop does not reuse a Redis client
    # created by a preceding async test.
    monkeypatch.setattr(get_settings(), "rate_limit_enabled", False)
    monkeypatch.setattr(CollectionService, "collect", failed_collect)
    monkeypatch.setattr(collections_route, "render_page", no_render)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://nexora-circuit.test",
    ) as client:
        suffix = uuid.uuid4().hex[:12]
        email = f"circuit-{suffix}@example.com"
        register = await client.post(
            "/v1/auth/register",
            json={
                "organization_name": f"Circuit Breaker Review {suffix}",
                "full_name": "Circuit Breaker Reviewer",
                "email": email,
                "password": PASSWORD,
            },
        )
        assert register.status_code == 201, register.text
        assert register.json()["email_verification_required"] is False, register.text

        login = await client.post(
            "/v1/auth/login",
            json={"email": email, "password": PASSWORD},
        )
        assert login.status_code == 200, login.text
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

        competitor_response = await client.post(
            "/v1/competitors",
            headers=headers,
            json={
                "name": f"Unavailable Retail Endpoint {suffix}",
                "domain": f"circuit-{suffix}.example.com",
            },
        )
        assert competitor_response.status_code == 201, competitor_response.text
        competitor_id = competitor_response.json()["id"]

        source_response = await client.post(
            "/v1/sources",
            headers=headers,
            json={
                "competitor_id": competitor_id,
                "name": f"Unavailable Storefront {suffix}",
                "base_url": "https://www.example.com/pricing/",
                "source_type": "OFFICIAL_STORE",
                "collection_method": "HTTP_FAST",
                "configuration": {
                    "rate_limit_rpm": 600,
                    "request_delay_seconds": 0,
                    "timeout_seconds": 5,
                    "max_retries": 0,
                    "user_agent_strategy": "ROTATE_CHROME",
                    "requires_proxy": False,
                    "requires_javascript": False,
                    "custom_headers": {},
                    "extraction_selectors": {},
                },
            },
        )
        assert source_response.status_code == 201, source_response.text
        source_id = source_response.json()["id"]

        offering_response = await client.post(
            "/v1/offerings",
            headers=headers,
            json={
                "name": f"Circuit Probe Offering {suffix}",
                "offering_type": "PRODUCT",
                "sku": f"CIRCUIT-{suffix}",
                "current_price": "100.00",
                "currency": "USD",
                "market": "US",
                "url": "https://www.example.com/products/circuit-probe",
                "category": "Test Fixtures > Circuit Breaker",
                "attributes": {"scenario": "persistent-source-failure"},
                "is_monitored": True,
            },
        )
        assert offering_response.status_code == 201, offering_response.text
        offering_id = offering_response.json()["id"]

        match_response = await client.post(
            "/v1/offering-matches",
            headers=headers,
            json={
                "offering_id": offering_id,
                "source_id": source_id,
                "target_url": "https://www.example.com/products/not-found",
                "match_status": "APPROVED",
                "confidence_score": 0.99,
            },
        )
        assert match_response.status_code == 201, match_response.text
        match_id = match_response.json()["id"]

        rule_response = await client.post(
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
        assert rule_response.status_code == 201, rule_response.text
        rule_id = rule_response.json()["id"]

        # The real collection route persists each failed job and advances the
        # source-health state machine. Only the external fetch is deterministic.
        for attempt in range(1, 6):
            collection = await client.post(
                "/v1/collections/run",
                headers=headers,
                params={"offering_match_id": match_id},
            )
            assert collection.status_code == 502, (
                f"Attempt {attempt} should persist an HTTP collection failure: "
                f"{collection.status_code} {collection.text}"
            )

        source_readback = await client.get(
            f"/v1/sources/{source_id}", headers=headers
        )
        assert source_readback.status_code == 200, source_readback.text
        assert source_readback.json()["failure_count"] == 5
        assert source_readback.json()["circuit_state"] == "OPEN"
        assert source_readback.json()["health_status"] == "FAILING"

        logs_response = await client.get("/v1/alerts/logs", headers=headers)
        assert logs_response.status_code == 200, logs_response.text
        circuit_alerts = [
            log for log in logs_response.json()
            if log["alert_rule_id"] == rule_id
            and log["title"] == "Competitor source circuit breaker tripped"
        ]
        assert len(circuit_alerts) == 1, (
            "Opening a source circuit should create one persistent alert; "
            f"received {circuit_alerts!r}"
        )
        assert circuit_alerts[0]["payload_snapshot"]["event"] == "CIRCUIT_BREAKER_TRIPPED"
        assert circuit_alerts[0]["payload_snapshot"]["source_name"] == f"Unavailable Storefront {suffix}"

        # Once open, the source is blocked without another fetch or job row.
        blocked = await client.post(
            "/v1/collections/run",
            headers=headers,
            params={"offering_match_id": match_id},
        )
        assert blocked.status_code == 503, blocked.text
        jobs_response = await client.get("/v1/jobs", headers=headers)
        assert jobs_response.status_code == 200, jobs_response.text
        failed_jobs = [
            row for row in jobs_response.json()
            if row["source_id"] == source_id and row["status"] == "FAILED"
        ]
        assert len(failed_jobs) == 5, failed_jobs
