"""Regression tests for security, concurrency, and reliability remediations."""

from __future__ import annotations

import asyncio
import socket
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select

from backend.core.deps import AuthenticatedUserContext, get_current_authorized_user, invalidate_authorization_cache
from backend.core.exceptions import ForbiddenException, RateLimitException
from backend.core.rate_limit import DistributedRateLimiter
from backend.db.session import AsyncSessionLocal
from backend.models.alert import AlertLogModel, AlertRuleModel
from backend.models.client import ClientModel
from backend.models.competitor import CompetitorModel, OfferingMatchModel, SourceModel
from backend.models.enums import AlertTypeEnum, ClientStatusEnum, JobStatusEnum, JobTypeEnum, RoleEnum
from backend.models.observation import JobModel, ObservationModel, SnapshotModel
from backend.models.offering import OfferingModel
from backend.schemas.alert import AlertRuleUpdate
from backend.schemas.competitor import CompetitorCreate
from backend.services.alert_service import AlertService
from backend.services.browser_proxy import SafeBrowserProxy
from backend.services.collection_service import CollectionResult, CollectionService
from backend.services.collection_runner import CollectionRunner
from backend.services.competitor_service import CompetitorService
from backend.services.discovery_service import WebsiteDiscoveryService
from backend.services.offering_service import OfferingService
from backend.services.pinned_http import PinnedDNSBackend
from backend.services.url_security import SecurityValidationError, UrlSecurityService
from backend.workers.tasks import _enqueue_monitored_match_jobs


async def _tenant_graph(*, max_competitors: int = 20, max_offerings: int = 500) -> dict:
    async with AsyncSessionLocal() as session:
        client = ClientModel(
            name=f"Remediation Tenant {uuid4().hex[:8]}",
            slug=f"remediation-{uuid4().hex}",
            status=ClientStatusEnum.ACTIVE,
            max_competitors=max_competitors,
            max_tracked_offerings=max_offerings,
        )
        session.add(client)
        await session.flush()

        competitor = CompetitorModel(
            client_id=client.id,
            name="Test Competitor",
            domain=f"competitor-{uuid4().hex}.example",
        )
        session.add(competitor)
        await session.flush()

        source = SourceModel(
            competitor_id=competitor.id,
            name="Test Source",
            base_url="https://example.com",
        )
        session.add(source)
        await session.flush()

        offering = OfferingModel(
            client_id=client.id,
            name="Test Offering",
            offering_type="PRODUCT",
            current_price=Decimal("100.00"),
            currency="USD",
            market="US",
            is_archived=False,
            is_monitored=True,
        )
        session.add(offering)
        await session.flush()

        match = OfferingMatchModel(
            offering_id=offering.id,
            source_id=source.id,
            target_url="https://example.com/product",
            is_active=True,
        )
        session.add(match)
        await session.commit()

        return {
            "client_id": client.id,
            "competitor_id": competitor.id,
            "source_id": source.id,
            "offering_id": offering.id,
            "match_id": match.id,
        }


@pytest.mark.asyncio
async def test_pinned_http_uses_one_validated_resolution_for_socket(monkeypatch):
    calls = []
    original = UrlSecurityService.resolve_and_validate_host

    def fake_resolve(hostname: str, port: int) -> str:
        calls.append((hostname, port))
        return "93.184.216.34"

    captured = {}

    async def fake_connect(ip, port, **kwargs):
        captured["ip"] = ip
        captured["port"] = port
        return object()

    monkeypatch.setattr(UrlSecurityService, "resolve_and_validate_host", fake_resolve)
    backend = PinnedDNSBackend()
    monkeypatch.setattr(backend._backend, "connect_tcp", fake_connect)

    stream = await backend.connect_tcp("rebind.example", 443)
    assert stream is not None
    assert calls == [("rebind.example", 443)]
    assert captured == {"ip": "93.184.216.34", "port": 443}


