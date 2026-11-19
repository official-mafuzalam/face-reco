from datetime import time
from pathlib import Path

import numpy as np

from app.surveillance.clips import ClipRecorder
from app.surveillance.detector import ObjectDetection
from app.surveillance.rules import (
    Rule,
    RuleEvaluator,
    crossing_direction,
    point_in_polygon,
    schedule_active,
)
from app.surveillance.tracker import ObjectTracker
from app.surveillance.worker import IncidentEngine

FRAME = (100, 100, 3)  # rule points are normalised, so a 100x100 frame keeps maths readable
NOON = time(12, 0)
SQUARE = ((0.5, 0.5), (1.0, 0.5), (1.0, 1.0), (0.5, 1.0))


def _rule(kind: str, points=SQUARE, **kw) -> Rule:
    return Rule(kw.pop("id", 1), kw.pop("name", "Store room"), kind, points,
                frozenset(kw.pop("classes", {"person"})), **kw)


def _det(foot_x: float, foot_y: float, category: str = "person") -> ObjectDetection:
    # 10x20 box whose bottom-centre (the "foot") is at (foot_x, foot_y)
    return ObjectDetection((foot_x - 5, foot_y - 20, 10, 20), category, category, 0.9)


def _step(tracker: ObjectTracker, evaluator: RuleEvaluator, rules, dets, now, local=NOON):
    tracker.update(dets, now)
    seen = [t for t in tracker.tracks.values() if t.last_seen == now]
    return evaluator.evaluate(rules, seen, FRAME, now, local, set(tracker.tracks))


def test_geometry_helpers() -> None:
    assert point_in_polygon((0.75, 0.75), SQUARE)
    assert not point_in_polygon((0.25, 0.75), SQUARE)
    a, b = (0.5, 0.0), (0.5, 1.0)  # vertical line, A at top
    assert crossing_direction(a, b, (0.4, 0.5), (0.6, 0.5)) == "ba"
    assert crossing_direction(a, b, (0.6, 0.5), (0.4, 0.5)) == "ab"
    assert crossing_direction(a, b, (0.4, 0.5), (0.45, 0.5)) is None
    # crossing the line's extension, beyond B, does not count
    assert crossing_direction(a, (0.5, 0.3), (0.4, 0.8), (0.6, 0.8)) is None


def test_schedule_including_overnight_window() -> None:
    night = _rule("intrusion", schedule_start="22:00", schedule_end="06:00")
    assert schedule_active(night, time(23, 30)) and schedule_active(night, time(5, 59))
    assert not schedule_active(night, time(12, 0))
    day = _rule("intrusion", schedule_start="09:00", schedule_end="17:00")
    assert schedule_active(day, time(9, 0)) and not schedule_active(day, time(17, 0))
    assert schedule_active(_rule("intrusion"), time(3, 0))  # no schedule = always


def test_intrusion_fires_once_per_track_and_only_for_listed_classes() -> None:
    tracker, evaluator = ObjectTracker(min_hits=1), RuleEvaluator()
    rules = [_rule("intrusion")]
    assert _step(tracker, evaluator, rules, [_det(20, 70)], 0.0) == []
    hits = _step(tracker, evaluator, rules, [_det(70, 70)], 0.5)
    assert [h.rule.kind for h in hits] == ["intrusion"]
    assert _step(tracker, evaluator, rules, [_det(72, 72)], 1.0) == []
    assert _step(tracker, evaluator, rules, [_det(70, 70, "vehicle")], 1.5) == []


def test_intrusion_respects_schedule() -> None:
    tracker, evaluator = ObjectTracker(min_hits=1), RuleEvaluator()
    rules = [_rule("intrusion", schedule_start="22:00", schedule_end="06:00")]
    assert _step(tracker, evaluator, rules, [_det(70, 70)], 0.0, time(12, 0)) == []
    assert len(_step(tracker, evaluator, rules, [_det(70, 70)], 0.5, time(23, 0))) == 1


