"""Regression tests for security, concurrency, quota and durability remediations."""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import func, select
from fastapi.security import HTTPAuthorizationCredentials

from backend.core.deps import get_current_user_claims
from backend.core.exceptions import AuthenticationException, RateLimitException
from backend.core.rate_limit import DistributedRateLimiter
from backend.core.security import create_access_token, decode_and_validate_access_token
from backend.db.session import AsyncSessionLocal
from backend.models.alert import AlertLogModel, AlertRuleModel, NotificationOutboxModel
from backend.models.client import ClientModel, UserModel
from backend.models.competitor import CompetitorModel, OfferingMatchModel, SourceModel
from backend.models.enums import (
    AlertTypeEnum,
    ClientStatusEnum,
    CompetitorStatusEnum,
    JobStatusEnum,
    JobTypeEnum,
    MatchStatusEnum,
    OfferingTypeEnum,
    RoleEnum,
    SourceTypeEnum,
)
from backend.models.observation import JobModel, SnapshotModel
from backend.models.offering import OfferingModel
from backend.services.collection_runner import CollectionRunner
from backend.services.discovery_service import WebsiteDiscoveryService
from backend.services.offering_service import OfferingService
from backend.services.pinned_http import PinnedAsyncNetworkBackend
from backend.services.url_security import UrlSecurityService
from backend.workers.tasks import _enqueue_monitored_match_jobs, _recover_stale_collection_jobs


@pytest.mark.asyncio
async def test_ssrf_validation_result_is_the_address_actually_used(monkeypatch):
    calls = {"dns": 0}
    async def fake_connect(host, port, timeout=None, local_address=None, socket_options=None):
        calls["connected"] = host
        return object()

    def fake_getaddrinfo(host, port, *args, **kwargs):
        calls["dns"] += 1
        if calls["dns"] == 1:
            return [(2, 1, 6, "", ("93.184.216.34", port))]
        return [(2, 1, 6, "", ("10.0.0.5", port))]

    monkeypatch.setattr("backend.services.url_security.socket.getaddrinfo", fake_getaddrinfo)
    backend = PinnedAsyncNetworkBackend({"rebind.example": "93.184.216.34"})
    backend._delegate.connect_tcp = fake_connect
    validated = UrlSecurityService.resolve_and_validate_url("https://rebind.example/")
    await backend.connect_tcp(validated.hostname, validated.port)
    assert calls["dns"] == 1
    assert calls["connected"] == "93.184.216.34"


@pytest.mark.asyncio
async def test_rate_limit_counter_and_expiry_are_one_atomic_redis_operation():
    fake_redis = AsyncMock()
    fake_redis.eval = AsyncMock(side_effect=[1, 2])
    fake_redis.ttl = AsyncMock(return_value=60)
    with patch("backend.core.rate_limit.get_redis_client", new=AsyncMock(return_value=fake_redis)),          patch("backend.core.rate_limit.settings.rate_limit_enabled", True):
        await DistributedRateLimiter.check_rate_limit("test", "atomic", 2, 60)
        with pytest.raises(RateLimitException):
            await DistributedRateLimiter.check_rate_limit("test", "atomic", 1, 60)
    assert fake_redis.eval.await_count == 2
    assert all("EXPIRE" in call.args[0] and "INCR" in call.args[0] for call in fake_redis.eval.await_args_list)


@pytest.mark.asyncio
async def test_rate_limit_local_fallback_remains_enforced():
    with patch("backend.core.rate_limit.get_redis_client", new=AsyncMock(return_value=None)),          patch("backend.core.rate_limit.settings.rate_limit_enabled", True):
        identifier = f"fallback-{uuid.uuid4()}"
        await DistributedRateLimiter.check_rate_limit("test", identifier, 1, 60)
        with pytest.raises(RateLimitException):
            await DistributedRateLimiter.check_rate_limit("test", identifier, 1, 60)


