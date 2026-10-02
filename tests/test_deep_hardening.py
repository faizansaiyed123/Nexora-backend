import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.rate_limit import TokenSessionStore
from backend.models.competitor import SourceModel
from backend.models.enums import CircuitStateEnum, HealthStatusEnum, AvailabilityStatusEnum, AlertTypeEnum
from backend.services.alert_service import AlertService
from backend.services.collection_service import CollectionService
from backend.services.source_service import SourceService


def test_circuit_breaker_opens_once_and_recovers():
    source = SourceModel(
        competitor_id=uuid.uuid4(),
        name="Test Source",
        base_url="https://example.com",
    )
    assert SourceService.before_collection(source) is True

    for _ in range(4):
        assert SourceService.record_collection_failure(source) is False
    assert source.failure_count == 4
    assert source.health_status == HealthStatusEnum.WARNING

    assert SourceService.record_collection_failure(source) is True
    assert source.circuit_state == CircuitStateEnum.OPEN
    assert SourceService.record_collection_failure(source) is False

    assert SourceService.before_collection(source) is False

    source.circuit_opened_at = datetime.now(timezone.utc) - timedelta(minutes=6)
    assert SourceService.before_collection(source) is True
    assert source.circuit_state == CircuitStateEnum.HALF_OPEN

    SourceService.record_collection_success(source)
    assert source.circuit_state == CircuitStateEnum.CLOSED
    assert source.failure_count == 0
    assert source.health_status == HealthStatusEnum.HEALTHY


def test_circuit_breaker_alert_only_on_transition():
    event = AlertService._evaluate_rule(
        AlertTypeEnum.CIRCUIT_BREAKER_TRIPPED,
        None,
        client_price=Decimal("100"),
        previous_price=Decimal("100"),
        current_price=Decimal("90"),
        previous_availability=AvailabilityStatusEnum.IN_STOCK,
        current_availability=AvailabilityStatusEnum.IN_STOCK,
        percentage_difference=-10,
        circuit_tripped=True,
    )
    assert event is not None

    no_event = AlertService._evaluate_rule(
        AlertTypeEnum.CIRCUIT_BREAKER_TRIPPED,
        None,
        client_price=None,
        previous_price=None,
        current_price=None,
        previous_availability=None,
        current_availability=AvailabilityStatusEnum.UNKNOWN,
        percentage_difference=None,
        circuit_tripped=False,
    )
    assert no_event is None


def test_rendered_html_extraction_supports_custom_selectors():
    html = """
    <html><body>
      <h1>Example Pro</h1>
      <div class="live-price">$123.45</div>
      <div class="live-currency">USD</div>
      <div class="live-stock">In stock</div>
    </body></html>
    """
    result = CollectionService().from_rendered_html(
        html,
        "https://example.com/product",
        custom_selectors={
            "price_selector": ".live-price",
            "currency_selector": ".live-currency",
            "availability_selector": ".live-stock",
        },
    )
    assert result.success is True
    assert result.price == Decimal("123.45")
    assert result.currency == "USD"
    assert result.availability == "IN_STOCK"


@pytest.mark.asyncio
async def test_http_redirect_is_revalidated():
    service = CollectionService()
    first = SimpleNamespace(
        status_code=302,
        headers={"location": "http://127.0.0.1:8000/private"},
        content=b"",
        text="",
        url="https://public.example/",
    )

    async def fake_get(*args, **kwargs):
        return first

    with patch("httpx.AsyncClient.get", new=AsyncMock(side_effect=fake_get)):
        result = await service.collect("https://public.example/")
    assert result.success is False
    assert result.extraction_status == "SECURITY_BLOCKED"


@pytest.mark.asyncio
async def test_refresh_session_fails_closed_without_redis():
    with patch("backend.core.rate_limit.get_redis_client", new=AsyncMock(return_value=None)):
        ok = await TokenSessionStore.validate_and_rotate_refresh_session(
            user_id=str(uuid.uuid4()),
            session_id="session",
            provided_token_hash="old",
            new_token_hash="new",
        )
    assert ok is False