def test_loiter_needs_continuous_presence() -> None:
    tracker, evaluator = ObjectTracker(min_hits=1, lost_after_s=30), RuleEvaluator()
    rules = [_rule("loiter", loiter_seconds=10)]
    now = 0.0
    hits = []
    while now < 9.5:
        hits += _step(tracker, evaluator, rules, [_det(70, 70)], now)
        now += 0.5
    assert hits == []
    hits = _step(tracker, evaluator, rules, [_det(70, 70)], 10.0)
    assert len(hits) == 1 and hits[0].detail == "10s"
    assert _step(tracker, evaluator, rules, [_det(70, 70)], 12.0) == []


def test_line_crossing_with_direction_filter() -> None:
    line = ((0.5, 0.0), (0.5, 1.0))
    tracker, evaluator = ObjectTracker(min_hits=1), RuleEvaluator()
    only_ab = [_rule("line", line, direction="ab")]
    _step(tracker, evaluator, only_ab, [_det(40, 50)], 0.0)
    assert _step(tracker, evaluator, only_ab, [_det(60, 50)], 0.5) == []  # moving "ba"
    hits = _step(tracker, evaluator, only_ab, [_det(40, 50)], 6.0)
    assert len(hits) == 1 and hits[0].detail == "ab"


class FakeStore:
    def __init__(self) -> None:
        self.rows: dict[int, dict] = {}

    def open(self, **fields) -> int:
        self.rows[len(self.rows) + 1] = dict(fields)
        return len(self.rows)

    def update(self, incident_id: int, **fields) -> None:
        self.rows[incident_id].update(fields)


def test_rule_hit_on_stationary_person_records_warning_with_clip(tmp_path: Path) -> None:
    """A person standing still is not 'activity', but a loiter hit must still be recorded."""
    store = FakeStore()
    recorder = ClipRecorder(fps=4, pre_seconds=1, max_width=100)
    recorder.push(np.zeros(FRAME, np.uint8))
    engine = IncidentEngine(1, store, recorder, media_root=tmp_path, idle_seconds=2, max_seconds=60)
    tracker, evaluator = ObjectTracker(lost_after_s=60), RuleEvaluator()
    rules = [_rule("loiter", loiter_seconds=15, severity="critical")]
    frame = np.zeros(FRAME, np.uint8)
    now = 0.0
    while now <= 16.0:
        hits = _step(tracker, evaluator, rules, [_det(70, 70)], now)
        engine.update(frame, tracker.confirmed(), now, hits)
        now += 0.5
    loiter = [row for row in store.rows.values() if row["kind"] == "loiter"]
    assert len(loiter) == 1
    assert loiter[0]["severity"] == "critical" and loiter[0]["clip_path"]
    # first 10 s counted as activity, then the person became stationary; loiter hit reopened a session
    engine.update(frame, [], 30.0)
    engine.update(frame, [], 40.0)
    assert all("ended_at" in row for row in store.rows.values())


# ---------------- rules web pages ----------------

from contextlib import asynccontextmanager  # noqa: E402
from collections.abc import AsyncIterator, Generator  # noqa: E402
import json  # noqa: E402

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, inspect, text  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.auth import hash_password  # noqa: E402
from app.db import Base, get_db, migrate  # noqa: E402
from app.main import app  # noqa: E402
from app.models import AuditLog, Camera, Person, SurveillanceRule, User  # noqa: E402

PASSWORD = "admin-password-123"


