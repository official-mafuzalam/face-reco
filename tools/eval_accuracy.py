import argparse
import csv
import html
import json
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import cv2
import numpy as np

from app.vision.detector import FaceDetector
from app.vision.embedder import FaceEmbedder
from app.vision.matcher import Gallery
from app.vision.quality import FaceQuality


DEFAULT_THRESHOLD = 0.363
THRESHOLDS = tuple(round(value / 100, 2) for value in range(20, 61))
METRIC_KEYS = ("true_accept_rate", "false_reject_rate", "wrong_person_rate", "false_accept_rate")


class Detector(Protocol):
    def detect(self, bgr: np.ndarray) -> list:
        ...


class Embedder(Protocol):
    def align_and_embed(self, bgr: np.ndarray, face: object) -> np.ndarray:
        ...


class Quality(Protocol):
    def check(self, bgr: np.ndarray, face: object) -> tuple[bool, list[str]]:
        ...


@dataclass(frozen=True)
class ImageSample:
    person_code: str
    path: str
    image: np.ndarray


def _process(
    sample: ImageSample,
    detector: Detector,
    embedder: Embedder,
    quality: Quality,
) -> tuple[np.ndarray | None, tuple[str, ...]]:
    try:
        faces = detector.detect(sample.image)
    except (ValueError, cv2.error):
        return None, ("invalid_image",)
    if not faces:
        return None, ("no_face",)
    if len(faces) != 1:
        return None, ("multiple_faces",)
    face = faces[0]
    ok, reasons = quality.check(sample.image, face)
    if not ok:
        return None, tuple(reasons) or ("quality_rejected",)
    try:
        vector = np.asarray(embedder.align_and_embed(sample.image, face), dtype=np.float32).reshape(-1)
    except (ValueError, cv2.error):
        return None, ("embedding_failed",)
    if vector.shape != (128,) or not np.isfinite(vector).all():
        return None, ("embedding_invalid",)
    norm = float(np.linalg.norm(vector))
    if norm == 0:
        return None, ("embedding_invalid",)
    return (vector / norm).astype(np.float32), ()


