from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from backend.models.enums import CircuitStateEnum, HealthStatusEnum
from backend.services.source_service import SourceService


def make_source(**overrides):
    values = {
        "is_active": True,
        "failure_count": 0,
        "consecutive_successes": 0,
        "circuit_state": CircuitStateEnum.CLOSED,
        "circuit_opened_at": None,
        "circuit_half_opened_at": None,
        "health_status": HealthStatusEnum.HEALTHY,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_circuit_breaker_opens_on_the_fifth_consecutive_failure() -> None:
    source = make_source()
    for expected_count in range(1, 5):
        assert SourceService.record_collection_failure(source) is False
        assert source.failure_count == expected_count
        assert source.consecutive_successes == 0
        assert source.circuit_state == CircuitStateEnum.CLOSED
        assert source.health_status == HealthStatusEnum.WARNING

    assert SourceService.record_collection_failure(source) is True
    assert source.failure_count == 5
    assert source.circuit_state == CircuitStateEnum.OPEN
    assert source.health_status == HealthStatusEnum.FAILING
    assert source.circuit_opened_at is not None
    assert source.circuit_half_opened_at is None


def test_open_circuit_blocks_repeated_collection_until_cooldown_expires() -> None:
    source = make_source(
        failure_count=5,
        circuit_state=CircuitStateEnum.OPEN,
        circuit_opened_at=datetime.now(timezone.utc),
        health_status=HealthStatusEnum.FAILING,
    )
    assert SourceService.before_collection(source) is False
    assert source.circuit_state == CircuitStateEnum.OPEN

    source.circuit_opened_at = datetime.now(timezone.utc) - timedelta(seconds=301)
    assert SourceService.before_collection(source) is True
    assert source.circuit_state == CircuitStateEnum.HALF_OPEN
    assert source.circuit_half_opened_at is not None
    assert source.health_status == HealthStatusEnum.WARNING


def test_half_open_circuit_allows_a_success_and_resets_the_failure_streak() -> None:
    source = make_source(
        failure_count=5,
        consecutive_successes=0,
        circuit_state=CircuitStateEnum.HALF_OPEN,
        circuit_opened_at=datetime.now(timezone.utc) - timedelta(minutes=6),
        circuit_half_opened_at=datetime.now(timezone.utc),
        health_status=HealthStatusEnum.WARNING,
    )
    SourceService.record_collection_success(source)

    assert source.failure_count == 0
    assert source.consecutive_successes == 1
    assert source.circuit_state == CircuitStateEnum.CLOSED
    assert source.circuit_opened_at is None
    assert source.circuit_half_opened_at is None
    assert source.health_status == HealthStatusEnum.HEALTHY


def test_disabled_sources_never_enter_collection() -> None:
    source = make_source(is_active=False)
    assert SourceService.before_collection(source) is False
    assert source.circuit_state == CircuitStateEnum.CLOSED


def test_success_after_an_ordinary_failure_resets_consecutive_failures() -> None:
    source = make_source(failure_count=2, consecutive_successes=0, health_status=HealthStatusEnum.WARNING)
    SourceService.record_collection_success(source)
    assert source.failure_count == 0
    assert source.consecutive_successes == 1
    assert source.health_status == HealthStatusEnum.HEALTHY