@pytest.fixture
def web(monkeypatch: pytest.MonkeyPatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def override_get_db() -> Generator[Session, None, None]:
        with factory() as db:
            yield db

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield

    monkeypatch.setattr(app.router, "lifespan_context", lifespan)
    app.dependency_overrides[get_db] = override_get_db
    with factory() as db:
        db.add(User(username="admin", password_hash=hash_password(PASSWORD), role="admin"))
        db.add(User(username="guard", password_hash=hash_password(PASSWORD), role="security"))
        db.add(Camera(name="Yard", gate="Back", direction="IN", rtsp_url_encrypted="x"))
        db.commit()
    with TestClient(app) as client:
        yield client, factory
    app.dependency_overrides.clear()
    engine.dispose()


def _login(client: TestClient, username: str) -> None:
    client.post("/login", data={"username": username, "password": PASSWORD})


def test_rules_page_is_admin_only(web) -> None:
    client, _ = web
    _login(client, "guard")
    assert client.get("/cameras/1/rules").status_code == 403


def test_admin_creates_toggles_and_deletes_rule(web) -> None:
    client, factory = web
    _login(client, "admin")
    assert client.get("/cameras/1/rules").status_code == 200
    response = client.post(
        "/cameras/1/rules/new",
        data={"name": "Night yard", "kind": "intrusion", "points": json.dumps([[0, 0], [1, 0], [1, 1]]),
              "classes": ["person", "vehicle"], "schedule_start": "22:00", "schedule_end": "06:00",
              "severity": "critical", "loiter_seconds": "60"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with factory() as db:
        rule = db.query(SurveillanceRule).one()
        assert rule.classes == "person,vehicle" and rule.severity == "critical"
    client.post(f"/cameras/1/rules/{rule.id}/toggle")
    with factory() as db:
        assert db.get(SurveillanceRule, rule.id).enabled is False
    client.post(f"/cameras/1/rules/{rule.id}/delete")
    with factory() as db:
        assert db.query(SurveillanceRule).count() == 0


@pytest.mark.parametrize(
    "overrides",
    [
        {"name": ""},
        {"points": json.dumps([[0, 0], [1, 1]])},  # zone needs 3 points
        {"kind": "line", "points": json.dumps([[0, 0], [1, 0], [1, 1]])},  # line needs exactly 2
        {"classes": []},
        {"schedule_start": "22:00", "schedule_end": ""},
        {"loiter_seconds": "2"},
        {"severity": "panic"},
    ],
)
def test_invalid_rules_are_rejected(web, overrides) -> None:
    client, factory = web
    _login(client, "admin")
    data = {"name": "Zone", "kind": "intrusion", "points": json.dumps([[0, 0], [1, 0], [1, 1]]),
            "classes": ["person"], "severity": "warning", "loiter_seconds": "60"}
    data.update(overrides)
    assert client.post("/cameras/1/rules/new", data=data).status_code == 400
    with factory() as db:
        assert db.query(SurveillanceRule).count() == 0


def test_migrate_adds_camera_columns_to_old_database() -> None:
    engine = create_engine("sqlite://", poolclass=StaticPool)
    with engine.begin() as connection:
        connection.execute(text(
            "CREATE TABLE cameras (id INTEGER PRIMARY KEY, name VARCHAR(200) NOT NULL, gate VARCHAR(200) NOT NULL,"
            " direction VARCHAR(3) NOT NULL, rtsp_url_encrypted TEXT NOT NULL, enabled BOOLEAN NOT NULL)"
        ))
        connection.execute(text("INSERT INTO cameras VALUES (1, 'Old', 'G', 'IN', 'x', 1)"))
    migrate(engine)
    columns = {c["name"] for c in inspect(engine).get_columns("cameras")}
    assert {"face_enabled", "surveillance_enabled"} <= columns
    with engine.connect() as connection:
        assert connection.execute(text("SELECT face_enabled, surveillance_enabled FROM cameras")).one() == (1, 1)
    migrate(engine)  # idempotent


def test_repeated_hits_of_one_rule_are_grouped_per_clip(tmp_path: Path) -> None:
    store = FakeStore()
    recorder = ClipRecorder(fps=4, pre_seconds=1, max_width=100)
    recorder.push(np.zeros(FRAME, np.uint8))
    engine = IncidentEngine(1, store, recorder, media_root=tmp_path, idle_seconds=2, max_seconds=60)
    tracker, evaluator = ObjectTracker(min_hits=1), RuleEvaluator()
    rules = [_rule("intrusion")]
    frame = np.zeros(FRAME, np.uint8)
    # three people walk into the zone one after another
    for i, now in enumerate((0.0, 0.5, 1.0)):
        dets = [_det(60 + 12 * j, 60 + 12 * j) for j in range(i + 1)]
        hits = _step(tracker, evaluator, rules, dets, now)
        engine.update(frame, tracker.confirmed(), now, hits)
    rule_rows = [row for row in store.rows.values() if row["kind"] == "intrusion"]
    assert len(rule_rows) == 1
    assert rule_rows[0]["person_count"] == 3
    assert json.loads(rule_rows[0]["details"])["hits"] == 3


def test_migrate_rebuilds_rules_table_without_kind_check() -> None:
    engine = create_engine("sqlite://", poolclass=StaticPool)
    with engine.begin() as connection:
        connection.execute(text(
            "CREATE TABLE cameras (id INTEGER PRIMARY KEY, name VARCHAR(200) NOT NULL, gate VARCHAR(200) NOT NULL,"
            " direction VARCHAR(3) NOT NULL, rtsp_url_encrypted TEXT NOT NULL, enabled BOOLEAN NOT NULL)"
        ))
        connection.execute(text("INSERT INTO cameras VALUES (1, 'Old', 'G', 'IN', 'x', 1)"))
        connection.execute(text(
            "CREATE TABLE surveillance_rules (id INTEGER PRIMARY KEY, camera_id INTEGER NOT NULL REFERENCES cameras(id),"
            " name VARCHAR(100) NOT NULL, kind VARCHAR(20) NOT NULL, points TEXT NOT NULL, classes VARCHAR(100) NOT NULL,"
            " direction VARCHAR(10) NOT NULL, loiter_seconds INTEGER NOT NULL, schedule_start VARCHAR(5) NOT NULL,"
            " schedule_end VARCHAR(5) NOT NULL, severity VARCHAR(10) NOT NULL, enabled BOOLEAN NOT NULL,"
            " CONSTRAINT ck_rules_kind CHECK (kind IN ('intrusion', 'line', 'loiter')))"
        ))
        connection.execute(text(
            "INSERT INTO surveillance_rules VALUES (1, 1, 'Door', 'intrusion', '[[0,0],[1,0],[1,1]]', 'person',"
            " 'any', 60, '', '', 'warning', 1)"
        ))
    migrate(engine)
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO surveillance_rules (camera_id, name, kind, points, classes, direction, loiter_seconds,"
            " schedule_start, schedule_end, severity, enabled) VALUES (1, 'Bag', 'abandoned', '[]', 'bag', 'any',"
            " 30, '', '', 'warning', 1)"
        ))
        names = [r[0] for r in connection.execute(text("SELECT name FROM surveillance_rules ORDER BY id"))]
    assert names == ["Door", "Bag"]


# ---------------- phase 3/4 rule kinds ----------------

WHOLE = ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0))


