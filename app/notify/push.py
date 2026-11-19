import json
import logging
from datetime import datetime
import threading
from zoneinfo import ZoneInfo

from pywebpush import WebPushException, webpush
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import SessionLocal
from app.models import PushSubscription

logger = logging.getLogger(__name__)

_SEVERITY_LEVELS = {
    "info": 1,
    "warning": 2,
    "critical": 3,
}


def is_in_quiet_hours(quiet_start: str, quiet_end: str, now_hhmm: str) -> bool:
    """Checks whether now_hhmm falls within quiet hours (inclusive of start, exclusive of end)."""
    if not quiet_start or not quiet_end or quiet_start == quiet_end:
        return False
    if quiet_start < quiet_end:
        return quiet_start <= now_hhmm < quiet_end
    # Overnight range, e.g. 22:00 to 06:00
    return now_hhmm >= quiet_start or now_hhmm < quiet_end


def subscribe(
    db: Session,
    user_id: int,
    endpoint: str,
    p256dh: str,
    auth: str,
    quiet_start: str = "22:00",
    quiet_end: str = "06:00",
    min_severity: str = "warning",
) -> PushSubscription:
    sub = db.scalar(select(PushSubscription).where(PushSubscription.endpoint == endpoint))
    if sub is None:
        sub = PushSubscription(
            user_id=user_id,
            endpoint=endpoint,
            p256dh=p256dh,
            auth=auth,
            quiet_start=quiet_start,
            quiet_end=quiet_end,
            min_severity=min_severity,
        )
        db.add(sub)
    else:
        sub.user_id = user_id
        sub.p256dh = p256dh
        sub.auth = auth
        sub.quiet_start = quiet_start
        sub.quiet_end = quiet_end
        sub.min_severity = min_severity
    db.commit()
    return sub


def unsubscribe(db: Session, endpoint: str) -> bool:
    sub = db.scalar(select(PushSubscription).where(PushSubscription.endpoint == endpoint))
    if sub is not None:
        db.delete(sub)
        db.commit()
        return True
    return False


def _send_worker(payload_str: str, subs_data: list[dict], vapid_key: str, claims: dict) -> None:
    stale_endpoints: list[str] = []
    for item in subs_data:
        sub_info = {
            "endpoint": item["endpoint"],
            "keys": {
                "p256dh": item["p256dh"],
                "auth": item["auth"],
            },
        }
        try:
            webpush(
                subscription_info=sub_info,
                data=payload_str,
                vapid_private_key=vapid_key,
                vapid_claims=claims,
                ttl=86400,
            )
        except WebPushException as exc:
            # 404 or 410 Gone indicates the user unregistered or revoked permission.
            if exc.response is not None and exc.response.status_code in (404, 410):
                stale_endpoints.append(item["endpoint"])
            else:
                logger.warning("WebPush failed for endpoint %s: %s", item["endpoint"][:30], exc)
        except Exception as exc:
            logger.warning("WebPush unexpected error for endpoint %s: %s", item["endpoint"][:30], exc)

    if stale_endpoints:
        try:
            with SessionLocal() as db:
                for ep in stale_endpoints:
                    unsubscribe(db, ep)
        except Exception:
            logger.exception("Failed to prune stale push subscriptions.")


def send_push_notification(
    db: Session,
    title: str,
    body: str,
    url: str = "/",
    image_url: str | None = None,
    severity: str = "critical",
    user_id: int | None = None,
) -> int:
    """Dispatches web push notifications to eligible subscribers (only one user's phones if user_id is set)."""
    if not settings.push_enabled or not settings.vapid_private_key:
        return 0

    try:
        now_dt = datetime.now(ZoneInfo(settings.tz))
        now_hhmm = now_dt.strftime("%H:%M")
    except Exception:
        now_hhmm = datetime.now().strftime("%H:%M")

    target_level = _SEVERITY_LEVELS.get(severity, 2)
    query = select(PushSubscription)
    if user_id is not None:
        query = query.where(PushSubscription.user_id == user_id)
    subs = list(db.scalars(query).all())
    eligible: list[dict] = []

    for sub in subs:
        sub_min = _SEVERITY_LEVELS.get(sub.min_severity, 2)
        if target_level < sub_min:
            continue
        # During quiet hours, only critical alerts are delivered
        if severity != "critical" and is_in_quiet_hours(sub.quiet_start, sub.quiet_end, now_hhmm):
            continue
        eligible.append({
            "endpoint": sub.endpoint,
            "p256dh": sub.p256dh,
            "auth": sub.auth,
        })

    if not eligible:
        return 0

    payload = {
        "title": title,
        "body": body,
        "url": url,
        "image": image_url,
        "severity": severity,
        "tag": f"gate-{severity}",
    }
    payload_str = json.dumps(payload, ensure_ascii=False)
    claims = {"sub": f"mailto:{settings.vapid_claims_email}"}

    thread = threading.Thread(
        target=_send_worker,
        args=(payload_str, eligible, settings.vapid_private_key, claims),
        daemon=True,
    )
    thread.start()
    return len(eligible)