@pytest.mark.asyncio
async def test_pinned_http_rejects_dns_rebinding_before_socket_connect(monkeypatch):
    resolutions = []

    def fake_getaddrinfo(host, port, *args, **kwargs):
        resolutions.append(host)
        ip = "93.184.216.34" if len(resolutions) == 1 else "127.0.0.1"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]

    async def fake_connect(ip, port, **kwargs):
        assert ip == "93.184.216.34"
        return object()

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    backend = PinnedDNSBackend()
    monkeypatch.setattr(backend._backend, "connect_tcp", fake_connect)

    await backend.connect_tcp("rebind.example", 443)
    assert resolutions == ["rebind.example"]


@pytest.mark.asyncio
async def test_pinned_http_rejects_private_rebinding_target(monkeypatch):
    async def fake_connect(*args, **kwargs):
        raise AssertionError("private destination must never reach the socket layer")

    monkeypatch.setattr(
        UrlSecurityService,
        "resolve_and_validate_host",
        lambda hostname, port: (_ for _ in ()).throw(
            SecurityValidationError("restricted destination")
        ),
    )
    backend = PinnedDNSBackend()
    monkeypatch.setattr(backend._backend, "connect_tcp", fake_connect)

    with pytest.raises(SecurityValidationError):
        await backend.connect_tcp("rebind.example", 443)


@pytest.mark.asyncio
async def test_browser_proxy_rejects_private_target_before_connect():
    proxy = SafeBrowserProxy()

    class Reader:
        async def readuntil(self, marker: bytes) -> bytes:
            return b"CONNECT 127.0.0.1:8000 HTTP/1.1\r\nHost: 127.0.0.1:8000\r\n\r\n"

    class Writer:
        def __init__(self):
            self.data = bytearray()
        def write(self, value):
            self.data.extend(value)
        async def drain(self):
            return None
        def close(self):
            return None
        async def wait_closed(self):
            return None

    writer = Writer()
    with patch(
        "backend.services.browser_proxy.UrlSecurityService.resolve_and_validate_host",
        side_effect=SecurityValidationError("blocked"),
    ), patch(
        "backend.services.browser_proxy.asyncio.open_connection",
        new=AsyncMock(side_effect=AssertionError("upstream connect must not occur")),
    ):
        await proxy._handle_client(Reader(), writer)

    assert b"403 Forbidden" in bytes(writer.data)


@pytest.mark.asyncio
async def test_collection_exception_messages_are_sanitized(monkeypatch):
    async def fake_get(*args, **kwargs):
        raise httpx.ConnectError("secret://database-password@example.internal")

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    result = await CollectionService().collect("https://example.com/product")
    assert result.success is False
    assert "secret://" not in (result.error or "")
    assert "database-password" not in (result.error or "")
    assert result.error == "Target could not be reached safely."


@pytest.mark.asyncio
async def test_authorization_cache_rejects_disabled_or_role_changed_user():
    graph = await _tenant_graph()
    async with AsyncSessionLocal() as session:
        user = __import__("backend.models.client", fromlist=["UserModel"]).UserModel(
            client_id=graph["client_id"],
            email=f"authz-{uuid4().hex}@example.com",
            full_name="Authz Test",
            hashed_password="test",
            role=RoleEnum.ORG_ADMIN,
            is_active=True,
            email_verified=True,
        )
        session.add(user)
        await session.commit()
        user_id = user.id

    context = AuthenticatedUserContext(
        user_id=user_id,
        client_id=graph["client_id"],
        role=RoleEnum.ORG_ADMIN,
        email=f"authz-{user_id}@example.com",
        jti=str(uuid4()),
    )

    async with AsyncSessionLocal() as session:
        await get_current_authorized_user(context, session)

    async with AsyncSessionLocal() as session:
        db_user = await session.get(__import__("backend.models.client", fromlist=["UserModel"]).UserModel, user_id)
        db_user.role = RoleEnum.VIEWER
        await session.commit()

    invalidate_authorization_cache(user_id, graph["client_id"])

    async with AsyncSessionLocal() as session:
        from backend.core.exceptions import AuthenticationException
        with pytest.raises(AuthenticationException) as exc:
            await get_current_authorized_user(context, session)
        assert exc.value.code == "AUTHORIZATION_STALE"

    async with AsyncSessionLocal() as session:
        db_user = await session.get(__import__("backend.models.client", fromlist=["UserModel"]).UserModel, user_id)
        db_user.is_active = False
        await session.commit()

    invalidate_authorization_cache(user_id, graph["client_id"])

    async with AsyncSessionLocal() as session:
        from backend.core.exceptions import AuthenticationException
        with pytest.raises(AuthenticationException) as exc:
            await get_current_authorized_user(context, session)
        assert exc.value.code == "USER_DISABLED"