def _box(x: float, y: float, w: float, h: float, category: str = "person") -> ObjectDetection:
    return ObjectDetection((x, y, w, h), category, category, 0.9)


def test_running_needs_fast_movement_relative_to_body_height() -> None:
    rules = [_rule("running", WHOLE)]
    # walking: 20 px tall person moving 8 px/s = 0.4 body heights per second
    tracker, evaluator = ObjectTracker(min_hits=1), RuleEvaluator()
    hits = []
    for i in range(6):
        hits += _step(tracker, evaluator, rules, [_box(10 + i * 4, 40, 10, 20)], i * 0.5)
    assert hits == []
    # running: 20 px tall person moving 40 px/s = 2 body heights per second
    tracker, evaluator = ObjectTracker(min_hits=1), RuleEvaluator()
    hits = []
    for i in range(5):
        hits += _step(tracker, evaluator, rules, [_box(5 + i * 10, 40, 10, 20)], i * 0.25)
    assert len(hits) == 1 and float(hits[0].detail) >= 1.5


def test_fall_needs_upright_then_lying_for_a_while() -> None:
    rules = [_rule("fall", WHOLE)]
    tracker, evaluator = ObjectTracker(min_hits=1, iou_threshold=0.05), RuleEvaluator()
    hits = []
    hits += _step(tracker, evaluator, rules, [_box(40, 30, 10, 30)], 0.0)  # standing
    now = 0.5
    while now <= 4.0:  # lying: wide box near the floor
        hits += _step(tracker, evaluator, rules, [_box(35, 50, 30, 12)], now)
        now += 0.5
    assert len(hits) == 1 and hits[0].rule.kind == "fall"
    # someone already lying when first seen (e.g. sleeping on a bench) is not a fall
    tracker, evaluator = ObjectTracker(min_hits=1), RuleEvaluator()
    assert all(_step(tracker, evaluator, rules, [_box(35, 50, 30, 12)], t / 2) == [] for t in range(10))


