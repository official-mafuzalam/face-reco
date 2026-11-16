"""Live notifications for every open page (Server-Sent Events) and acknowledging them."""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.db import get_db
from app.models import AuditLog, Event, Incident, User
from app.notify.bus import Notification, bus

router = APIRouter(prefix="/notify", dependencies=[Depends(get_current_user)])
KEEPALIVE_SECONDS = 15.0


def _frame(notification: Notification) -> bytes:
    return f"id: {notification.id}\nevent: notify\ndata: {notification.to_json()}\n\n".encode()


async def _stream(request: Request, last_id: int) -> AsyncIterator[bytes]:
    loop, queue = bus.subscribe()
    try:
        # Tell the browser how long to wait before reconnecting after a drop.
        yield b"retry: 3000\n\n"
        for notification in bus.since(last_id):
            yield _frame(notification)
        while not await request.is_disconnected():
            try:
                notification = await asyncio.wait_for(queue.get(), timeout=KEEPALIVE_SECONDS)
            except TimeoutError:
                yield b": keepalive\n\n"
                continue
            if notification.id > last_id:
                last_id = notification.id
                yield _frame(notification)
    finally:
        bus.unsubscribe(loop, queue)


@router.get("/stream")
def stream(request: Request) -> StreamingResponse:
    # The page passes the last id it saw (kept across page loads), so nothing raised while
    # moving between pages is lost. A first visit starts from "now"; older alarms are on
    # the Alerts and Surveillance pages, not replayed as pop-ups.
    header = request.headers.get("last-event-id") or request.query_params.get("since") or ""
    last_id = min(int(header), bus.last_id) if header.isdigit() else bus.last_id
    return StreamingResponse(
        _stream(request, last_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


class Ack(BaseModel):
    type: str
    id: int


@router.post("/ack")
def acknowledge(ack: Ack, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> JSONResponse:
    """Mark the incident or unknown-face event behind an alarm as reviewed."""
    if ack.type == "incident":
        item = db.get(Incident, ack.id)
        action = "incident.review"
    elif ack.type == "event":
        item = db.get(Event, ack.id)  # an unknown face, or a matched blacklisted person
        action = "event.review"
    else:
        raise HTTPException(status_code=422, detail="Unknown notification type")
    if item is None:
        raise HTTPException(status_code=404, detail="Not found")
    if item.reviewed_at is None:
        item.reviewed_by = user.id
        item.reviewed_at = datetime.now(UTC)
        db.add(AuditLog(user_id=user.id, action=action, detail=f"{ack.type}_id={ack.id}; via=live alarm"))
        db.commit()
    return JSONResponse({"ok": True})