@pytest.mark.asyncio
async def test_auth_version_rejects_disabled_user_token():
    client_id = uuid.uuid4()
    user_id = uuid.uuid4()
    async with AsyncSessionLocal() as session:
        client = ClientModel(id=client_id, name="Auth Version Tenant", slug=f"auth-version-{uuid.uuid4().hex[:8]}", status=ClientStatusEnum.ACTIVE)
        user = UserModel(
            id=user_id,
            client_id=client_id,
            email=f"auth-{uuid.uuid4().hex[:8]}@example.com",
            hashed_password="x",
            full_name="Auth User",
            role=RoleEnum.ANALYST,
            is_active=True,
            email_verified=True,
        )
        session.add_all([client, user])
        await session.commit()
        token, _, _ = create_access_token(user_id, client_id, user.role, user.email, auth_version=user.auth_version)

    async with AsyncSessionLocal() as session:
        stored = await session.get(UserModel, user_id)
        stored.is_active = False
        await session.commit()
        with patch("backend.core.rate_limit.AuthorizationStateStore.get_cached_version", new=AsyncMock(return_value=None)),              patch("backend.core.rate_limit.AuthorizationStateStore.set_version", new=AsyncMock()):
            with pytest.raises(AuthenticationException) as exc:
                await get_current_user_claims(
                    HTTPAuthorizationCredentials(scheme="Bearer", credentials=token),
                    session,
                )
            assert exc.value.code == "USER_DISABLED"


@pytest.mark.asyncio
async def test_auth_version_rejects_role_change():
    client_id = uuid.uuid4()
    user_id = uuid.uuid4()
    async with AsyncSessionLocal() as session:
        client = ClientModel(id=client_id, name="Role Version Tenant", slug=f"role-version-{uuid.uuid4().hex[:8]}", status=ClientStatusEnum.ACTIVE)
        user = UserModel(
            id=user_id,
            client_id=client_id,
            email=f"role-{uuid.uuid4().hex[:8]}@example.com",
            hashed_password="x",
            full_name="Role User",
            role=RoleEnum.ANALYST,
            is_active=True,
            email_verified=True,
        )
        session.add_all([client, user])
        await session.commit()
        token, _, _ = create_access_token(user_id, client_id, user.role, user.email, auth_version=user.auth_version)

    async with AsyncSessionLocal() as session:
        stored = await session.get(UserModel, user_id)
        stored.role = RoleEnum.VIEWER
        await session.commit()
        with patch("backend.core.rate_limit.AuthorizationStateStore.get_cached_version", new=AsyncMock(return_value=None)), \
             patch("backend.core.rate_limit.AuthorizationStateStore.set_version", new=AsyncMock()):
            with pytest.raises(AuthenticationException) as exc:
                await get_current_user_claims(
                    HTTPAuthorizationCredentials(scheme="Bearer", credentials=token),
                    session,
                )
            assert exc.value.code == "TOKEN_REVOKED"


@pytest.mark.asyncio
async def test_collection_error_is_sanitized():
    from backend.services.collection_service import CollectionService
    with patch(
        "backend.services.collection_service.pinned_get",
        new=AsyncMock(side_effect=RuntimeError("secret internal connection detail")),
    ):
        result = await CollectionService().collect("https://example.com")
    assert result.success is False
    assert "secret internal" not in (result.error or "")
    assert result.error == "Internal collection error."


@pytest.mark.asyncio
async def test_discovery_cannot_create_competitor_past_tenant_quota():
    async with AsyncSessionLocal() as session:
        client = ClientModel(
            id=uuid.uuid4(),
            name="Discovery Quota",
            slug=f"discovery-quota-{uuid.uuid4().hex[:8]}",
            status=ClientStatusEnum.ACTIVE,
            max_competitors=1,
        )
        existing = CompetitorModel(
            id=uuid.uuid4(),
            client_id=client.id,
            name="Existing",
            domain="existing.example",
            status=CompetitorStatusEnum.ACTIVE,
        )
        session.add_all([client, existing])
        await session.commit()
        with pytest.raises(HTTPException) as exc:
            await WebsiteDiscoveryService._get_or_create_client_source(
                session,
                client.id,
                "https://new.example/catalog",
            )
        assert exc.value.status_code == 402