def test_abandoned_bag_only_when_nobody_is_near() -> None:
    rule = Rule(9, "Lobby", "abandoned", WHOLE, frozenset({"bag"}), loiter_seconds=10)
    bag = _box(70, 70, 8, 8, "bag")
    owner_far = _box(5, 5, 6, 15)
    owner_near = _box(66, 60, 6, 15)
    tracker, evaluator = ObjectTracker(min_hits=1, lost_after_s=30), RuleEvaluator()
    hits = []
    for i in range(30):  # owner stands beside the bag for 15 s: no alert
        hits += _step(tracker, evaluator, [rule], [bag, owner_near], i * 0.5)
    assert hits == []
    for i in range(30, 34):  # owner walks away
        hits += _step(tracker, evaluator, [rule], [bag, owner_far], i * 0.5)
    assert len(hits) == 1 and hits[0].track.category == "bag"


def test_removed_item_detected_after_view_clears() -> None:
    rule = Rule(7, "Shelf", "removed", ((0.2, 0.2), (0.8, 0.2), (0.8, 0.8), (0.2, 0.8)), frozenset(),
                loiter_seconds=3)
    evaluator = RuleEvaluator()
    shelf = np.full((100, 100, 3), 90, np.uint8)
    shelf[30:70, 30:45] = 220  # boxes on the shelf
    shelf[30:70, 55:70] = 30
    empty = np.full((100, 100, 3), 90, np.uint8)
    brighter = np.clip(shelf.astype(int) + 40, 0, 255).astype(np.uint8)

    def run(frame, now, tracks=()):
        return evaluator.evaluate([rule], list(tracks), frame.shape, now, NOON, set(), frame)

    assert run(shelf, 0.0) == []  # baseline
    assert all(run(brighter, t) == [] for t in (1.0, 2.0, 5.0))  # lights on is not a removal
    tracker = ObjectTracker(min_hits=1)
    person = tracker.update([_box(40, 20, 20, 60)], 6.0)
    assert run(empty, 6.0, person) == []  # someone blocking the view: wait
    hits = []
    for t in (7.0, 8.0, 9.0, 10.5):
        hits += run(empty, t)
    assert len(hits) == 1 and hits[0].track is None
    assert run(empty, 15.0) == []  # reported once, new scene accepted


def test_removed_hit_without_track_becomes_incident(tmp_path: Path) -> None:
    from app.surveillance.rules import RuleHit

    store = FakeStore()
    recorder = ClipRecorder(fps=4, pre_seconds=1, max_width=100)
    recorder.push(np.zeros(FRAME, np.uint8))
    engine = IncidentEngine(1, store, recorder, media_root=tmp_path, idle_seconds=2, max_seconds=60)
    rule = Rule(7, "Shelf", "removed", SQUARE, frozenset(), loiter_seconds=3, severity="critical")
    engine.update(np.zeros(FRAME, np.uint8), [], 1.0, [RuleHit(rule, None, "3s")])
    row = store.rows[1]
    assert row["kind"] == "removed" and row["person_count"] == 0 and row["vehicle_count"] == 0
    assert row["snapshot_path"] and row["severity"] == "critical"


