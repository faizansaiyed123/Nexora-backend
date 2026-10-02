from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional
import uuid

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.exceptions import NotFoundException
from backend.models.alert import AlertLogModel, AlertRuleModel
from backend.models.client import UserModel
from backend.models.enums import AlertTypeEnum, AvailabilityStatusEnum
from backend.workers.email_tasks import send_competitive_alert_email_async

logger = logging.getLogger(__name__)


class AlertService:
    @staticmethod
    async def list_rules(db: AsyncSession, client_id: uuid.UUID) -> list[AlertRuleModel]:
        result = await db.execute(
            select(AlertRuleModel)
            .where(AlertRuleModel.client_id == client_id)
            .order_by(AlertRuleModel.created_at.desc())
        )
        return list(result.scalars().all())

    @staticmethod
    async def create_rule(db: AsyncSession, client_id: uuid.UUID, data: Any) -> AlertRuleModel:
        if data.offering_id is not None:
            from backend.models.offering import OfferingModel

            offering = await db.scalar(
                select(OfferingModel).where(
                    OfferingModel.id == data.offering_id,
                    OfferingModel.client_id == client_id,
                )
            )
            if offering is None:
                raise NotFoundException("Offering not found.", code="OFFERING_NOT_FOUND")

        rule = AlertRuleModel(
            client_id=client_id,
            offering_id=data.offering_id,
            name=data.name.strip(),
            alert_type=data.alert_type,
            threshold_value=data.threshold_value,
            target_channels=data.target_channels or {},
            is_active=data.is_active,
            cooldown_minutes=data.cooldown_minutes,
        )
        db.add(rule)
        await db.flush()
        return rule

    @staticmethod
    async def update_rule(
        db: AsyncSession,
        client_id: uuid.UUID,
        rule_id: uuid.UUID,
        data: Any,
    ) -> Optional[AlertRuleModel]:
        rule = await db.scalar(
            select(AlertRuleModel).where(
                AlertRuleModel.id == rule_id,
                AlertRuleModel.client_id == client_id,
            )
        )
        if rule is None:
            return None

        if data.name is not None:
            rule.name = data.name.strip()
        if data.alert_type is not None:
            rule.alert_type = data.alert_type
        if data.threshold_value is not None:
            rule.threshold_value = data.threshold_value
        if data.target_channels is not None:
            rule.target_channels = data.target_channels
        if data.cooldown_minutes is not None:
            rule.cooldown_minutes = data.cooldown_minutes
        if data.is_active is not None:
            rule.is_active = data.is_active

        await db.flush()
        return rule

    @staticmethod
    async def delete_rule(db: AsyncSession, client_id: uuid.UUID, rule_id: uuid.UUID) -> bool:
        rule = await db.scalar(
            select(AlertRuleModel).where(
                AlertRuleModel.id == rule_id,
                AlertRuleModel.client_id == client_id,
            )
        )
        if rule is None:
            return False
        await db.delete(rule)
        await db.flush()
        return True

    @staticmethod
    async def list_logs(db: AsyncSession, client_id: uuid.UUID) -> list[AlertLogModel]:
        result = await db.execute(
            select(AlertLogModel)
            .join(AlertRuleModel, AlertRuleModel.id == AlertLogModel.alert_rule_id)
            .where(AlertRuleModel.client_id == client_id)
            .order_by(AlertLogModel.created_at.desc())
            .limit(200)
        )
        return list(result.scalars().all())

    @staticmethod
    async def mark_read(
        db: AsyncSession,
        client_id: uuid.UUID,
        log_id: uuid.UUID,
    ) -> Optional[AlertLogModel]:
        result = await db.execute(
            select(AlertLogModel)
            .join(AlertRuleModel, AlertRuleModel.id == AlertLogModel.alert_rule_id)
            .where(
                AlertLogModel.id == log_id,
                AlertRuleModel.client_id == client_id,
            )
        )
        log = result.scalar_one_or_none()
        if log is None:
            return None
        log.is_read = True
        await db.flush()
        return log

    @classmethod
    async def evaluate_and_trigger(
        cls,
        db: AsyncSession,
        *,
        client_id: uuid.UUID,
        offering_id: Optional[uuid.UUID],
        offering_match_id: uuid.UUID,
        offering_name: str,
        competitor_name: str,
        source_name: str,
        client_price: Optional[Decimal],
        previous_price: Optional[Decimal],
        current_price: Optional[Decimal],
        previous_availability: Optional[AvailabilityStatusEnum],
        current_availability: AvailabilityStatusEnum,
        percentage_difference: Optional[float],
        circuit_tripped: bool = False,
    ) -> int:
        result = await db.execute(
            select(AlertRuleModel)
            .where(
                AlertRuleModel.client_id == client_id,
                AlertRuleModel.is_active.is_(True),
                or_(
                    AlertRuleModel.offering_id.is_(None),
                    AlertRuleModel.offering_id == offering_id,
                ),
            )
        )
        rules = list(result.scalars().all())
        now = datetime.now(timezone.utc)
        triggered = 0

        for rule in rules:
            if rule.last_triggered_at is not None:
                elapsed = (now - rule.last_triggered_at).total_seconds() / 60
                if elapsed < rule.cooldown_minutes:
                    continue

            event = cls._evaluate_rule(
                rule.alert_type,
                rule.threshold_value,
                client_price=client_price,
                previous_price=previous_price,
                current_price=current_price,
                previous_availability=previous_availability,
                current_availability=current_availability,
                percentage_difference=percentage_difference,
                circuit_tripped=circuit_tripped,
            )
            if event is None:
                continue

            triggered_value, title, message, payload = event
            log = AlertLogModel(
                alert_rule_id=rule.id,
                offering_match_id=offering_match_id,
                title=title,
                message=message,
                triggered_value=triggered_value,
                payload_snapshot={
                    **payload,
                    "offering_id": str(offering_id) if offering_id else None,
                    "offering_match_id": str(offering_match_id),
                    "competitor_name": competitor_name,
                    "source_name": source_name,
                },
                is_read=False,
            )
            db.add(log)
            rule.last_triggered_at = now
            await db.flush()
            triggered += 1

            await cls._notify_channels(
                db,
                client_id=client_id,
                channels=rule.target_channels or {},
                title=title,
                message=message,
            )

        return triggered

    @staticmethod
    def _evaluate_rule(
        alert_type: AlertTypeEnum,
        threshold: Optional[float],
        *,
        client_price: Optional[Decimal],
        previous_price: Optional[Decimal],
        current_price: Optional[Decimal],
        previous_availability: Optional[AvailabilityStatusEnum],
        current_availability: AvailabilityStatusEnum,
        percentage_difference: Optional[float],
        circuit_tripped: bool = False,
    ) -> Optional[tuple[Optional[float], str, str, dict[str, Any]]]:
        threshold = abs(threshold) if threshold is not None else None

        if alert_type == AlertTypeEnum.CIRCUIT_BREAKER_TRIPPED:
            if not circuit_tripped:
                return None
            return (
                None,
                "Competitor source circuit breaker tripped",
                "A competitor collection source crossed the failure threshold and was opened.",
                {"event": "CIRCUIT_BREAKER_TRIPPED"},
            )

        if alert_type == AlertTypeEnum.PRICE_CHANGE:
            if previous_price is None or current_price is None or previous_price == current_price:
                return None
            difference = float(current_price - previous_price)
            if threshold is not None and abs(difference) < threshold:
                return None
            direction = "increased" if difference > 0 else "decreased"
            return difference, "Competitor price changed", (
                f"A tracked competitor price {direction} by {abs(difference):.2f}."
            ), {
                "event": "PRICE_CHANGE",
                "previous_price": float(previous_price),
                "current_price": float(current_price),
                "difference": difference,
            }

        if alert_type == AlertTypeEnum.PERCENTAGE_DROP:
            if percentage_difference is None or percentage_difference >= 0:
                return None
            drop = abs(percentage_difference)
            if threshold is not None and drop < threshold:
                return None
            return percentage_difference, "Competitor price dropped", (
                f"A competitor price dropped {drop:.2f}%."
            ), {
                "event": "PERCENTAGE_DROP",
                "percentage_difference": percentage_difference,
            }

        if alert_type == AlertTypeEnum.UNDERCUT_THRESHOLD:
            if client_price is None or current_price is None or client_price <= 0:
                return None
            undercut_pct = ((float(current_price) - float(client_price)) / float(client_price)) * 100
            if undercut_pct >= 0:
                return None
            if threshold is not None and abs(undercut_pct) < threshold:
                return None
            return undercut_pct, "Competitor is undercutting you", (
                f"The competitor price is {abs(undercut_pct):.2f}% below your catalog price."
            ), {
                "event": "UNDERCUT_THRESHOLD",
                "client_price": float(client_price),
                "competitor_price": float(current_price),
                "undercut_percentage": undercut_pct,
            }

        if alert_type == AlertTypeEnum.STOCK_CHANGE:
            if previous_availability is None or previous_availability == current_availability:
                return None
            return None, "Competitor availability changed", (
                f"Availability changed from {previous_availability.value} to {current_availability.value}."
            ), {
                "event": "STOCK_CHANGE",
                "previous_availability": previous_availability.value,
                "current_availability": current_availability.value,
            }

        return None

    @staticmethod
    async def _notify_channels(
        db: AsyncSession,
        *,
        client_id: uuid.UUID,
        channels: dict[str, Any],
        title: str,
        message: str,
    ) -> None:
        email_setting = channels.get("email")
        if not email_setting:
            return

        if isinstance(email_setting, list):
            recipients = [str(x) for x in email_setting if x]
        else:
            result = await db.execute(
                select(UserModel.email).where(
                    UserModel.client_id == client_id,
                    UserModel.is_active.is_(True),
                    UserModel.email_verified.is_(True),
                )
            )
            recipients = [str(x) for x in result.scalars().all()]

        for email in recipients:
            try:
                await send_competitive_alert_email_async(email, title, message)
            except Exception:
                logger.warning("Competitive alert email failed for %s", email, exc_info=True)
