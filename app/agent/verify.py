import base64
import json
import logging
import queue
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import cv2
import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import SessionLocal
from app.models import Incident
from app.notify.bus import safe_publish
from app.notify.push import send_push_notification

logger = logging.getLogger(__name__)

_verify_queue: queue.Queue[int] = queue.Queue(maxsize=100)
_stop_event = threading.Event()
_worker_thread: threading.Thread | None = None


def check_ollama_status() -> dict[str, Any]:
    """Probes the Ollama HTTP API and returns connectivity and available models."""
    url = f"{settings.ollama_url.rstrip('/')}/api/tags"
    try:
        with httpx.Client(timeout=2.0) as client:
            resp = client.get(url)
            if resp.status_code == 200:
                data = resp.json()
                models = [m.get("name") for m in data.get("models", [])]
                has_target = any(settings.ollama_model in m for m in models)
                return {
                    "status": "online",
                    "models": models,
                    "target_model": settings.ollama_model,
                    "target_model_installed": has_target,
                }
    except Exception as exc:
        return {
            "status": "offline",
            "error": str(exc),
            "target_model": settings.ollama_model,
            "target_model_installed": False,
        }
    return {
        "status": "offline",
        "target_model": settings.ollama_model,
        "target_model_installed": False,
    }


def extract_incident_frames(snapshot_path: str | None, clip_path: str | None, max_frames: int = 3) -> list[str]:
    """Returns base64-encoded JPEG strings of 1-3 key frames."""
    frames_b64: list[str] = []

    # 1. First frame from snapshot if exists
    if snapshot_path and Path(snapshot_path).is_file():
        try:
            raw = Path(snapshot_path).read_bytes()
            frames_b64.append(base64.b64encode(raw).decode())
        except Exception:
            pass

    # 2. Extract 1-2 additional frames from clip
    if clip_path and Path(clip_path).is_file() and len(frames_b64) < max_frames:
        try:
            cap = cv2.VideoCapture(str(clip_path))
            total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            if total > 0:
                indices = [total // 3, (2 * total) // 3] if total >= 6 else [total // 2]
                for idx in indices:
                    cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
                    ret, frame = cap.read()
                    if ret and frame is not None:
                        # Resize if too large for VLM efficiency
                        h, w = frame.shape[:2]
                        if w > 640:
                            frame = cv2.resize(frame, (640, int(h * (640 / w))))
                        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 75])
                        if ok:
                            frames_b64.append(base64.b64encode(buf.tobytes()).decode())
                            if len(frames_b64) >= max_frames:
                                break
            cap.release()
        except Exception:
            pass

    return frames_b64


def query_ollama_vlm(
    images_b64: list[str],
    kind: str,
    details: str,
    timeout: float = 15.0,
) -> dict[str, Any] | None:
    """Queries Ollama chat endpoint with images and returns parsed JSON verdict."""
    if not images_b64:
        return None

    prompt = (
        f"You are an AI CCTV verification agent. An event of kind '{kind}' was detected "
        f"with details: '{details}'. Examine the camera frame(s) carefully. "
        "Determine if this is a genuine security event or a false alarm (e.g. shadow, reflection, pets, authorized worker). "
        "Reply ONLY with a JSON object in this format:\n"
        '{"verdict": "confirmed" | "false_alarm", "confidence": 0.0-1.0, '
        '"summary_bn": "স্বল্প ব্যাখ্যা বাংলায়", "summary_en": "short explanation in English"}'
    )

    payload = {
        "model": settings.ollama_model,
        "messages": [
            {
                "role": "user",
                "content": prompt,
                "images": images_b64,
            }
        ],
        "format": "json",
        "stream": False,
    }

    url = f"{settings.ollama_url.rstrip('/')}/api/chat"
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(url, json=payload)
            if resp.status_code == 200:
                data = resp.json()
                content = data.get("message", {}).get("content", "{}")
                return json.loads(content)
    except Exception as exc:
        logger.debug("Ollama VLM query failed or timed out: %s", exc)
        return None
    return None