def test_admin_can_create_fixed_class_rules(web) -> None:
    client, factory = web
    _login(client, "admin")
    for kind in ("abandoned", "removed", "running", "fall"):
        response = client.post(
            "/cameras/1/rules/new",
            data={"name": kind, "kind": kind, "points": json.dumps([[0, 0], [1, 0], [1, 1]]),
                  "severity": "warning", "loiter_seconds": "30"},
            follow_redirects=False,
        )
        assert response.status_code == 303, kind
    with factory() as db:
        classes = {r.kind: r.classes for r in db.query(SurveillanceRule)}
    assert classes == {"abandoned": "bag", "removed": "", "running": "person", "fall": "person"}


def test_intrusion_fires_when_feet_are_cut_off_by_frame_edge() -> None:
    from app.surveillance.rules import zone_probe_points

    # Zone covers the lower-left of the frame but stops just above the bottom edge.
    zone = ((0.0, 0.3), (0.5, 0.3), (0.5, 0.94), (0.0, 0.94))
    rule = _rule("intrusion", points=zone)
    tracker, evaluator = ObjectTracker(), RuleEvaluator()
    # Person close to the camera: box runs to the bottom of the frame, body inside the zone.
    close = ObjectDetection((20, 40, 20, 60), "person", "person", 0.9)

    assert not point_in_polygon((0.3, 1.0), zone)  # the bare foot point alone would miss
    assert len(zone_probe_points(close.box, 100, 100)) > 1
    hits = _step(tracker, evaluator, [rule], [close], 0.0) + _step(tracker, evaluator, [rule], [close], 0.5)

    assert [hit.rule.kind for hit in hits] == ["intrusion"]


def test_feet_in_view_still_use_only_the_foot_point() -> None:
    from app.surveillance.rules import zone_probe_points

    assert zone_probe_points((20, 10, 20, 60), 100, 100) == ((0.3, 0.7),)
    # Body crosses the zone but the feet stand outside it: no intrusion.
    zone = ((0.0, 0.0), (1.0, 0.0), (1.0, 0.5), (0.0, 0.5))
    tracker, evaluator = ObjectTracker(), RuleEvaluator()
    standing = ObjectDetection((20, 10, 20, 60), "person", "person", 0.9)
    rules = [_rule("intrusion", points=zone)]
    assert _step(tracker, evaluator, rules, [standing], 0.0) + _step(tracker, evaluator, rules, [standing], 0.5) == []


def test_tailgating_detection() -> None:
    line_rule = Rule(1, "Gate Line", "tailgating", ((0.5, 0.0), (0.5, 1.0)), frozenset({"person"}))
    tracker, evaluator = ObjectTracker(min_hits=1), RuleEvaluator()

    # Track 1 crosses from left to right (x=0.4 to x=0.6)
    t1_step1 = [ObjectDetection((35, 40, 10, 20), "person", "person", 0.9)]  # foot at x=40
    t1_step2 = [ObjectDetection((55, 40, 10, 20), "person", "person", 0.9)]  # foot at x=60
    hits1 = _step(tracker, evaluator, [line_rule], t1_step1, 0.0)
    hits2 = _step(tracker, evaluator, [line_rule], t1_step2, 0.5)
    # First person alone: no tailgating
    assert len(hits1) == 0
    assert len(hits2) == 0

    # Track 2 follows immediately while track 1 moves further right
    step3 = [
        ObjectDetection((75, 40, 10, 20), "person", "person", 0.9),  # track 1 at x=80
        ObjectDetection((35, 40, 10, 20), "person", "person", 0.9),  # track 2 at x=40
    ]
    step4 = [
        ObjectDetection((85, 40, 10, 20), "person", "person", 0.9),  # track 1 at x=90
        ObjectDetection((55, 40, 10, 20), "person", "person", 0.9),  # track 2 at x=60 (crosses!)
    ]
    _step(tracker, evaluator, [line_rule], step3, 1.0)
    hits_t2 = _step(tracker, evaluator, [line_rule], step4, 1.5)
    assert any(h.rule.kind == "tailgating" for h in hits_t2)