@pytest.mark.asyncio
async def test_alert_rule_update_persists_offering_scope():
    graph = await _tenant_graph()
    async with AsyncSessionLocal() as session:
        rule = AlertRuleModel(
            client_id=graph["client_id"],
            name="Scope rule",
            alert_type=AlertTypeEnum.PERCENTAGE_DROP,
            threshold_value=5,
            target_channels={},
            cooldown_minutes=60,
            is_active=True,
        )
        session.add(rule)
        await session.commit()
        rule_id = rule.id

    async with AsyncSessionLocal() as session:
        updated = await AlertService.update_rule(
            session,
            graph["client_id"],
            rule_id,
            AlertRuleUpdate(offering_id=graph["offering_id"]),
        )
        await session.commit()
        assert updated is not None
        assert updated.offering_id == graph["offering_id"]


@pytest.mark.asyncio
async def test_alert_cooldown_is_atomic_under_concurrency():
    graph = await _tenant_graph()
    async with AsyncSessionLocal() as session:
        rule = AlertRuleModel(
            client_id=graph["client_id"],
            name="Concurrent rule",
            alert_type=AlertTypeEnum.PRICE_CHANGE,
            threshold_value=None,
            target_channels={},
            cooldown_minutes=60,
            is_active=True,
        )
        session.add(rule)
        await session.commit()

    async def trigger() -> int:
        async with AsyncSessionLocal() as session:
            result = await AlertService.evaluate_and_trigger(
                session,
                client_id=graph["client_id"],
                offering_id=graph["offering_id"],
                offering_match_id=graph["match_id"],
                offering_name="Test Offering",
                competitor_name="Test Competitor",
                source_name="Test Source",
                client_price=Decimal("100"),
                previous_price=Decimal("100"),
                current_price=Decimal("90"),
                previous_availability=None,
                current_availability="IN_STOCK",
                percentage_difference=-10,
                circuit_tripped=False,
            )
            await session.commit()
            return result

    results = await asyncio.gather(trigger(), trigger())
    assert sorted(results) == [0, 1]

    async with AsyncSessionLocal() as session:
        count = await session.scalar(
            select(func.count(AlertLogModel.id)).join(
                AlertRuleModel,
                AlertRuleModel.id == AlertLogModel.alert_rule_id,
            ).where(AlertRuleModel.client_id == graph["client_id"])
        )
        assert count == 1


@pytest.mark.asyncio
async def test_alert_outbox_rolls_back_with_transaction():
    graph = await _tenant_graph()
    async with AsyncSessionLocal() as session:
        rule = AlertRuleModel(
            client_id=graph["client_id"],
            name="Rollback rule",
            alert_type=AlertTypeEnum.PRICE_CHANGE,
            target_channels={"email": True},
            cooldown_minutes=60,
            is_active=True,
        )
        session.add(rule)
        await session.commit()
        rule_id = rule.id

    async with AsyncSessionLocal() as session:
        await AlertService.evaluate_and_trigger(
            session,
            client_id=graph["client_id"],
            offering_id=graph["offering_id"],
            offering_match_id=graph["match_id"],
            offering_name="Test Offering",
            competitor_name="Test Competitor",
            source_name="Test Source",
            client_price=Decimal("100"),
            previous_price=Decimal("100"),
            current_price=Decimal("90"),
            previous_availability=None,
            current_availability="IN_STOCK",
            percentage_difference=-10,
        )
        await session.rollback()

    from backend.models.notification import NotificationOutboxModel
    async with AsyncSessionLocal() as session:
        assert await session.scalar(
            select(func.count(NotificationOutboxModel.id)).where(
                NotificationOutboxModel.client_id == graph["client_id"]
            )
        ) == 0
        assert await session.scalar(
            select(func.count(AlertRuleModel.id)).where(
                AlertRuleModel.client_id == graph["client_id"]
            )
        ) == 1


