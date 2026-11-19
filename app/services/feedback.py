import json
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Camera, AuditLog, Incident


VALID_VERDICTS = {"confirmed", "false_alarm", "missed"}


def get_labels_dir(base_dir: Path | None = None) -> Path:
    target = base_dir or (settings.db_path.parent / "labels")
    target.mkdir(parents=True, exist_ok=True)
    return target


def record_feedback(
    db: Session,
    incident: Incident,
    reviewer_id: int,
    verdict: str,
    note: str = "",
    labels_dir: Path | None = None,
) -> Path:
    """Record human review verdict on an incident and export label record."""
    clean_verdict = verdict.strip().lower()
    if clean_verdict not in VALID_VERDICTS:
        raise ValueError(f"Invalid verdict: {verdict}. Expected one of {VALID_VERDICTS}")

    incident.reviewed_at = datetime.now(UTC)
    incident.reviewed_by = reviewer_id
    incident.review_verdict = clean_verdict
    incident.review_note = note.strip()[:1000]

    db.add(
        AuditLog(
            user_id=reviewer_id,
            action="incident.review",
            detail=f"incident_id={incident.id} verdict={clean_verdict}",
        )
    )
    db.commit()
    db.refresh(incident)

    # Export label metadata for future model evaluation and autotuning
    target_dir = get_labels_dir(labels_dir)
    export_path = target_dir / f"incident_{incident.id}.json"

    label_payload = {
        "incident_id": incident.id,
        "camera_id": incident.camera_id,
        "kind": incident.kind,
        "severity": incident.severity,
        "started_at": incident.started_at.isoformat() if incident.started_at else None,
        "ended_at": incident.ended_at.isoformat() if incident.ended_at else None,
        "verdict": clean_verdict,
        "note": incident.review_note,
        "reviewer_id": reviewer_id,
        "ai_verdict": incident.ai_verdict,
        "ai_confidence": incident.ai_confidence,
        "snapshot_path": incident.snapshot_path,
        "clip_path": incident.clip_path,
        "exported_at": datetime.now(UTC).isoformat(),
    }
    export_path.write_text(json.dumps(label_payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return export_path


def get_feedback_summary(db: Session) -> dict:
    """Summarize human review verdicts and compute camera-specific autotune suggestions."""
    incidents = list(db.scalars(select(Incident).where(Incident.review_verdict.is_not(None))))
    total = len(incidents)
    by_verdict = {"confirmed": 0, "false_alarm": 0, "missed": 0}
    by_camera: dict[int, dict[str, int]] = {}

    for inc in incidents:
        v = inc.review_verdict or "confirmed"
        if v in by_verdict:
            by_verdict[v] += 1
        cam_stats = by_camera.setdefault(inc.camera_id, {"total": 0, "false_alarm": 0, "confirmed": 0, "missed": 0})
        cam_stats["total"] += 1
        if v in cam_stats:
            cam_stats[v] += 1

    recommendations: list[dict] = []
    overrides = dict(db.execute(select(Camera.id, Camera.object_confidence)).all())

    for cam_id, stats in by_camera.items():
        current_conf = overrides.get(cam_id) or settings.object_confidence
        total_cam = stats["total"]
        fa_rate = round(stats["false_alarm"] / total_cam, 3) if total_cam else 0.0

        if fa_rate >= 0.30 and total_cam >= 3:
            suggested = round(min(0.85, current_conf + 0.05), 2)
            recommendations.append({
                "camera_id": cam_id,
                "current_confidence": current_conf,
                "suggested_confidence": suggested,
                "false_alarm_rate": fa_rate,
                "total_reviewed": total_cam,
                "reason_bn": f"ক্যামেরা #{cam_id}-এ ভুল অ্যালার্মের হার {int(fa_rate * 100)}%। ডিটেকশন কনফিডেন্স {suggested}-এ বাড়ানো অথবা লোইটার সময় বাড়ানোর প্রস্তাব।",
                "reason_en": f"Camera #{cam_id} false alarm rate is {int(fa_rate * 100)}%. Recommend increasing object confidence to {suggested} or raising loiter duration.",
            })
        elif stats["missed"] >= 2:
            suggested = round(max(0.25, current_conf - 0.05), 2)
            recommendations.append({
                "camera_id": cam_id,
                "current_confidence": current_conf,
                "suggested_confidence": suggested,
                "false_alarm_rate": fa_rate,
                "total_reviewed": total_cam,
                "reason_bn": f"ক্যামেরা #{cam_id}-এ মিস হওয়া ঘটনা রিপোর্ট করা হয়েছে। সেন্সিটিভিটি বাড়াতে কনফিডেন্স {suggested}-এ কমানোর প্রস্তাব।",
                "reason_en": f"Camera #{cam_id} has missed incidents. Recommend increasing sensitivity with confidence {suggested}.",
            })

    return {
        "total_reviewed": total,
        "by_verdict": by_verdict,
        "by_camera": by_camera,
        "recommendations": recommendations,
    }