def test_authorized_zone_and_drawer() -> None:
    auth_rule = _rule("authorized_zone", id=1, extra_params={"allowed": [99], "grace": 3})
    drawer_rule = _rule("drawer", id=2)
    tracker, evaluator = ObjectTracker(min_hits=1), RuleEvaluator()

    tracker.update([_det(75, 75)], 0.0)
    track = list(tracker.tracks.values())[0]  # person inside the zone, face not recognised

    # The drawer rule fires at once; the authorized zone waits for the face to be recognised.
    hits = evaluator.evaluate([auth_rule, drawer_rule], [track], FRAME, 0.0, NOON)
    assert {h.rule.kind for h in hits} == {"drawer"}

    hits = evaluator.evaluate([auth_rule], [track], FRAME, 4.0, NOON)
    assert [(h.rule.kind, h.detail) for h in hits] == [("authorized_zone", "unauthorized_person")]


def test_authorized_zone_allows_listed_person_only() -> None:
    rule = _rule("authorized_zone", id=1, extra_params={"allowed": [99], "grace": 0})
    tracker, evaluator = ObjectTracker(min_hits=1), RuleEvaluator()
    tracker.update([_det(75, 75)], 0.0)
    track = list(tracker.tracks.values())[0]

    track.person_id = 99  # recognised and allowed
    assert evaluator.evaluate([rule], [track], FRAME, 0.0, NOON) == []

    track.person_id = 7  # recognised but not on the list
    hits = evaluator.evaluate([rule], [track], FRAME, 1.0, NOON)
    assert [h.detail for h in hits] == ["not_allowed"]


def test_attach_identities_names_the_person_whose_box_holds_the_face() -> None:
    from app.pipeline.overlay import FaceOverlay
    from app.surveillance.tracker import attach_identities

    tracker = ObjectTracker(min_hits=1)
    tracker.update([_det(30, 60), _det(80, 90)], 0.0)
    left, right = sorted(tracker.tracks.values(), key=lambda t: t.box[0])
    face = FaceOverlay((right.box[0] + 2, right.box[1] + 2, 6, 6), "matched", 5)
    unsure = FaceOverlay((left.box[0] + 2, left.box[1] + 2, 6, 6), "checking")
    attach_identities([left, right], [face, unsure])
    assert (left.person_id, right.person_id) == (None, 5)


def test_count_limit_minimum_alerts_on_an_empty_area() -> None:
    rule = _rule("count_limit", extra_params={"max": 3, "min": 1})
    hits = RuleEvaluator().evaluate([rule], [], FRAME, 0.0, NOON)
    assert [h.detail for h in hits] == ["count=0"]


def test_count_limit_and_tamper() -> None:
    count_rule = _rule("count_limit", extra_params={"max": 1, "min": 0})
    tamper_rule = _rule("tamper")
    evaluator = RuleEvaluator()

    # 2 persons inside zone
    tracker = ObjectTracker(min_hits=1)
    tracker.update([_det(60, 60), _det(80, 80)], 0.0)
    tracks = list(tracker.tracks.values())

    # Count limit triggers (2 > max of 1)
    hits = evaluator.evaluate([count_rule], tracks, FRAME, 0.0, NOON)
    assert any(h.rule.kind == "count_limit" for h in hits)

    # Tamper triggers on dark frame
    dark_frame = np.zeros((100, 100, 3), dtype=np.uint8)
    tamper_hits = evaluator.evaluate([tamper_rule], [], FRAME, 0.0, NOON, frame=dark_frame)
    assert any(h.rule.kind == "tamper" for h in tamper_hits)


def test_apply_site_template_web_route(web) -> None:
    client, factory = web
    _login(client, "admin")
    res = client.post(
        "/cameras/1/rules/template",
        data={"template_key": "factory_gate"},
        follow_redirects=False,
    )
    assert res.status_code == 303
    with factory() as db:
        rules = list(db.query(SurveillanceRule).filter(SurveillanceRule.camera_id == 1))
        kinds = {r.kind for r in rules}
        assert "line" in kinds
        assert "tailgating" in kinds
        assert "after_hours" in kinds
        assert "tamper" in kinds



