from __future__ import annotations

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import UiSetting
from app.schemas import CustomQuickKeyOut

CUSTOM_QUICK_KEYS_SETTING_KEY = "custom_quick_keys"


async def get_custom_quick_keys(session: AsyncSession) -> list[CustomQuickKeyOut]:
    setting = await session.get(UiSetting, CUSTOM_QUICK_KEYS_SETTING_KEY)
    if setting is None:
        return []

    raw_quick_keys = setting.value_json.get("quick_keys")
    if not isinstance(raw_quick_keys, list):
        return []

    quick_keys: list[CustomQuickKeyOut] = []
    for item in raw_quick_keys:
        try:
            quick_keys.append(CustomQuickKeyOut.model_validate(item))
        except ValidationError:
            continue
    return quick_keys


async def put_custom_quick_keys(
    session: AsyncSession,
    quick_keys: list[CustomQuickKeyOut],
) -> list[CustomQuickKeyOut]:
    setting = await session.get(UiSetting, CUSTOM_QUICK_KEYS_SETTING_KEY)
    value_json = {
        "quick_keys": [
            quick_key.model_dump(mode="json", exclude_none=True)
            for quick_key in quick_keys
        ],
    }
    if setting is None:
        setting = UiSetting(key=CUSTOM_QUICK_KEYS_SETTING_KEY, value_json=value_json)
        session.add(setting)
    else:
        setting.value_json = value_json

    await session.flush()
    return quick_keys
