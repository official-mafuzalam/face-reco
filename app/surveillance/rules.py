"""Zone / line / loitering rules evaluated against tracked objects."""

import json
import logging
import threading
import time as monotonic_time
from dataclasses import dataclass
from datetime import datetime, time
from zoneinfo import ZoneInfo

import cv2
import numpy as np

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.db import SessionLocal
from app.models import SurveillanceRule
from app.surveillance.tracker import ObjectTrack


logger = logging.getLogger(__name__)
Point = tuple[float, float]
LINE_REFIRE_SECONDS = 5.0


@dataclass(frozen=True)
class Rule:
    id: int
    name: str
    kind: str  # intrusion | line | loiter | abandoned | removed | running | fall
    points: tuple[Point, ...]
    classes: frozenset[str]
    direction: str = "any"  # line only: any | ab | ba
    loiter_seconds: int = 60
    schedule_start: str = ""
    schedule_end: str = ""
    severity: str = "warning"

    @classmethod
    def from_model(cls, model: SurveillanceRule) -> "Rule":
        points = tuple((float(x), float(y)) for x, y in json.loads(model.points))
        classes = frozenset(c.strip() for c in model.classes.split(",") if c.strip())
        return cls(
            model.id, model.name, model.kind, points, classes, model.direction,
            model.loiter_seconds, model.schedule_start, model.schedule_end, model.severity,
        )


@dataclass(frozen=True)
class RuleHit:
    rule: Rule
    track: ObjectTrack | None  # None for scene rules (removed object)
    detail: str = ""


def parse_hhmm(value: str) -> time | None:
    try:
        hours, minutes = value.split(":")
        return time(int(hours), int(minutes))
    except (ValueError, AttributeError):
        return None


def schedule_active(rule: Rule, local_now: time) -> bool:
    start, end = parse_hhmm(rule.schedule_start), parse_hhmm(rule.schedule_end)
    if start is None or end is None or start == end:
        return True
    if start < end:
        return start <= local_now < end
    return local_now >= start or local_now < end  # crosses midnight, e.g. 22:00-06:00


def point_in_polygon(point: Point, polygon: tuple[Point, ...]) -> bool:
    x, y = point
    inside = False
    count = len(polygon)
    for index in range(count):
        x1, y1 = polygon[index]
        x2, y2 = polygon[(index + 1) % count]
        if (y1 > y) != (y2 > y):
            crossing = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x < crossing:
                inside = not inside
    return inside


# A box whose bottom edge is this close to the frame edge has its feet cut off.
CUT_OFF_BOTTOM = 0.95
CUT_OFF_STRIP = 0.2


def zone_probe_points(
    box: tuple[float, float, float, float], width: int, height: int
) -> tuple[Point, ...]:
    """Normalised points that decide whether an object stands inside a zone.

    Normally the foot point (bottom centre). When the box reaches the bottom of the frame
    the feet are out of view and the "foot" is just the frame edge, so a few points across
    the lowest part of the visible body are used instead.
    """
    x, y, w, h = box
    bottom = y + h
    foot = ((x + w / 2) / width, bottom / height)
    if bottom < height * CUT_OFF_BOTTOM:
        return (foot,)
    rows = (bottom - h * CUT_OFF_STRIP, bottom - h * CUT_OFF_STRIP / 2, bottom - 1)
    columns = (x + w * 0.25, x + w * 0.5, x + w * 0.75)
    return (foot,) + tuple((cx / width, cy / height) for cy in rows for cx in columns)


def _side(a: Point, b: Point, p: Point) -> float:
    return (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0])


def crossing_direction(a: Point, b: Point, previous: Point, current: Point) -> str | None:
    """'ab' when moving from the left of A->B to the right, 'ba' for the reverse, None if no crossing."""
    s1, s2 = _side(a, b, previous), _side(a, b, current)
    if s1 == 0 or s2 == 0 or (s1 > 0) == (s2 > 0):
        return None
    # The movement must also cross within the segment, not its extension.
    t1, t2 = _side(previous, current, a), _side(previous, current, b)
    if (t1 > 0) == (t2 > 0):
        return None
    return "ab" if s1 < 0 < s2 else "ba"


ZONE_KINDS = ("intrusion", "loiter", "abandoned", "running", "fall")
RUN_SPEED_BODY_HEIGHTS = 1.5  # walking is ~0.8 body heights per second, running ~2
RUN_REFIRE_SECONDS = 10.0
FALL_SECONDS = 3.0
SCENE_DIFF_CHANGED = 0.15  # share of the area's pixels that changed
SCENE_DIFF_STABLE = 0.05


