from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Optional
import uuid

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.exceptions import NotFoundException
from backend.models.alert import AlertLogModel, AlertRuleModel, NotificationOutboxModel
from backend.models.client import UserModel
from backend.models.enums import AlertTypeEnum, AvailabilityStatusEnum

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

        if data.offering_id is not None or "offering_id" in data.model_fields_set:
            if data.offering_id is None:
                rule.offering_id = None
            else:
                from backend.models.offering import OfferingModel
                offering = await db.scalar(
                    select(OfferingModel).where(
                        OfferingModel.id == data.offering_id,
                        OfferingModel.client_id == client_id,
                    )
                )
                if offering is None:
                    raise NotFoundException("Offering not found.", code="OFFERING_NOT_FOUND")
                rule.offering_id = data.offering_id

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

        for candidate_rule in rules:
            event = cls._evaluate_rule(
                candidate_rule.alert_type,
                candidate_rule.threshold_value,
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
            cutoff = now - timedelta(minutes=candidate_rule.cooldown_minutes)
            claimed = await db.scalar(
                update(AlertRuleModel)
                .where(
                    AlertRuleModel.id == candidate_rule.id,
                    AlertRuleModel.client_id == client_id,
                    AlertRuleModel.is_active.is_(True),
                    or_(
                        AlertRuleModel.last_triggered_at.is_(None),
                        AlertRuleModel.last_triggered_at <= cutoff,
                    ),
                )
                .values(last_triggered_at=now)
                .returning(AlertRuleModel.id)
            )
            if claimed is None:
                continue

            log = AlertLogModel(
                alert_rule_id=candidate_rule.id,
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
            await db.flush()
            await cls._enqueue_notifications(
                db,
                alert_log=log,
                client_id=client_id,
                channels=candidate_rule.target_channels or {},
                title=title,
                message=message,
            )
            await db.flush()
            triggered += 1
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
    async def _enqueue_notifications(
        db: AsyncSession,
        *,
        alert_log: AlertLogModel,
        client_id: uuid.UUID,
        channels: dict[str, Any],
        title: str,
        message: str,
    ) -> int:
        """Queue notification records transactionally; delivery happens after commit."""
        email_setting = channels.get("email")
        if not email_setting:
            return 0

        if isinstance(email_setting, (list, tuple, set)):
            recipients = [str(x).strip().lower() for x in email_setting if str(x).strip()]
        elif isinstance(email_setting, str) and email_setting.strip():
            recipients = [email_setting.strip().lower()]
        else:
            result = await db.execute(
                select(UserModel.email).where(
                    UserModel.client_id == client_id,
                    UserModel.is_active.is_(True),
                    UserModel.email_verified.is_(True),
                )
            )
            recipients = [str(x).strip().lower() for x in result.scalars().all()]

        rows: list[NotificationOutboxModel] = []
        for email in dict.fromkeys(recipients):
            dedupe_key = hashlib.sha256(
                f"{alert_log.id}:EMAIL:{email}".encode("utf-8")
            ).hexdigest()
            rows.append(
                NotificationOutboxModel(
                    alert_log_id=alert_log.id,
                    recipient=email,
                    channel="EMAIL",
                    subject=f"Nexora Competitive Alert: {title}",
                    body=(
                        "Hello,\n\n"
                        "Nexora detected a competitive intelligence event:\n\n"
                        f"{title}\n\n"
                        f"{message}\n\n"
                        "Open your Nexora workspace to review the latest observation "
                        "and decide whether action is needed.\n\n"
                        "Regards,\nNexora"
                    ),
                    status="PENDING",
                    attempt_count=0,
                    next_attempt_at=datetime.now(timezone.utc),
                    dedupe_key=dedupe_key,
                )
            )

        if rows:
            db.add_all(rows)
            await db.flush()

        return len(rows)