ZONE_POINTS = json.dumps([[0.1, 0.1], [0.9, 0.1], [0.9, 0.9]])
LINE_POINTS = json.dumps([[0.5, 0.2], [0.5, 0.8]])


def _add_rule(client, **data):
    base = {"name": "Rule", "severity": "warning", "loiter_seconds": "60", "points": ZONE_POINTS}
    base.update(data)
    return client.post("/cameras/1/rules/new", data=base, follow_redirects=False)


def test_authorized_zone_needs_allowed_people(web) -> None:
    client, factory = web
    _login(client, "admin")
    with factory() as db:
        person = Person(code="E1", name="Ali")
        db.add(person)
        db.commit()
        person_id = person.id
    assert _add_rule(client, kind="authorized_zone").status_code == 400
    assert _add_rule(client, kind="authorized_zone", allowed=["9999"]).status_code == 400  # unknown person
    assert _add_rule(client, kind="authorized_zone", allowed=[str(person_id)]).status_code == 303
    with factory() as db:
        rule = db.query(SurveillanceRule).one()
        assert json.loads(rule.extra_params) == {"allowed": [person_id]}


def test_tailgating_is_a_line_with_a_saved_direction(web) -> None:
    client, factory = web
    _login(client, "admin")
    assert _add_rule(client, kind="tailgating").status_code == 400  # an area is not a line
    assert _add_rule(client, kind="tailgating", points=LINE_POINTS, direction="ab").status_code == 303
    with factory() as db:
        assert db.query(SurveillanceRule).one().direction == "ab"


@pytest.mark.parametrize("count_max, count_min, status", [(3, 1, 303), (1, 2, 400), (0, 0, 400), (51, 0, 400)])
def test_count_limit_numbers_are_saved_and_checked(web, count_max, count_min, status) -> None:
    client, factory = web
    _login(client, "admin")
    response = _add_rule(client, kind="count_limit", count_max=str(count_max), count_min=str(count_min))
    assert response.status_code == status
    if status == 303:
        with factory() as db:
            assert json.loads(db.query(SurveillanceRule).one().extra_params) == {"max": 3, "min": 1}


def test_rule_can_be_edited_including_its_zone(web) -> None:
    client, factory = web
    _login(client, "admin")
    _add_rule(client, kind="count_limit", count_max="2", count_min="0")
    with factory() as db:
        rule_id = db.query(SurveillanceRule).one().id
    page = client.get(f"/cameras/1/rules?edit={rule_id}")
    assert page.status_code == 200 and f"/rules/{rule_id}/edit" in page.text
    new_points = json.dumps([[0.2, 0.2], [0.6, 0.2], [0.6, 0.6], [0.2, 0.6]])
    response = client.post(
        f"/cameras/1/rules/{rule_id}/edit", follow_redirects=False,
        data={"name": "Renamed", "kind": "intrusion", "severity": "critical", "loiter_seconds": "60",
              "points": new_points, "count_max": "5", "count_min": "1"},
    )
    assert response.status_code == 303
    with factory() as db:
        rule = db.get(SurveillanceRule, rule_id)
        assert (rule.name, rule.kind, rule.severity) == ("Renamed", "count_limit", "critical")  # type stays fixed
        assert json.loads(rule.points)[2] == [0.6, 0.6]
        assert json.loads(rule.extra_params) == {"max": 5, "min": 1}
        assert db.query(AuditLog).filter(AuditLog.action == "rule.update").count() == 1
    bad = client.post(f"/cameras/1/rules/{rule_id}/edit",
                      data={"name": "", "severity": "warning", "loiter_seconds": "60", "points": new_points})
    assert bad.status_code == 400


def test_finance_template_leaves_authorized_zone_off_until_people_are_chosen(web) -> None:
    client, factory = web
    _login(client, "admin")
    client.post("/cameras/1/rules/template", data={"template_key": "finance_room"})
    with factory() as db:
        rules = {r.kind: r for r in db.query(SurveillanceRule)}
        assert rules["authorized_zone"].enabled is False
        assert rules["count_limit"].enabled and json.loads(rules["count_limit"].extra_params)["max"] == 2