@pytest.mark.asyncio
async def test_discovery_cannot_bypass_competitor_quota():
    graph = await _tenant_graph(max_competitors=1)
    async with AsyncSessionLocal() as session:
        with pytest.raises(ForbiddenException):
            await WebsiteDiscoveryService._get_or_create_client_source(
                session,
                graph["client_id"],
                "https://another-domain.example/catalog",
            )
        await session.rollback()


@pytest.mark.asyncio
async def test_competitor_quota_is_safe_under_concurrency():
    graph = await _tenant_graph(max_competitors=2)

    async def create() -> str:
        async with AsyncSessionLocal() as session:
            try:
                created = await CompetitorService.create(
                    session,
                    graph["client_id"],
                    CompetitorCreate(
                        name=f"Comp-{uuid4().hex[:6]}",
                        domain=f"comp-{uuid4().hex}.example",
                    ),
                )
                return str(created.id)
            except Exception as exc:
                await session.rollback()
                return type(exc).__name__

    results = await asyncio.gather(create(), create())
    assert sum(value not in {"ForbiddenException"} for value in results) == 1

    async with AsyncSessionLocal() as session:
        count = await session.scalar(
            select(func.count(CompetitorModel.id)).where(
                CompetitorModel.client_id == graph["client_id"]
            )
        )
        assert count == 2


@pytest.mark.asyncio
async def test_offering_quota_is_safe_under_concurrency():
    graph = await _tenant_graph(max_offerings=2)

    async def create() -> str:
        async with AsyncSessionLocal() as session:
            try:
                await OfferingService.create(
                    session,
                    graph["client_id"],
                    SimpleNamespace(
                        url=None,
                        image_url=None,
                        attributes={},
                        name=f"Offering-{uuid4().hex[:6]}",
                        offering_type="PRODUCT",
                        custom_type_name=None,
                        sku=None,
                        current_price=Decimal("10"),
                        currency="USD",
                        market="US",
                        category=None,
                        description=None,
                        is_monitored=True,
                        created_via="MANUAL",
                    ),
                )
                return "created"
            except Exception as exc:
                await session.rollback()
                return type(exc).__name__

    results = await asyncio.gather(create(), create())
    assert sum(value == "created" for value in results) == 1


@pytest.mark.asyncio
async def test_offering_unarchive_cannot_bypass_quota_under_concurrency():
    graph = await _tenant_graph(max_offerings=2)
    archived_ids = []
    async with AsyncSessionLocal() as session:
        for index in range(2):
            archived = OfferingModel(
                client_id=graph["client_id"],
                name=f"Archived-{index}-{uuid4().hex[:6]}",
                offering_type="PRODUCT",
                current_price=Decimal("10"),
                currency="USD",
                market="US",
                is_archived=True,
                is_monitored=False,
            )
            session.add(archived)
            await session.flush()
            archived_ids.append(archived.id)
        await session.commit()

    async def restore(offering_id):
        async with AsyncSessionLocal() as session:
            try:
                await OfferingService.update(
                    session,
                    graph["client_id"],
                    offering_id,
                    __import__("backend.schemas.offering", fromlist=["OfferingUpdate"]).OfferingUpdate(is_archived=False),
                )
                return "created"
            except Exception as exc:
                await session.rollback()
                return getattr(exc, "status_code", type(exc).__name__)

    results = await asyncio.gather(*(restore(offering_id) for offering_id in archived_ids))
    assert sorted(results, key=str) == ["created", 402]

    async with AsyncSessionLocal() as session:
        active_count = await session.scalar(
            select(func.count(OfferingModel.id)).where(
                OfferingModel.client_id == graph["client_id"],
                OfferingModel.is_archived.is_(False),
            )
        )
        assert active_count == 2


