"""Repeatable accuracy and performance benchmark.

Run after every change and compare with docs/benchmarks/history.csv:

    python -m tools.benchmark --note "what changed"

Measures:
  1. unit tests (pass/fail counts)
  2. object detector speed on this machine
  3. person/vehicle detection counts against hand-labelled frames of a fixed sample video
  4. the full surveillance pipeline on that video: incidents, peak count, false alarms, clip size
  5. face recognition accuracy, only if a consented dataset exists in data/eval (see docs/ACCURACY.md)
"""

import argparse
import csv
import hashlib
import json
import platform
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
LABELS = ROOT / "tools" / "benchmark_data" / "vtest_labels.json"
VIDEO_DIR = ROOT / "data" / "benchmark"
RESULTS_DIR = ROOT / "docs" / "benchmarks" / "results"
HISTORY = ROOT / "docs" / "benchmarks" / "history.csv"
CONFIDENCES = (0.30, 0.45, 0.60)
HISTORY_FIELDS = [
    "date", "commit", "note", "tests_passed", "tests_failed",
    "detector_ms", "person_count_recall", "person_count_precision", "person_count_mae",
    "vehicle_count_recall", "peak_people", "max_people_truth", "incidents",
    "false_alarms", "clip_gb_per_hour", "max_cameras_at_2fps",
    "face_top1", "face_far", "face_frr",
]


def _git_commit() -> str:
    try:
        sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True,
                             text=True, check=True).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, capture_output=True,
                               text=True, check=True).stdout.strip()
        return sha + ("+changes" if dirty else "")
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def run_tests() -> dict[str, int]:
    output = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
        cwd=ROOT, capture_output=True, text=True,
    ).stdout
    summary = output.strip().splitlines()[-1] if output.strip() else ""
    counts = {key: int(n) for n, key in re.findall(r"(\d+) (passed|failed|error|skipped)", summary)}
    return {"passed": counts.get("passed", 0), "failed": counts.get("failed", 0) + counts.get("error", 0),
            "skipped": counts.get("skipped", 0)}


def ensure_video(spec: dict) -> Path:
    path = VIDEO_DIR / spec["name"]
    if not path.exists():
        VIDEO_DIR.mkdir(parents=True, exist_ok=True)
        print(f"Downloading sample video to {path} ...")
        request = urllib.request.Request(spec["url"], headers={"User-Agent": "gate-face-benchmark"})
        with urllib.request.urlopen(request, timeout=120) as response:
            path.write_bytes(response.read())
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != spec["sha256"]:
        raise RuntimeError(f"Sample video hash mismatch ({digest}); delete {path} and re-run.")
    return path


def read_frames(video: Path, indices: list[int]) -> dict[int, np.ndarray]:
    capture = cv2.VideoCapture(str(video))
    frames = {}
    for index in indices:
        capture.set(cv2.CAP_PROP_POS_FRAMES, index)
        ok, frame = capture.read()
        if not ok:
            raise RuntimeError(f"Unable to read frame {index}")
        frames[index] = frame
    capture.release()
    return frames


def detector_speed(detector, frame: np.ndarray) -> float:
    detector.detect(frame)  # warm-up
    times = []
    for _ in range(10):
        started = time.perf_counter()
        detector.detect(frame)
        times.append(time.perf_counter() - started)
    return round(float(np.median(times)) * 1000, 1)


def detection_counts(detector, frames: dict[int, np.ndarray], truth: dict[str, dict]) -> dict[str, dict]:
    """Count-based accuracy: matched = min(detected, true) per frame and category."""
    results = {}
    for confidence in CONFIDENCES:
        totals = {cat: {"true": 0, "detected": 0, "matched": 0, "abs_error": 0} for cat in ("person", "vehicle")}
        per_frame = {}
        for index, frame in frames.items():
            detections = detector.detect(frame, confidence)
            found = {cat: sum(d.category == cat for d in detections) for cat in totals}
            per_frame[index] = found
            for cat, bucket in totals.items():
                expected = truth[str(index)][cat]
                bucket["true"] += expected
                bucket["detected"] += found[cat]
                bucket["matched"] += min(expected, found[cat])
                bucket["abs_error"] += abs(expected - found[cat])
        summary = {}
        for cat, bucket in totals.items():
            summary[cat] = {
                "recall": round(bucket["matched"] / bucket["true"], 3) if bucket["true"] else None,
                "precision": round(bucket["matched"] / bucket["detected"], 3) if bucket["detected"] else None,
                "mae": round(bucket["abs_error"] / len(frames), 2),
                **bucket,
            }
        results[f"{confidence:.2f}"] = {"summary": summary, "per_frame": per_frame}
    return results