class RuleEvaluator:
    def __init__(self) -> None:
        self._inside_since: dict[tuple[int, int], float] = {}
        self._fired: set[tuple[int, int]] = set()
        self._fired_at: dict[tuple[int, int], float] = {}
        self._last_foot: dict[int, Point] = {}
        self._upright: set[tuple[int, int]] = set()
        self._fallen_since: dict[tuple[int, int], float] = {}
        # "removed" rules: per-rule scene baseline
        self._baseline: dict[int, np.ndarray] = {}
        self._changed_since: dict[int, float] = {}
        self._masks: dict[tuple[int, tuple[int, int]], np.ndarray] = {}

    def evaluate(
        self,
        rules: list[Rule],
        tracks: list[ObjectTrack],
        frame_shape: tuple[int, ...],
        now: float,
        local_now: time,
        alive_ids: set[int] | None = None,
        frame: np.ndarray | None = None,
    ) -> list[RuleHit]:
        """`tracks`: objects seen this frame. `alive_ids`: every id the tracker still holds."""
        height, width = frame_shape[:2]
        hits: list[RuleHit] = []
        live_ids = alive_ids if alive_ids is not None else {track.track_id for track in tracks}
        active_rules = [rule for rule in rules if schedule_active(rule, local_now)]
        people = [track for track in tracks if track.category == "person"]
        for track in tracks:
            fx, fy = track.foot
            foot = (fx / width, fy / height)
            probes = zone_probe_points(track.box, width, height)
            previous = self._last_foot.get(track.track_id)
            for rule in active_rules:
                if track.category not in rule.classes:
                    continue
                hit = self._track_rule(rule, track, foot, previous, people, now, probes)
                if hit is not None:
                    hits.append(hit)
            self._last_foot[track.track_id] = foot
        if frame is not None:
            for rule in active_rules:
                if rule.kind == "removed" and self._scene_changed(rule, frame, tracks, now):
                    hits.append(RuleHit(rule, None, f"{rule.loiter_seconds}s"))
        self._forget(live_ids)
        return hits

    def _track_rule(
        self,
        rule: Rule,
        track: ObjectTrack,
        foot: Point,
        previous: Point | None,
        people: list[ObjectTrack],
        now: float,
        probes: tuple[Point, ...] | None = None,
    ) -> RuleHit | None:
        key = (rule.id, track.track_id)
        if rule.kind == "line":
            if len(rule.points) < 2 or previous is None:
                return None
            direction = crossing_direction(rule.points[0], rule.points[1], previous, foot)
            if direction and rule.direction in ("any", direction) and self._cooled(key, now, LINE_REFIRE_SECONDS):
                return RuleHit(rule, track, direction)
            return None
        if rule.kind not in ZONE_KINDS or len(rule.points) < 3:
            return None
        if not any(point_in_polygon(point, rule.points) for point in (probes or (foot,))):
            self._inside_since.pop(key, None)
            self._fallen_since.pop(key, None)
            return None
        since = self._inside_since.setdefault(key, now)

        if rule.kind == "running":
            speed = _body_heights_per_second(track, now)
            if speed is not None and speed >= RUN_SPEED_BODY_HEIGHTS and self._cooled(key, now, RUN_REFIRE_SECONDS):
                return RuleHit(rule, track, f"{speed:.1f}")
            return None
        if rule.kind == "fall":
            _x, _y, w, h = track.box
            ratio = h / max(w, 1.0)
            if ratio > 1.3:
                self._upright.add(key)
                self._fallen_since.pop(key, None)
                self._fired.discard(key)  # got back up: a later fall counts again
            elif ratio < 0.9 and key in self._upright:
                fallen = self._fallen_since.setdefault(key, now)
                if now - fallen >= FALL_SECONDS and key not in self._fired:
                    self._fired.add(key)
                    return RuleHit(rule, track, f"{int(now - fallen)}s")
            return None

        if key in self._fired:
            return None
        if rule.kind == "intrusion":
            self._fired.add(key)
            return RuleHit(rule, track, "")
        if rule.kind == "loiter" and now - since >= rule.loiter_seconds:
            self._fired.add(key)
            return RuleHit(rule, track, f"{int(now - since)}s")
        if rule.kind == "abandoned" and track.is_stationary(now, window_s=rule.loiter_seconds):
            if not any(_near(person, track) for person in people):
                self._fired.add(key)
                return RuleHit(rule, track, f"{int(now - track.first_seen)}s")
        return None

    def _cooled(self, key: tuple[int, int], now: float, seconds: float) -> bool:
        if now - self._fired_at.get(key, -1e9) < seconds:
            return False
        self._fired_at[key] = now
        return True

    def _scene_changed(self, rule: Rule, frame: np.ndarray, tracks: list[ObjectTrack], now: float) -> bool:
        """Detect a lasting change inside the area (something taken or left) while nobody blocks the view."""
        height, width = frame.shape[:2]
        xs = [x for x, _ in rule.points]
        ys = [y for _, y in rule.points]
        area = (min(xs) * width, min(ys) * height, max(xs) * width, max(ys) * height)
        for track in tracks:
            if track.category in ("person", "vehicle") and _overlaps(track.box, area):
                self._changed_since.pop(rule.id, None)
                return False  # someone is in the way; judge only once the view is clear
        scale = min(1.0, 320 / width)
        small = cv2.resize(frame, (max(1, int(width * scale)), max(1, int(height * scale))), interpolation=cv2.INTER_AREA)
        gray = cv2.GaussianBlur(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY), (5, 5), 0).astype(np.float32)
        mask_key = (rule.id, gray.shape)
        mask = self._masks.get(mask_key)
        if mask is None:
            canvas = np.zeros(gray.shape, np.uint8)
            polygon = np.array([(x * gray.shape[1], y * gray.shape[0]) for x, y in rule.points], np.int32)
            cv2.fillPoly(canvas, [polygon], 1)
            mask = self._masks[mask_key] = canvas.astype(bool)
        if not mask.any():
            return False
        values = gray[mask]
        values = values - values.mean()  # ignore overall brightness changes (lights on/off)
        baseline = self._baseline.get(rule.id)
        if baseline is None or baseline.shape != values.shape:
            self._baseline[rule.id] = values
            return False
        changed = float(np.mean(np.abs(values - baseline) > 25))
        if changed < SCENE_DIFF_STABLE:
            self._baseline[rule.id] = 0.95 * baseline + 0.05 * values  # follow slow lighting drift
            self._changed_since.pop(rule.id, None)
            return False
        if changed < SCENE_DIFF_CHANGED:
            return False
        since = self._changed_since.setdefault(rule.id, now)
        if now - since < rule.loiter_seconds:
            return False
        self._baseline[rule.id] = values  # accept the new scene so it is reported once
        self._changed_since.pop(rule.id, None)
        return True

    def _forget(self, live_ids: set[int]) -> None:
        """Drop state for tracks the tracker has deleted (briefly missed tracks keep theirs)."""
        self._last_foot = {tid: p for tid, p in self._last_foot.items() if tid in live_ids}
        for store in (self._inside_since, self._fired_at, self._fallen_since):
            for key in [k for k in store if k[1] not in live_ids]:
                del store[key]
        self._fired = {key for key in self._fired if key[1] in live_ids}
        self._upright = {key for key in self._upright if key[1] in live_ids}