@pytest.mark.asyncio
async def test_collection_job_claim_has_one_executor():
    graph = await _tenant_graph()
    async with AsyncSessionLocal() as session:
        job = JobModel(
            source_id=graph["source_id"],
            job_type="ON_DEMAND_REFRESH",
            status=JobStatusEnum.PENDING,
            meta_info={"offering_match_id": str(graph["match_id"])},
        )
        session.add(job)
        await session.commit()
        job_id = job.id

    async def claim():
        async with AsyncSessionLocal() as session:
            return await CollectionRunner()._claim_job(session, job_id)

    first, second = await asyncio.gather(claim(), claim())
    tokens = [first[1], second[1]]
    assert sum(token is not None for token in tokens) == 1


@pytest.mark.asyncio
async def test_stale_collection_job_can_be_reclaimed():
    graph = await _tenant_graph()
    async with AsyncSessionLocal() as session:
        job = JobModel(
            source_id=graph["source_id"],
            job_type="ON_DEMAND_REFRESH",
            status=JobStatusEnum.RUNNING,
            started_at=datetime.now(timezone.utc) - timedelta(hours=2),
            lease_expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
            meta_info={"offering_match_id": str(graph["match_id"])},
        )
        session.add(job)
        await session.commit()
        job_id = job.id

    async with AsyncSessionLocal() as session:
        reclaimed, token = await CollectionRunner()._claim_job(session, job_id)
        assert reclaimed is not None
        assert token is not None
        assert reclaimed.status == JobStatusEnum.RUNNING


@pytest.mark.asyncio
async def test_scheduler_job_creation_is_idempotent_under_concurrency():
    graph = await _tenant_graph()

    async def insert_scheduled():
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        async with AsyncSessionLocal() as session:
            stmt = (
                pg_insert(JobModel)
                .values(
                    source_id=graph["source_id"],
                    job_type="SCHEDULED_CRAWL",
                    status=JobStatusEnum.PENDING,
                    meta_info={"offering_match_id": str(graph["match_id"])},
                )
                .on_conflict_do_nothing()
                .returning(JobModel.id)
            )
            inserted = await session.scalar(stmt)
            await session.commit()
            return inserted

    ids = await asyncio.gather(insert_scheduled(), insert_scheduled())
    assert sum(item is not None for item in ids) == 1

    async with AsyncSessionLocal() as session:
        count = await session.scalar(
            select(func.count(JobModel.id)).where(
                JobModel.source_id == graph["source_id"],
                JobModel.job_type == "SCHEDULED_CRAWL",
                JobModel.status.in_([JobStatusEnum.PENDING, JobStatusEnum.RUNNING]),
            )
        )
        assert count == 1


@pytest.mark.asyncio
async def test_snapshot_transition_is_serialized():
    graph = await _tenant_graph()
    async with AsyncSessionLocal() as session:
        session.add(
            SnapshotModel(
                offering_match_id=graph["match_id"],
                current_price=Decimal("100"),
                current_availability="IN_STOCK",
                last_observed_at=datetime.now(timezone.utc),
            )
        )
        job1 = JobModel(
            source_id=graph["source_id"],
            job_type="ON_DEMAND_REFRESH",
            status=JobStatusEnum.PENDING,
            meta_info={"offering_match_id": str(graph["match_id"])},
        )
        job2 = JobModel(
            source_id=graph["source_id"],
            job_type="ON_DEMAND_REFRESH",
            status=JobStatusEnum.PENDING,
            meta_info={"offering_match_id": str(graph["match_id"])},
        )
        session.add_all([job1, job2])
        await session.commit()

    prices = iter([Decimal("110"), Decimal("120")])

    async def fake_collect(self, *args, **kwargs):
        return CollectionResult(
            success=True,
            url="https://example.com/product",
            status_code=200,
            response_time_ms=10,
            price=next(prices),
            currency="USD",
            availability="IN_STOCK",
            attributes={},
        )

    with patch.object(CollectionService, "collect", new=fake_collect):
        async def run(job_id):
            async with AsyncSessionLocal() as session:
                return await CollectionRunner().run(session, job_id)

        await asyncio.gather(run(job1.id), run(job2.id))

    async with AsyncSessionLocal() as session:
        snapshot = await session.scalar(
            select(SnapshotModel).where(
                SnapshotModel.offering_match_id == graph["match_id"]
            )
        )
        assert {Decimal(str(snapshot.previous_price)), Decimal(str(snapshot.current_price))} == {
            Decimal("110"), Decimal("120")
        }
        assert await session.scalar(
            select(func.count(ObservationModel.id)).where(
                ObservationModel.offering_match_id == graph["match_id"]
            )
        ) == 2


