from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.repositories.ui_settings import get_custom_quick_keys, put_custom_quick_keys
from app.routers.ui_events import ui_event_hub_from_state
from app.schemas import CustomQuickKeysOut, CustomQuickKeysPutIn

router = APIRouter(prefix="/api/ui-settings", tags=["ui-settings"])


@router.get(
    "/custom-quick-keys",
    response_model=CustomQuickKeysOut,
    response_model_exclude_none=True,
)
async def read_custom_quick_keys(
    session: AsyncSession = Depends(get_session),
) -> CustomQuickKeysOut:
    return CustomQuickKeysOut(quick_keys=await get_custom_quick_keys(session))


@router.put(
    "/custom-quick-keys",
    response_model=CustomQuickKeysOut,
    response_model_exclude_none=True,
)
async def update_custom_quick_keys(
    payload: CustomQuickKeysPutIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> CustomQuickKeysOut:
    quick_keys = await put_custom_quick_keys(session, payload.quick_keys)
    await session.commit()
    await ui_event_hub_from_state(request.app.state).publish_invalidation(
        ["ui_settings"],
        reason="custom_quick_keys_updated",
    )
    return CustomQuickKeysOut(quick_keys=quick_keys)