class _MemoryStore:
    def __init__(self) -> None:
        self.rows: dict[int, dict] = {}

    def open(self, **fields) -> int:
        self.rows[len(self.rows) + 1] = dict(fields)
        return len(self.rows)

    def update(self, incident_id: int, **fields) -> None:
        self.rows[incident_id].update(fields)


def pipeline_run(detector, video: Path, analysis_fps: float = 2.0, clip_fps: float = 4.0) -> dict:
    from app.surveillance.rules import Rule
    from app.surveillance.worker import SurveillanceWorker

    whole = ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0))
    rules = [
        Rule(1, "zone", "intrusion", ((0.6, 0.35), (1.0, 0.35), (1.0, 0.7), (0.6, 0.7)), frozenset({"person"})),
        Rule(2, "line", "line", ((0.55, 0.3), (0.55, 0.8)), frozenset({"person"})),
        Rule(3, "running", "running", whole, frozenset({"person"})),
        Rule(4, "fall", "fall", whole, frozenset({"person"})),
        Rule(5, "abandoned", "abandoned", whole, frozenset({"bag"}), loiter_seconds=10),
    ]
    store = _MemoryStore()
    with tempfile.TemporaryDirectory(prefix="gate-face-bench-") as temp:
        media = Path(temp) / "data" / "incidents"
        worker = SurveillanceWorker(1, detector, store=store, frame_provider=lambda _id: None,
                                    media_root=media, rule_provider=lambda: rules)
        capture = cv2.VideoCapture(str(video))
        fps = capture.get(cv2.CAP_PROP_FPS) or 10.0
        index = analysed = 0
        analysis_seconds = 0.0
        started = time.perf_counter()
        while True:
            ok, frame = capture.read()
            if not ok:
                break
            now = index / fps
            if index % max(1, round(fps / clip_fps)) == 0:
                worker.recorder.push(frame)
            if index % max(1, round(fps / analysis_fps)) == 0:
                tick = time.perf_counter()
                worker.process(frame, now)
                analysis_seconds += time.perf_counter() - tick
                analysed += 1
            index += 1
        worker.engine.close()
        wall = time.perf_counter() - started
        capture.release()
        clips = list(media.rglob("*.mp4")) + list(media.rglob("*.webm"))
        clip_bytes = sum(path.stat().st_size for path in clips)
        clip_ext = clips[0].suffix if clips else ""

    rows = list(store.rows.values())
    by_kind: dict[str, int] = {}
    for row in rows:
        by_kind[row["kind"]] = by_kind.get(row["kind"], 0) + 1
    video_seconds = index / fps
    # Clips cover the whole video here (people are always present), so size per hour of video ≈ per hour of activity.
    gb_per_hour = clip_bytes / video_seconds * 3600 / 1024**3 if video_seconds else 0.0
    per_frame_ms = analysis_seconds / analysed * 1000 if analysed else 0.0
    return {
        "video_seconds": round(video_seconds, 1),
        "analysed_frames": analysed,
        "wall_seconds": round(wall, 1),
        "analysis_ms_per_frame": round(per_frame_ms, 1),
        "max_cameras_at_2fps": round(1000 / per_frame_ms / analysis_fps, 1) if per_frame_ms else None,
        "incidents": len(rows),
        "incidents_by_kind": by_kind,
        "peak_people": max((row.get("person_count", 0) for row in rows if row["kind"] == "person"), default=0),
        "false_alarms": sum(by_kind.get(kind, 0) for kind in ("running", "fall", "abandoned")),
        "clip_format": clip_ext,
        "clip_gb_per_hour": round(gb_per_hour, 3),
    }