@pytest.mark.asyncio
async def test_concurrent_offering_quota_is_not_exceeded():
    async with AsyncSessionLocal() as setup:
        client = ClientModel(
            id=uuid.uuid4(),
            name="Offering Quota",
            slug=f"offering-quota-{uuid.uuid4().hex[:8]}",
            status=ClientStatusEnum.ACTIVE,
            max_tracked_offerings=1,
        )
        setup.add(client)
        await setup.commit()
        client_id = client.id

    from backend.schemas.offering import OfferingCreate

    async def create_one(index: int):
        async with AsyncSessionLocal() as session:
            try:
                await OfferingService.create(
                    session,
                    client_id,
                    OfferingCreate(
                        name=f"Concurrent {index}",
                        offering_type=OfferingTypeEnum.PRODUCT,
                        current_price=Decimal("10"),
                        currency="USD",
                        market="US",
                    ),
                )
                return True
            except HTTPException as exc:
                return exc.status_code

    results = await asyncio.gather(create_one(1), create_one(2))
    assert results.count(True) == 1
    async with AsyncSessionLocal() as session:
        count = await session.scalar(
            select(func.count(OfferingModel.id)).where(OfferingModel.client_id == client_id)
        )
    assert count == 1


@pytest.mark.asyncio
async def test_concurrent_competitor_quota_is_not_exceeded():
    async with AsyncSessionLocal() as setup:
        client = ClientModel(
            id=uuid.uuid4(),
            name="Competitor Quota",
            slug=f"competitor-quota-{uuid.uuid4().hex[:8]}",
            status=ClientStatusEnum.ACTIVE,
            max_competitors=1,
        )
        setup.add(client)
        await setup.commit()
        client_id = client.id

    from backend.schemas.competitor import CompetitorCreate
    from backend.services.competitor_service import CompetitorService

    async def create_one(index: int):
        async with AsyncSessionLocal() as session:
            try:
                await CompetitorService.create(
                    session,
                    client_id,
                    CompetitorCreate(
                        name=f"Concurrent Rival {index}",
                        domain=f"rival-{index}-{uuid.uuid4().hex[:6]}.example",
                    ),
                )
                return True
            except Exception:
                return False

    results = await asyncio.gather(create_one(1), create_one(2))
    assert results.count(True) == 1
    async with AsyncSessionLocal() as session:
        count = await session.scalar(
            select(func.count(CompetitorModel.id)).where(CompetitorModel.client_id == client_id)
        )
    assert count == 1


@pytest.mark.asyncio
async def test_job_claim_is_single_executor():
    async with AsyncSessionLocal() as setup:
        client = ClientModel(id=uuid.uuid4(), name="Claim Tenant", slug=f"claim-{uuid.uuid4().hex[:8]}", status=ClientStatusEnum.ACTIVE)
        competitor = CompetitorModel(id=uuid.uuid4(), client_id=client.id, name="Rival", domain="rival.example")
        source = SourceModel(id=uuid.uuid4(), competitor_id=competitor.id, name="Rival Source", base_url="https://rival.example", source_type=SourceTypeEnum.OFFICIAL_STORE)
        job = JobModel(
            id=uuid.uuid4(),
            source_id=source.id,
            job_type=JobTypeEnum.SCHEDULED_CRAWL,
            status=JobStatusEnum.PENDING,
            meta_info={"offering_match_id": str(uuid.uuid4())},
        )
        setup.add_all([client, competitor, source, job])
        await setup.commit()
        job_id = job.id

    async def claim():
        async with AsyncSessionLocal() as session:
            result = await CollectionRunner._claim_job(session, job_id)
            await session.commit()
            return result is not None

    results = await asyncio.gather(claim(), claim())
    assert results.count(True) == 1


