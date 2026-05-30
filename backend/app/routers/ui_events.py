from __future__ import annotations

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect

from app.auth import require_websocket_auth
from app.services.ui_events import UiEventHub

router = APIRouter(prefix="/api", tags=["ui-events"])


def ui_event_hub_from_state(state: object) -> UiEventHub:
    hub = getattr(state, "ui_event_hub", None)
    if hub is None:
        hub = UiEventHub()
        setattr(state, "ui_event_hub", hub)
    return hub


@router.get("/ui-events")
async def read_ui_events_status(request: Request) -> dict[str, str]:
    ui_event_hub_from_state(request.app.state)
    return {"status": "ok"}


@router.websocket("/ui-events")
async def ui_events_websocket(websocket: WebSocket) -> None:
    if not await require_websocket_auth(websocket):
        return

    hub = ui_event_hub_from_state(websocket.app.state)
    await websocket.accept()
    await websocket.send_json({"type": "connected", "seq": 0})
    await hub.subscribe(websocket.send_text)
    try:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                return
    except WebSocketDisconnect:
        return
    finally:
        await hub.unsubscribe(websocket.send_text)