def verify_incident(incident_id: int) -> None:
    """Processes a single incident, queries VLM if available, and updates DB."""
    with SessionLocal() as db:
        incident = db.get(Incident, incident_id)
        if incident is None or incident.ai_verdict is not None:
            return

        images_b64 = extract_incident_frames(incident.snapshot_path, incident.clip_path)
        result = None
        if images_b64:
            result = query_ollama_vlm(
                images_b64,
                incident.kind,
                incident.details,
                timeout=settings.ai_verify_timeout_seconds,
            )

        now = datetime.now(UTC)
        if result and isinstance(result, dict) and "verdict" in result:
            verdict = result.get("verdict", "inconclusive")
            if verdict not in ("confirmed", "false_alarm"):
                verdict = "inconclusive"
            confidence = float(result.get("confidence", 0.8))
            summary_bn = str(result.get("summary_bn", ""))
            summary_en = str(result.get("summary_en", ""))
        else:
            verdict = "inconclusive"
            confidence = 0.5
            summary_bn = "AI যাচাইয়ের তথ্য পাওয়া যায়নি"
            summary_en = "AI verification inconclusive"

        incident.ai_verdict = verdict
        incident.ai_confidence = confidence
        incident.ai_summary_bn = summary_bn
        incident.ai_summary_en = summary_en
        incident.ai_checked_at = now
        db.commit()

        # Real-time event for UI
        safe_publish(
            "incident_verified",
            "info",
            "incident_verified_title",
            "incident_verified_body",
            incident_id=incident.id,
            verdict=verdict,
            summary_bn=summary_bn,
            link=f"/incidents/{incident.id}",
        )

        # Warning pushes are held for this verdict. Only a verified false alarm stays quiet:
        # an unreachable or unsure AI must never swallow a real alert.
        if incident.severity == "warning" and verdict in ("confirmed", "inconclusive"):
            title = "AI নিশ্চিত অ্যালার্ট" if verdict == "confirmed" else "অ্যালার্ট (AI যাচাই হয়নি)"
            send_push_notification(
                db=db,
                title=title,
                body=summary_bn if verdict == "confirmed" and summary_bn else f"{incident.camera.name} ক্যামেরায় ঘটনা শনাক্ত হয়েছে।",
                url=f"/incidents/{incident.id}",
                image_url=f"/incidents/{incident.id}/snapshot.jpg" if incident.snapshot_path else None,
                severity="warning",
            )


def _worker_loop() -> None:
    while not _stop_event.is_set():
        try:
            incident_id = _verify_queue.get(timeout=1.0)
        except queue.Empty:
            continue
        try:
            verify_incident(incident_id)
        except Exception:
            logger.exception("Error verifying incident %s", incident_id)
        finally:
            _verify_queue.task_done()


def _push_unverified_warning(incident_id: int) -> None:
    """Sends the held warning push for an incident that could not be queued for the AI."""
    with SessionLocal() as db:
        incident = db.get(Incident, incident_id)
        if incident is not None and incident.severity == "warning":
            send_push_notification(
                db=db,
                title="অ্যালার্ট (AI যাচাই হয়নি)",
                body=f"{incident.camera.name} ক্যামেরায় ঘটনা শনাক্ত হয়েছে।",
                url=f"/incidents/{incident.id}",
                image_url=f"/incidents/{incident.id}/snapshot.jpg" if incident.snapshot_path else None,
                severity="warning",
            )


def queue_incident_for_verification(incident_id: int) -> None:
    """Enqueues an incident ID to be processed asynchronously by the AI verify worker."""
    if not settings.ai_verify_enabled:
        return
    try:
        _verify_queue.put_nowait(incident_id)
    except queue.Full:
        logger.warning("AI verification queue is full; skipping incident %s", incident_id)
        try:
            _push_unverified_warning(incident_id)
        except Exception:
            logger.exception("Could not push incident %s after the queue filled up.", incident_id)


def start_ai_verifier() -> tuple[threading.Event, threading.Thread]:
    global _stop_event, _worker_thread
    _stop_event = threading.Event()
    _worker_thread = threading.Thread(
        target=_worker_loop,
        name="gate-ai-verifier",
        daemon=True,
    )
    _worker_thread.start()
    return _stop_event, _worker_thread


def stop_ai_verifier(stop_event: threading.Event, thread: threading.Thread) -> None:
    stop_event.set()
    thread.join(timeout=5)


def is_ai_verifier_alive() -> bool:
    return _worker_thread is not None and _worker_thread.is_alive()


def restart_ai_verifier() -> tuple[threading.Event, threading.Thread] | None:
    global _stop_event, _worker_thread
    if not settings.ai_verify_enabled:
        return None
    if _worker_thread is not None and _worker_thread.is_alive():
        return _stop_event, _worker_thread
    return start_ai_verifier()
