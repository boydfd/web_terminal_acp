from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import HTTPException, status

TERMINAL_TIME_RANGE_DAYS = {
    "1d": 1,
    "3d": 3,
    "5d": 5,
    "7d": 7,
    "14d": 14,
    "30d": 30,
}


def terminal_visible_since(range_value: str | None, *, now: datetime | None = None) -> datetime | None:
    if range_value in (None, "", "all"):
        return None

    days = TERMINAL_TIME_RANGE_DAYS.get(range_value)
    if days is None:
        allowed = ", ".join([*TERMINAL_TIME_RANGE_DAYS.keys(), "all"])
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"invalid terminal time range; expected one of: {allowed}",
        )

    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    return current - timedelta(days=days)