def face_accuracy() -> dict:
    dataset = ROOT / "data" / "eval"
    if not dataset.is_dir() or not any(dataset.iterdir()):
        return {"status": "not measured: no consented dataset in data/eval (see docs/ACCURACY.md)"}
    from tools.eval_accuracy import _load_models, evaluate_samples, load_dataset

    detector, embedder, quality = _load_models()
    result = evaluate_samples(load_dataset(dataset), detector, embedder, quality)
    default = next(row for row in result["threshold_rows"] if row["threshold"] == result["default_threshold"])
    return {
        "status": "measured",
        "people": len([p for p in dataset.iterdir() if p.is_dir() and p.name != "_unknown"]),
        "known_probes": result["known_probe_count"],
        "unknown_probes": result["unknown_probe_count"],
        "top1": result["default_top1_accuracy"],
        "far": default["false_accept_rate"],
        "frr": default["false_reject_rate"],
        "wrong_person": default["wrong_person_rate"],
        "dropped": result["dropped"],
        "best_threshold_far_0_1pct": result["best_fa_threshold"],
    }


def _fmt(value) -> str:
    if value is None:
        return "—"
    if isinstance(value, float) and value <= 1.0:
        return f"{value * 100:.1f}%"
    return str(value)


def check_release_gate(current: dict) -> list[str]:
    """Check whether benchmark metrics meet release criteria against previous benchmark history."""
    if not HISTORY.exists():
        return []
    try:
        with HISTORY.open("r", encoding="utf-8") as f:
            reader = list(csv.DictReader(f))
        if not reader:
            return []
        prev = reader[-1]
    except Exception:
        return []

    failures: list[str] = []
    tests_failed = current.get("tests_failed")
    if tests_failed and str(tests_failed).isdigit() and int(tests_failed) > 0:
        failures.append(f"Unit tests failed: {tests_failed}")

    def _to_float(v):
        try:
            return float(v) if v not in ("", None) else None
        except ValueError:
            return None

    cur_p_rec = _to_float(current.get("person_count_recall"))
    prev_p_rec = _to_float(prev.get("person_count_recall"))
    if cur_p_rec is not None and prev_p_rec is not None and cur_p_rec < (prev_p_rec - 0.05):
        failures.append(f"Person recall degraded: previous {prev_p_rec:.3f}, current {cur_p_rec:.3f}")

    cur_p_prec = _to_float(current.get("person_count_precision"))
    prev_p_prec = _to_float(prev.get("person_count_precision"))
    if cur_p_prec is not None and prev_p_prec is not None and cur_p_prec < (prev_p_prec - 0.05):
        failures.append(f"Person precision degraded: previous {prev_p_prec:.3f}, current {cur_p_prec:.3f}")

    cur_fa = _to_float(current.get("false_alarms"))
    prev_fa = _to_float(prev.get("false_alarms"))
    if cur_fa is not None and prev_fa is not None and cur_fa > (prev_fa + 5):
        failures.append(f"False alarms increased significantly: previous {prev_fa}, current {cur_fa}")

    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--note", default="", help="What changed since the last run (stored in history).")
    parser.add_argument("--skip-tests", action="store_true")
    parser.add_argument("--no-save", action="store_true", help="Print results without writing history.")
    parser.add_argument("--gate", action="store_true", help="Enforce release gate checks against previous benchmark.")
    args = parser.parse_args(argv)

    sys.path.insert(0, str(ROOT))
    from app.config import settings
    from app.surveillance.detector import ObjectDetector
    from app.surveillance.worker import MODEL_PATH

    labels = json.loads(LABELS.read_text(encoding="utf-8"))
    result: dict = {
        "date": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
        "commit": _git_commit(),
        "note": args.note,
        "machine": {"cpu": platform.processor(), "python": platform.python_version(), "opencv": cv2.__version__},
        "settings": {"object_confidence": settings.object_confidence, "match_threshold": settings.match_threshold},
    }
    if not args.skip_tests:
        print("Running unit tests ...")
        result["tests"] = run_tests()
    video = ensure_video(labels["video"])
    detector = ObjectDetector(MODEL_PATH)
    frames = read_frames(video, sorted(int(i) for i in labels["frames"]))
    print("Measuring detector ...")
    result["detector_ms"] = detector_speed(detector, next(iter(frames.values())))
    result["detection"] = detection_counts(detector, frames, labels["frames"])
    print("Running surveillance pipeline on the sample video ...")
    result["pipeline"] = pipeline_run(detector, video)
    result["pipeline"]["max_people_truth"] = labels["max_people_at_once"]
    print("Face recognition ...")
    result["face"] = face_accuracy()

    current = result["detection"][f"{settings.object_confidence:.2f}"]["summary"] \
        if f"{settings.object_confidence:.2f}" in result["detection"] else result["detection"]["0.45"]["summary"]
    pipe, face, tests = result["pipeline"], result["face"], result.get("tests", {})
    row = {
        "date": result["date"], "commit": result["commit"], "note": args.note,
        "tests_passed": tests.get("passed", ""), "tests_failed": tests.get("failed", ""),
        "detector_ms": result["detector_ms"],
        "person_count_recall": current["person"]["recall"],
        "person_count_precision": current["person"]["precision"],
        "person_count_mae": current["person"]["mae"],
        "vehicle_count_recall": current["vehicle"]["recall"],
        "peak_people": pipe["peak_people"], "max_people_truth": pipe["max_people_truth"],
        "incidents": pipe["incidents"], "false_alarms": pipe["false_alarms"],
        "clip_gb_per_hour": pipe["clip_gb_per_hour"], "max_cameras_at_2fps": pipe["max_cameras_at_2fps"],
        "face_top1": face.get("top1", ""), "face_far": face.get("far", ""), "face_frr": face.get("frr", ""),
    }

    print("\n=== Benchmark summary ===")
    print(f"Commit {row['commit']}  {row['date']}  note: {args.note or '-'}")
    if tests:
        print(f"Tests: {tests['passed']} passed, {tests['failed']} failed")
    print(f"Detector: {row['detector_ms']} ms/frame -> about {row['max_cameras_at_2fps']} cameras at 2 fps")
    for confidence, data in result["detection"].items():
        person = data["summary"]["person"]
        print(f"  conf {confidence}: person recall {_fmt(person['recall'])}, precision {_fmt(person['precision'])}, "
              f"MAE {person['mae']}  | vehicle recall {_fmt(data['summary']['vehicle']['recall'])}")
    print(f"Pipeline: {pipe['incidents']} incidents {pipe['incidents_by_kind']}, peak people {pipe['peak_people']}"
          f"/{pipe['max_people_truth']}, false alarms {pipe['false_alarms']}, clips {pipe['clip_gb_per_hour']} GB/h")
    if face["status"] == "measured":
        print(f"Face: top-1 {_fmt(face['top1'])}, FAR {_fmt(face['far'])}, FRR {_fmt(face['frr'])}")
    else:
        print(f"Face: {face['status']}")

    if not args.no_save:
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        out = RESULTS_DIR / f"{stamp}_{row['commit'].replace('+', '-')}.json"
        out.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
        new_file = not HISTORY.exists()
        with HISTORY.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=HISTORY_FIELDS)
            if new_file:
                writer.writeheader()
            writer.writerow(row)
        print(f"\nSaved {out.relative_to(ROOT)} and appended {HISTORY.relative_to(ROOT)}")
    gate_failures = check_release_gate(row) if args.gate else []
    if gate_failures:
        print("\n❌ RELEASE GATE VIOLATIONS:")
        for fail in gate_failures:
            print(f"  - {fail}")
        return 1

    return 1 if tests.get("failed") else 0


if __name__ == "__main__":
    raise SystemExit(main())