@pytest.mark.asyncio
async def test_snapshot_lock_serializes_same_match_transactions():
    async with AsyncSessionLocal() as setup:
        client = ClientModel(id=uuid.uuid4(), name="Snapshot Tenant", slug=f"snapshot-{uuid.uuid4().hex[:8]}", status=ClientStatusEnum.ACTIVE)
        competitor = CompetitorModel(id=uuid.uuid4(), client_id=client.id, name="Rival", domain="snapshot-rival.example")
        source = SourceModel(id=uuid.uuid4(), competitor_id=competitor.id, name="Rival Source", base_url="https://snapshot-rival.example")
        offering = OfferingModel(id=uuid.uuid4(), client_id=client.id, name="Item", offering_type=OfferingTypeEnum.PRODUCT, current_price=Decimal("100"), currency="USD")
        match = OfferingMatchModel(id=uuid.uuid4(), offering_id=offering.id, source_id=source.id, target_url="https://snapshot-rival.example/item", match_status=MatchStatusEnum.APPROVED)
        setup.add_all([client, competitor, source, offering, match])
        await setup.commit()
        match_id = match.id

    started = asyncio.Event()
    release = asyncio.Event()

    async def tx1():
        async with AsyncSessionLocal() as session:
            await session.execute(
                __import__("sqlalchemy").select(OfferingMatchModel.id)
                .where(OfferingMatchModel.id == match_id)
                .with_for_update()
            )
            started.set()
            await release.wait()
            await session.commit()

    async def tx2():
        async with AsyncSessionLocal() as session:
            await started.wait()
            task = asyncio.create_task(
                session.execute(
                    __import__("sqlalchemy").select(OfferingMatchModel.id)
                    .where(OfferingMatchModel.id == match_id)
                    .with_for_update()
                )
            )
            await asyncio.sleep(0.1)
            assert not task.done()
            release.set()
            await task
            await session.commit()

    await asyncio.gather(tx1(), tx2())


@pytest.mark.asyncio
async def test_scheduler_is_idempotent_under_concurrent_runs():
    async with AsyncSessionLocal() as setup:
        client = ClientModel(id=uuid.uuid4(), name="Scheduler Tenant", slug=f"scheduler-{uuid.uuid4().hex[:8]}", status=ClientStatusEnum.ACTIVE)
        competitor = CompetitorModel(id=uuid.uuid4(), client_id=client.id, name="Rival", domain="scheduler-rival.example")
        source = SourceModel(id=uuid.uuid4(), competitor_id=competitor.id, name="Rival Source", base_url="https://scheduler-rival.example")
        offering = OfferingModel(id=uuid.uuid4(), client_id=client.id, name="Item", offering_type=OfferingTypeEnum.PRODUCT, current_price=Decimal("100"), currency="USD", is_monitored=True)
        match = OfferingMatchModel(id=uuid.uuid4(), offering_id=offering.id, source_id=source.id, target_url="https://scheduler-rival.example/item", match_status=MatchStatusEnum.APPROVED, is_active=True)
        setup.add_all([client, competitor, source, offering, match])
        await setup.commit()
        client_id, match_id = client.id, match.id

    with patch("backend.workers.tasks.execute_collection_job.delay"):
        first, second = await asyncio.gather(
            _enqueue_monitored_match_jobs(),
            _enqueue_monitored_match_jobs(),
        )
    async with AsyncSessionLocal() as session:
        count = await session.scalar(
            __import__("sqlalchemy").select(__import__("sqlalchemy").func.count(JobModel.id))
            .where(
                JobModel.source_id == source.id,
                JobModel.job_type == JobTypeEnum.SCHEDULED_CRAWL,
            )
        )
    assert count == 1


@pytest.mark.asyncio
async def test_stale_running_job_is_recovered_without_reclaiming_it():
    async with AsyncSessionLocal() as setup:
        client = ClientModel(id=uuid.uuid4(), name="Stale Tenant", slug=f"stale-{uuid.uuid4().hex[:8]}", status=ClientStatusEnum.ACTIVE)
        competitor = CompetitorModel(id=uuid.uuid4(), client_id=client.id, name="Rival", domain="stale-rival.example")
        source = SourceModel(id=uuid.uuid4(), competitor_id=competitor.id, name="Rival Source", base_url="https://stale-rival.example")
        job = JobModel(id=uuid.uuid4(), source_id=source.id, status=JobStatusEnum.RUNNING, job_type=JobTypeEnum.SCHEDULED_CRAWL, started_at=datetime.now(timezone.utc)-timedelta(hours=1), lease_expires_at=datetime.now(timezone.utc)-timedelta(minutes=1), meta_info={})
        setup.add_all([client, competitor, source, job])
        await setup.commit()
    async with AsyncSessionLocal() as session:
        changed = await _recover_stale_collection_jobs(session)
        await session.commit()
        refreshed = await session.get(JobModel, job.id)
    assert changed == 1
    assert refreshed.status == JobStatusEnum.FAILED
    assert refreshed.lease_expires_at is None
    assert refreshed.error_message == "Collection worker lease expired."