@pytest.mark.asyncio
async def test_discovery_history_restores_persisted_items():
    graph = await _tenant_graph()
    item = {
        "id": str(graph["offering_id"]),
        "name": "Restored Offering",
        "sku": "RESTORED-1",
        "price": "19.99",
        "currency": "USD",
        "url": "https://example.com/restored",
        "category": "Test",
        "image_url": None,
        "attributes": {"source": "history"},
        "action": "CREATED",
    }
    async with AsyncSessionLocal() as session:
        job = JobModel(
            source_id=graph["source_id"],
            job_type="SCHEMA_DISCOVERY",
            status=JobStatusEnum.COMPLETED,
            total_items_processed=1,
            successful_items=1,
            failed_items=0,
            started_at=datetime.now(timezone.utc) - timedelta(minutes=1),
            completed_at=datetime.now(timezone.utc),
            meta_info={
                "client_id": str(graph["client_id"]),
                "target_url": "https://example.com",
                "pages_crawled": 1,
                "created_count": 1,
                "updated_count": 0,
                "items": [item],
            },
        )
        session.add(job)
        await session.commit()
        job_id = job.id

    async with AsyncSessionLocal() as session:
        response = await WebsiteDiscoveryService.get_discovery_job(
            db=session,
            job_id=job_id,
            client_id=graph["client_id"],
        )

    assert len(response.items) == 1
    assert response.items[0].name == "Restored Offering"
    assert response.items[0].sku == "RESTORED-1"
    assert response.items[0].action == "CREATED"


@pytest.mark.asyncio
async def test_scheduler_reaps_legacy_running_job_without_lease():
    graph = await _tenant_graph()
    async with AsyncSessionLocal() as session:
        job = JobModel(
            source_id=graph["source_id"],
            job_type="SCHEDULED_CRAWL",
            status=JobStatusEnum.RUNNING,
            started_at=datetime.now(timezone.utc) - timedelta(seconds=7200),
            lease_expires_at=None,
            meta_info={"offering_match_id": str(graph["match_id"])},
        )
        session.add(job)
        await session.commit()
        job_id = job.id

    job_ids = await _enqueue_monitored_match_jobs()
    assert job_ids

    async with AsyncSessionLocal() as session:
        old_job = await session.scalar(
            select(JobModel).where(JobModel.id == job_id)
        )
        new_job = await session.scalar(
            select(JobModel).where(
                JobModel.source_id == graph["source_id"],
                JobModel.job_type == JobTypeEnum.SCHEDULED_CRAWL,
                JobModel.status == JobStatusEnum.PENDING,
                JobModel.meta_info["offering_match_id"].astext == str(graph["match_id"]),
            )
        )

    assert old_job is not None
    assert old_job.status == JobStatusEnum.FAILED
    assert old_job.lease_expires_at is None
    assert new_job is not None
    assert new_job.status == JobStatusEnum.PENDING


@pytest.mark.asyncio
async def test_rate_limit_is_atomic_under_concurrency(redis_client=None):
    identifier = f"remediation-{uuid4()}"
    results = await asyncio.gather(
        *[
            _run_rate_limit(identifier)
            for _ in range(10)
        ]
    )
    assert sum(result == "allowed" for result in results) == 1


async def _run_rate_limit(identifier: str) -> str:
    try:
        await DistributedRateLimiter.check_rate_limit(
            key_prefix="atomic-test",
            identifier=identifier,
            max_requests=1,
            window_seconds=60,
        )
        return "allowed"
    except RateLimitException:
        return "limited"
