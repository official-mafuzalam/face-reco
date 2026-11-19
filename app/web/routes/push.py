import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.config import settings
from app.db import get_db
from app.models import PushSubscription, User
from app.notify.push import send_push_notification, subscribe, unsubscribe

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/push", dependencies=[Depends(get_current_user)])


class PushKeys(BaseModel):
    p256dh: str
    auth: str


class PushSubscribePayload(BaseModel):
    endpoint: str
    keys: PushKeys
    quiet_start: str = "22:00"
    quiet_end: str = "06:00"
    min_severity: str = "warning"


class PushUnsubscribePayload(BaseModel):
    endpoint: str


class PushPreferencesPayload(BaseModel):
    quiet_start: str = "22:00"
    quiet_end: str = "06:00"
    min_severity: str = "warning"


@router.get("/vapid-public-key")
def get_vapid_public_key() -> dict[str, Any]:
    return {
        "public_key": settings.vapid_public_key,
        "enabled": settings.push_enabled and bool(settings.vapid_public_key),
    }


@router.post("/subscribe")
def subscribe_device(
    payload: PushSubscribePayload,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    subscribe(
        db=db,
        user_id=current_user.id,
        endpoint=payload.endpoint,
        p256dh=payload.keys.p256dh,
        auth=payload.keys.auth,
        quiet_start=payload.quiet_start,
        quiet_end=payload.quiet_end,
        min_severity=payload.min_severity,
    )
    return {"status": "ok"}


@router.post("/unsubscribe")
def unsubscribe_device(
    payload: PushUnsubscribePayload,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    sub = db.scalar(select(PushSubscription).where(PushSubscription.endpoint == payload.endpoint))
    if sub is not None:
        if sub.user_id != current_user.id and current_user.role != "admin":
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cannot unsubscribe other users' devices")
        db.delete(sub)
        db.commit()
    return {"status": "ok"}


@router.post("/preferences")
def update_push_preferences(
    payload: PushPreferencesPayload,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    db.execute(
        update(PushSubscription)
        .where(PushSubscription.user_id == current_user.id)
        .values(
            quiet_start=payload.quiet_start,
            quiet_end=payload.quiet_end,
            min_severity=payload.min_severity,
        )
    )
    db.commit()
    return {"status": "ok"}


@router.post("/test")
def test_push(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    count = send_push_notification(
        db=db,
        title="Gate Face টেস্ট নোটিফিকেশন",
        body=f"হ্যালো {current_user.username}! আপনার ফোনে পুশ নোটিফিকেশন সফলভাবে কাজ করছে।",
        url="/",
        severity="critical",
    )
    return {"status": "ok", "delivered_targets": count}