def _body_heights_per_second(track: ObjectTrack, now: float, window_s: float = 2.0) -> float | None:
    samples = [(t, x, y) for t, x, y in track.path if now - t <= window_s]
    if len(samples) < 3:
        return None
    (t0, x0, y0), (t1, x1, y1) = samples[0], samples[-1]
    if t1 - t0 < 1.0:
        return None
    return ((x1 - x0) ** 2 + (y1 - y0) ** 2) ** 0.5 / (t1 - t0) / max(track.box[3], 1.0)


def _near(person: ObjectTrack, item: ObjectTrack) -> bool:
    px, py = person.foot
    ix, iy = item.center
    reach = 2.5 * max(item.box[2], item.box[3], person.box[2])
    return ((px - ix) ** 2 + (py - iy) ** 2) ** 0.5 <= reach


def _overlaps(box: tuple[float, float, float, float], area: tuple[float, float, float, float]) -> bool:
    x, y, w, h = box
    return x < area[2] and x + w > area[0] and y < area[3] and y + h > area[1]


class RuleProvider:
    """Loads a camera's enabled rules and refreshes them every few seconds, so edits apply live."""

    def __init__(
        self,
        camera_id: int,
        session_factory: sessionmaker[Session] = SessionLocal,
        refresh_seconds: float = 5.0,
    ) -> None:
        self.camera_id = camera_id
        self._session_factory = session_factory
        self._refresh_seconds = refresh_seconds
        self._rules: list[Rule] = []
        self._loaded_at = -1e9
        self._lock = threading.Lock()

    def __call__(self) -> list[Rule]:
        now = monotonic_time.monotonic()
        with self._lock:
            if now - self._loaded_at >= self._refresh_seconds:
                self._loaded_at = now
                try:
                    with self._session_factory() as db:
                        models = db.scalars(
                            select(SurveillanceRule).where(
                                SurveillanceRule.camera_id == self.camera_id,
                                SurveillanceRule.enabled.is_(True),
                            )
                        ).all()
                    self._rules = [Rule.from_model(model) for model in models]
                except Exception:
                    logger.exception("Unable to load rules for camera %s.", self.camera_id)
            return self._rules


def local_time_now() -> time:
    try:
        zone = ZoneInfo(settings.tz)
    except Exception:
        zone = None
    return datetime.now(zone).time()
