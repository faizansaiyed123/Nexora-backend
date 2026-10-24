from decimal import Decimal

from backend.models.enums import AlertTypeEnum, AvailabilityStatusEnum
from backend.services.alert_service import AlertService


IN_STOCK = AvailabilityStatusEnum.IN_STOCK
OUT_OF_STOCK = AvailabilityStatusEnum.OUT_OF_STOCK


def evaluate(
    alert_type: AlertTypeEnum,
    threshold: float | None = None,
    *,
    client_price: Decimal | None = Decimal("100.00"),
    previous_price: Decimal | None = Decimal("100.00"),
    current_price: Decimal | None = Decimal("100.00"),
    previous_availability: AvailabilityStatusEnum | None = IN_STOCK,
    current_availability: AvailabilityStatusEnum = IN_STOCK,
    percentage_difference: float | None = 0.0,
    circuit_tripped: bool = False,
):
    return AlertService._evaluate_rule(
        alert_type,
        threshold,
        client_price=client_price,
        previous_price=previous_price,
        current_price=current_price,
        previous_availability=previous_availability,
        current_availability=current_availability,
        percentage_difference=percentage_difference,
        circuit_tripped=circuit_tripped,
    )


def test_price_change_alert_uses_absolute_price_threshold() -> None:
    event = evaluate(
        AlertTypeEnum.PRICE_CHANGE,
        10,
        previous_price=Decimal("100.00"),
        current_price=Decimal("112.50"),
    )
    assert event is not None
    assert event[0] == 12.5
    assert event[1] == "Competitor price changed"
    assert event[3]["event"] == "PRICE_CHANGE"

    assert evaluate(
        AlertTypeEnum.PRICE_CHANGE,
        10,
        previous_price=Decimal("100.00"),
        current_price=Decimal("105.00"),
    ) is None
    assert evaluate(
        AlertTypeEnum.PRICE_CHANGE,
        None,
        previous_price=Decimal("100.00"),
        current_price=Decimal("100.00"),
    ) is None
    assert evaluate(
        AlertTypeEnum.PRICE_CHANGE,
        1,
        previous_price=None,
        current_price=Decimal("105.00"),
    ) is None


def test_percentage_drop_alert_obeys_direction_and_threshold() -> None:
    event = evaluate(
        AlertTypeEnum.PERCENTAGE_DROP,
        10,
        previous_price=Decimal("100.00"),
        current_price=Decimal("87.50"),
        percentage_difference=-12.5,
    )
    assert event is not None
    assert event[0] == -12.5
    assert event[3]["event"] == "PERCENTAGE_DROP"

    assert evaluate(
        AlertTypeEnum.PERCENTAGE_DROP,
        10,
        percentage_difference=-9.99,
    ) is None
    assert evaluate(
        AlertTypeEnum.PERCENTAGE_DROP,
        1,
        percentage_difference=12.5,
    ) is None
    assert evaluate(
        AlertTypeEnum.PERCENTAGE_DROP,
        1,
        percentage_difference=None,
    ) is None


def test_undercut_alert_compares_competitor_to_own_catalog_price() -> None:
    event = evaluate(
        AlertTypeEnum.UNDERCUT_THRESHOLD,
        10,
        client_price=Decimal("100.00"),
        current_price=Decimal("85.00"),
    )
    assert event is not None
    assert event[0] == -15
    assert event[3]["event"] == "UNDERCUT_THRESHOLD"
    assert event[3]["client_price"] == 100.0
    assert event[3]["competitor_price"] == 85.0

    assert evaluate(
        AlertTypeEnum.UNDERCUT_THRESHOLD,
        10,
        client_price=Decimal("100.00"),
        current_price=Decimal("95.00"),
    ) is None
    assert evaluate(
        AlertTypeEnum.UNDERCUT_THRESHOLD,
        1,
        client_price=Decimal("100.00"),
        current_price=Decimal("105.00"),
    ) is None
    assert evaluate(
        AlertTypeEnum.UNDERCUT_THRESHOLD,
        1,
        client_price=Decimal("0.00"),
        current_price=Decimal("1.00"),
    ) is None


def test_stock_change_alert_requires_a_real_availability_transition() -> None:
    event = evaluate(
        AlertTypeEnum.STOCK_CHANGE,
        None,
        previous_availability=IN_STOCK,
        current_availability=OUT_OF_STOCK,
    )
    assert event is not None
    assert event[1] == "Competitor availability changed"
    assert event[3] == {
        "event": "STOCK_CHANGE",
        "previous_availability": "IN_STOCK",
        "current_availability": "OUT_OF_STOCK",
    }

    assert evaluate(
        AlertTypeEnum.STOCK_CHANGE,
        None,
        previous_availability=IN_STOCK,
        current_availability=IN_STOCK,
    ) is None
    assert evaluate(
        AlertTypeEnum.STOCK_CHANGE,
        None,
        previous_availability=None,
        current_availability=OUT_OF_STOCK,
    ) is None


def test_circuit_breaker_alert_requires_a_tripped_source() -> None:
    event = evaluate(
        AlertTypeEnum.CIRCUIT_BREAKER_TRIPPED,
        None,
        circuit_tripped=True,
    )
    assert event is not None
    assert event[3]["event"] == "CIRCUIT_BREAKER_TRIPPED"

    assert evaluate(
        AlertTypeEnum.CIRCUIT_BREAKER_TRIPPED,
        None,
        circuit_tripped=False,
    ) is None
