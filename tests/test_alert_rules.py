from decimal import Decimal

from backend.models.enums import AlertTypeEnum, AvailabilityStatusEnum
from backend.services.alert_service import AlertService


def test_percentage_drop_rule_threshold():
    event = AlertService._evaluate_rule(
        AlertTypeEnum.PERCENTAGE_DROP,
        5,
        client_price=Decimal("100"),
        previous_price=Decimal("100"),
        current_price=Decimal("90"),
        previous_availability=AvailabilityStatusEnum.IN_STOCK,
        current_availability=AvailabilityStatusEnum.IN_STOCK,
        percentage_difference=-10,
    )
    assert event is not None
    assert event[0] == -10


def test_percentage_drop_rule_does_not_trigger_below_threshold():
    event = AlertService._evaluate_rule(
        AlertTypeEnum.PERCENTAGE_DROP,
        10,
        client_price=Decimal("100"),
        previous_price=Decimal("100"),
        current_price=Decimal("95"),
        previous_availability=AvailabilityStatusEnum.IN_STOCK,
        current_availability=AvailabilityStatusEnum.IN_STOCK,
        percentage_difference=-5,
    )
    assert event is None


def test_stock_change_rule():
    event = AlertService._evaluate_rule(
        AlertTypeEnum.STOCK_CHANGE,
        None,
        client_price=None,
        previous_price=Decimal("10"),
        current_price=Decimal("9"),
        previous_availability=AvailabilityStatusEnum.IN_STOCK,
        current_availability=AvailabilityStatusEnum.OUT_OF_STOCK,
        percentage_difference=-10,
    )
    assert event is not None