@pytest.mark.asyncio
async def test_alert_cooldown_allows_only_one_concurrent_trigger():
    async with AsyncSessionLocal() as setup:
        client = ClientModel(id=uuid.uuid4(), name="Alert Tenant", slug=f"alert-{uuid.uuid4().hex[:8]}", status=ClientStatusEnum.ACTIVE)
        competitor = CompetitorModel(id=uuid.uuid4(), client_id=client.id, name="Rival", domain="alert-rival.example")
        source = SourceModel(id=uuid.uuid4(), competitor_id=competitor.id, name="Rival Source", base_url="https://alert-rival.example")
        offering = OfferingModel(id=uuid.uuid4(), client_id=client.id, name="Item", offering_type=OfferingTypeEnum.PRODUCT, current_price=Decimal("100"), currency="USD")
        match = OfferingMatchModel(id=uuid.uuid4(), offering_id=offering.id, source_id=source.id, target_url="https://alert-rival.example/item")
        rule = AlertRuleModel(id=uuid.uuid4(), client_id=client.id, offering_id=offering.id, name="Drop", alert_type=AlertTypeEnum.PERCENTAGE_DROP, threshold_value=5, target_channels={"email": False}, cooldown_minutes=60, is_active=True)
        setup.add_all([client, competitor, source, offering, match, rule])
        await setup.commit()

    async def trigger():
        async with AsyncSessionLocal() as session:
            with patch("backend.services.alert_service.AlertService._enqueue_notifications", new=AsyncMock()):
                result = await __import__("backend.services.alert_service", fromlist=["AlertService"]).AlertService.evaluate_and_trigger(
                    session,
                    client_id=client.id,
                    offering_id=offering.id,
                    offering_match_id=match.id,
                    offering_name=offering.name,
                    competitor_name=competitor.name,
                    source_name=source.name,
                    client_price=Decimal("100"),
                    previous_price=Decimal("100"),
                    current_price=Decimal("90"),
                    previous_availability=None,
                    current_availability=__import__("backend.models.enums", fromlist=["AvailabilityStatusEnum"]).AvailabilityStatusEnum.IN_STOCK,
                    percentage_difference=-10,
                )
                await session.commit()
                return result

    results = await asyncio.gather(trigger(), trigger())
    assert sorted(results) == [0, 1]
    async with AsyncSessionLocal() as session:
        count = await session.scalar(
            __import__("sqlalchemy").select(__import__("sqlalchemy").func.count(AlertLogModel.id))
            .where(AlertLogModel.id != None, AlertLogModel.alert_rule_id == rule.id)
        )
    assert count == 1


@pytest.mark.asyncio
async def test_alert_notifications_are_transactional_outbox_records():
    from backend.services.alert_service import AlertService

    async with AsyncSessionLocal() as setup:
        client = ClientModel(
            id=uuid.uuid4(),
            name="Outbox Tenant",
            slug=f"outbox-{uuid.uuid4().hex[:8]}",
            status=ClientStatusEnum.ACTIVE,
        )
        rule = AlertRuleModel(
            id=uuid.uuid4(),
            client_id=client.id,
            name="Drop",
            alert_type=AlertTypeEnum.PERCENTAGE_DROP,
            threshold_value=5,
            target_channels={"email": ["person@example.com"]},
            cooldown_minutes=60,
            is_active=True,
        )
        setup.add_all([client, rule])
        await setup.commit()

        log = AlertLogModel(
            alert_rule_id=rule.id,
            title="Competitor price dropped",
            message="The competitor price dropped 10.00%.",
            triggered_value=-10,
            payload_snapshot={},
            is_read=False,
        )
        setup.add(log)
        await setup.flush()

        await AlertService._enqueue_notifications(
            setup,
            alert_log=log,
            client_id=client.id,
            channels={"email": ["person@example.com"]},
            title=log.title,
            message=log.message,
        )
        await setup.flush()

        outbox_count = await setup.scalar(
            select(func.count(NotificationOutboxModel.id))
        )
        assert outbox_count == 1
        await setup.rollback()

    async with AsyncSessionLocal() as verify:
        count = await verify.scalar(
            select(func.count(NotificationOutboxModel.id)).where(
                NotificationOutboxModel.recipient == "person@example.com"
            )
        )
        assert count == 0