def _rate(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def evaluate_samples(
    samples_by_person: dict[str, list[ImageSample]],
    detector: Detector,
    embedder: Embedder,
    quality: Quality,
    *,
    k: int = 3,
    margin: float = 0.05,
    default_threshold: float = DEFAULT_THRESHOLD,
    thresholds: tuple[float, ...] = THRESHOLDS,
) -> dict[str, object]:
    if k < 1:
        raise ValueError("k must be at least one.")
    if not thresholds or any(value < 0 or value > 1 for value in thresholds):
        raise ValueError("thresholds must be non-empty values between 0 and 1.")

    gallery_ids: list[str] = []
    gallery_vectors: list[np.ndarray] = []
    known_probes: list[tuple[str, np.ndarray]] = []
    unknown_probes: list[np.ndarray] = []
    dropped: Counter[str] = Counter()
    usable: Counter[str] = Counter()

    for code, samples in sorted(samples_by_person.items()):
        ordered = sorted(samples, key=lambda sample: sample.path)
        gallery_samples = [] if code == "_unknown" else ordered[:k]
        probe_samples = ordered if code == "_unknown" else ordered[k:]
        for sample in gallery_samples:
            vector, reasons = _process(sample, detector, embedder, quality)
            if reasons:
                dropped.update(reasons)
            elif vector is not None:
                gallery_ids.append(code)
                gallery_vectors.append(vector)
                usable["gallery"] += 1
        for sample in probe_samples:
            vector, reasons = _process(sample, detector, embedder, quality)
            if reasons:
                dropped.update(reasons)
            elif vector is not None:
                if code == "_unknown":
                    unknown_probes.append(vector)
                else:
                    known_probes.append((code, vector))
                usable["probe"] += 1

    vectors = (
        np.stack(gallery_vectors).astype(np.float32)
        if gallery_vectors
        else np.empty((0, 128), dtype=np.float32)
    )
    threshold_rows: list[dict[str, object]] = []
    default_counts: dict[str, int] | None = None
    for threshold in thresholds:
        gallery = Gallery(gallery_ids, vectors, threshold=threshold, margin=margin)
        true_accepts = false_rejects = wrong_person = false_accepts = correct = 0
        for expected, vector in known_probes:
            matched, _score, _second = gallery.match(vector)
            if matched is None:
                false_rejects += 1
            elif matched == expected:
                true_accepts += 1
                correct += 1
            else:
                wrong_person += 1
        for vector in unknown_probes:
            matched, _score, _second = gallery.match(vector)
            if matched is not None:
                false_accepts += 1
        counts = {
            "true_accepts": true_accepts,
            "false_rejects": false_rejects,
            "wrong_person": wrong_person,
            "false_accepts": false_accepts,
            "correct_top1": correct,
        }
        threshold_rows.append(
            {
                "threshold": threshold,
                "true_accept_rate": _rate(true_accepts, len(known_probes)),
                "false_reject_rate": _rate(false_rejects, len(known_probes)),
                "wrong_person_rate": _rate(wrong_person, len(known_probes)),
                "false_accept_rate": _rate(false_accepts, len(unknown_probes)),
                "top1_accuracy": _rate(correct, len(known_probes))
                if threshold == default_threshold
                else None,
                **counts,
            }
        )
        if threshold == default_threshold:
            default_counts = counts

    eligible = [
        row
        for row in threshold_rows
        if row["false_accept_rate"] is not None and row["false_accept_rate"] <= 0.001
    ]
    best_fa = max(eligible, key=lambda row: row["true_accept_rate"] or 0) if eligible else None
    return {
        "threshold_rows": threshold_rows,
        "dropped": dict(sorted(dropped.items())),
        "usable": dict(usable),
        "known_probe_count": len(known_probes),
        "unknown_probe_count": len(unknown_probes),
        "gallery_count": len(gallery_ids),
        "default_threshold": default_threshold,
        "default_top1_accuracy": _rate(
            (default_counts or {}).get("correct_top1", 0), len(known_probes)
        )
        if default_counts is not None
        else None,
        "best_fa_threshold": best_fa["threshold"] if best_fa else None,
        "best_fa_true_accept_rate": best_fa["true_accept_rate"] if best_fa else None,
        "margin": margin,
    }


def load_dataset(dataset_dir: Path) -> dict[str, list[ImageSample]]:
    if not dataset_dir.is_dir():
        raise FileNotFoundError(f"Dataset directory does not exist: {dataset_dir}")
    result: dict[str, list[ImageSample]] = {}
    for person_dir in sorted(path for path in dataset_dir.iterdir() if path.is_dir()):
        paths = sorted(
            path
            for path in person_dir.iterdir()
            if path.is_file() and path.suffix.lower() in {".jpg", ".jpeg", ".png"}
        )
        samples: list[ImageSample] = []
        for path in paths:
            image = cv2.imread(str(path), cv2.IMREAD_COLOR)
            if image is None:
                samples.append(ImageSample(person_dir.name, str(path), np.empty((0,), dtype=np.uint8)))
            else:
                samples.append(ImageSample(person_dir.name, str(path), image))
        result[person_dir.name] = samples
    if "_unknown" not in result:
        result["_unknown"] = []
    return result


def write_reports(result: dict[str, object], output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / "report.csv"
    html_path = output_dir / "report.html"
    rows = result["threshold_rows"]
    fields = [
        "threshold",
        "true_accept_rate",
        "false_reject_rate",
        "wrong_person_rate",
        "false_accept_rate",
        "top1_accuracy",
        "true_accepts",
        "false_rejects",
        "wrong_person",
        "false_accepts",
    ]
    with csv_path.open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        writer.writerows({key: row.get(key) for key in fields} for row in rows)

    html_path.write_text(_render_html(result), encoding="utf-8")
    return html_path, csv_path


def _percentage(value: object) -> str:
    return "N/A" if value is None else f"{100 * float(value):.3f}%"


def _render_html(result: dict[str, object]) -> str:
    rows = result["threshold_rows"]
    width, height, pad = 800, 280, 40
    chart_points: list[str] = []
    for key, color in (
        ("true_accept_rate", "#16803c"),
        ("false_reject_rate", "#d97706"),
        ("wrong_person_rate", "#7c3aed"),
        ("false_accept_rate", "#dc2626"),
    ):
        points = []
        for index, row in enumerate(rows):
            value = row[key]
            if value is None:
                continue
            x = pad + index * (width - 2 * pad) / max(1, len(rows) - 1)
            y = height - pad - float(value) * (height - 2 * pad)
            points.append(f"{x:.1f},{y:.1f}")
        if points:
            chart_points.append(
                f'<polyline fill="none" stroke="{color}" stroke-width="2" points="{" ".join(points)}"/>'
            )
    lines = []
    for value in (0, 0.25, 0.5, 0.75, 1):
        y = height - pad - value * (height - 2 * pad)
        lines.append(
            f'<line x1="{pad}" y1="{y:.1f}" x2="{width-pad}" y2="{y:.1f}" stroke="#ddd"/>'
            f'<text x="4" y="{y+4:.1f}" font-size="12">{value:.0%}</text>'
        )
    table_rows = "".join(
        "<tr>"
        f"<td>{row['threshold']:.2f}</td>"
        f"<td>{_percentage(row['true_accept_rate'])}</td>"
        f"<td>{_percentage(row['false_reject_rate'])}</td>"
        f"<td>{_percentage(row['wrong_person_rate'])}</td>"
        f"<td>{_percentage(row['false_accept_rate'])}</td>"
        f"<td>{_percentage(row['top1_accuracy'])}</td>"
        "</tr>"
        for row in rows
    )
    reason_rows = "".join(
        f"<tr><td>{html.escape(reason)}</td><td>{count}</td></tr>"
        for reason, count in result["dropped"].items()
    ) or "<tr><td colspan='2'>None</td></tr>"
    best_threshold = result["best_fa_threshold"]
    best_text = (
        "No threshold met the false-accept rate target."
        if best_threshold is None
        else (
            f"Threshold {best_threshold:.2f}: "
            f"true-accept rate {_percentage(result['best_fa_true_accept_rate'])} "
            "at false-accept rate <= 0.1%."
        )
    )
    return f"""<!doctype html>
<html lang="en"><meta charset="utf-8"><title>Offline accuracy evaluation</title>
<style>body{{font:16px system-ui,sans-serif;max-width:1100px;margin:2rem auto;padding:0 1rem}}
table{{border-collapse:collapse;width:100%;margin:1rem 0}}th,td{{border:1px solid #ccc;padding:.4rem;text-align:left}}
th{{background:#eee}}svg{{width:100%;height:auto}}.legend span{{margin-right:1rem}}</style>
<h1>Offline accuracy evaluation</h1>
<p>Gallery embeddings: {result['gallery_count']} · Known probes: {result['known_probe_count']} ·
Unknown probes: {result['unknown_probe_count']} · Matcher margin: {result['margin']:.3f}</p>
<p>Top-1 accuracy at default threshold {result['default_threshold']:.3f}:
{_percentage(result['default_top1_accuracy'])}</p>
<p>{html.escape(best_text)}</p>
<h2>Rates across thresholds</h2>
<svg viewBox="0 0 {width} {height}" role="img" aria-label="Accuracy rates by threshold">
{"".join(lines)}{"".join(chart_points)}
<text x="{pad}" y="{height-5}" font-size="12">Threshold 0.20 to 0.60</text></svg>
<p class="legend"><span style="color:#16803c">True accept</span><span style="color:#d97706">False reject</span>
<span style="color:#7c3aed">Wrong person</span><span style="color:#dc2626">False accept</span></p>
<table><thead><tr><th>Threshold</th><th>True accept</th><th>False reject</th>
<th>Wrong person</th><th>False accept</th><th>Top-1 accuracy at default</th></tr></thead>
<tbody>{table_rows}</tbody></table>
<h2>Dropped image count by reason</h2>
<table><thead><tr><th>Reason</th><th>Count</th></tr></thead><tbody>{reason_rows}</tbody></table>
</html>"""


def _load_models() -> tuple[FaceDetector, FaceEmbedder, FaceQuality]:
    model_dir = Path(__file__).resolve().parents[1] / "models"
    detector_path = model_dir / "face_detection_yunet_2023mar.onnx"
    embedder_path = model_dir / "face_recognition_sface_2021dec.onnx"
    missing = [str(path) for path in (detector_path, embedder_path) if not path.is_file()]
    if missing:
        raise FileNotFoundError(
            "Missing model files; run `python -m tools.download_models`: " + ", ".join(missing)
        )
    return FaceDetector(detector_path), FaceEmbedder(embedder_path), FaceQuality()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate face recognition on an offline dataset.")
    parser.add_argument("--dataset", type=Path, default=Path("data/eval"))
    parser.add_argument("--k", type=int, default=3, help="Gallery images per person.")
    parser.add_argument("--margin", type=float, default=0.05)
    parser.add_argument("--default-threshold", type=float, default=DEFAULT_THRESHOLD)
    args = parser.parse_args(argv)
    try:
        detector, embedder, quality = _load_models()
        samples = load_dataset(args.dataset)
        result = evaluate_samples(
            samples,
            detector,
            embedder,
            quality,
            k=args.k,
            margin=args.margin,
            default_threshold=args.default_threshold,
        )
        html_path, csv_path = write_reports(result, args.dataset)
    except (FileNotFoundError, ValueError, cv2.error) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(f"HTML report: {html_path}")
    print(f"CSV report: {csv_path}")
    print(f"Dropped image counts by reason: {json.dumps(result['dropped'], sort_keys=True)}")
    print(f"Top-1 accuracy at default threshold {args.default_threshold:.3f}: {_percentage(result['default_top1_accuracy'])}")
    if result["best_fa_threshold"] is None:
        print("No threshold met false-accept <= 0.1%.")
    else:
        print(
            f"Threshold with false-accept <= 0.1%: {result['best_fa_threshold']:.2f}; "
            f"highest true-accept rate: {_percentage(result['best_fa_true_accept_rate'])}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
