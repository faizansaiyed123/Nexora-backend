import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.deps import AuthenticatedUserContext, get_current_user_claims, require_admin, require_analyst, require_reader
from backend.db.session import get_db
from backend.schemas.alert import AlertLogRead, AlertRuleCreate, AlertRuleRead, AlertRuleUpdate
from backend.services.alert_service import AlertService

router = APIRouter()


@router.get("/rules", response_model=list[AlertRuleRead], dependencies=[Depends(require_reader)])
async def list_rules(
    auth_ctx: Annotated[AuthenticatedUserContext, Depends(get_current_user_claims)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    return await AlertService.list_rules(db, auth_ctx.client_id)


@router.post(
    "/rules",
    response_model=AlertRuleRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_admin)],
)
async def create_rule(
    data: AlertRuleCreate,
    auth_ctx: Annotated[AuthenticatedUserContext, Depends(get_current_user_claims)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    rule = await AlertService.create_rule(db, auth_ctx.client_id, data)
    await db.commit()
    await db.refresh(rule)
    return rule


@router.patch(
    "/rules/{rule_id}",
    response_model=AlertRuleRead,
    dependencies=[Depends(require_admin)],
)
async def update_rule(
    rule_id: uuid.UUID,
    data: AlertRuleUpdate,
    auth_ctx: Annotated[AuthenticatedUserContext, Depends(get_current_user_claims)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    rule = await AlertService.update_rule(db, auth_ctx.client_id, rule_id, data)
    if rule is None:
        raise HTTPException(status_code=404, detail="Alert rule not found.")
    await db.commit()
    await db.refresh(rule)
    return rule


@router.delete("/rules/{rule_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(require_admin)])
async def delete_rule(
    rule_id: uuid.UUID,
    auth_ctx: Annotated[AuthenticatedUserContext, Depends(get_current_user_claims)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    if not await AlertService.delete_rule(db, auth_ctx.client_id, rule_id):
        raise HTTPException(status_code=404, detail="Alert rule not found.")
    await db.commit()
    return None


@router.get("/logs", response_model=list[AlertLogRead], dependencies=[Depends(require_reader)])
async def list_logs(
    auth_ctx: Annotated[AuthenticatedUserContext, Depends(get_current_user_claims)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    return await AlertService.list_logs(db, auth_ctx.client_id)


@router.patch("/logs/{log_id}/read", response_model=AlertLogRead, dependencies=[Depends(require_analyst)])
async def mark_log_read(
    log_id: uuid.UUID,
    auth_ctx: Annotated[AuthenticatedUserContext, Depends(get_current_user_claims)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    log = await AlertService.mark_read(db, auth_ctx.client_id, log_id)
    if log is None:
        raise HTTPException(status_code=404, detail="Alert log not found.")
    await db.commit()
    await db.refresh(log)
    return log
