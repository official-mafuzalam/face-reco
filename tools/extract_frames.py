import argparse
from collections import Counter
from pathlib import Path

import cv2

from app.vision.detector import FaceDetector
from app.vision.quality import FaceQuality


def extract_frames(
    video_path: Path,
    output_dir: Path,
    every: int = 5,
    detector=None,
    quality=None,
) -> tuple[int, Counter[str]]:
    if every < 1:
        raise ValueError("every must be at least one.")
    if detector is None or quality is None:
        model_path = Path(__file__).resolve().parents[1] / "models" / "face_detection_yunet_2023mar.onnx"
        if not model_path.is_file():
            raise FileNotFoundError(f"Missing detector model; run `python -m tools.download_models`: {model_path}")
        detector = detector or FaceDetector(model_path)
        quality = quality or FaceQuality()
    capture = cv2.VideoCapture(str(video_path))
    if not capture.isOpened():
        capture.release()
        raise OSError(f"Unable to open video: {video_path}")

    output_dir.mkdir(parents=True, exist_ok=True)
    saved = 0
    skipped: Counter[str] = Counter()
    frame_index = 0
    try:
        while True:
            ok, frame = capture.read()
            if not ok:
                break
            current_index = frame_index
            frame_index += 1
            if current_index % every:
                continue
            faces = detector.detect(frame)
            if not faces:
                skipped["no_face"] += 1
                continue
            if len(faces) != 1:
                skipped["multiple_faces"] += 1
                continue
            quality_ok, reasons = quality.check(frame, faces[0])
            if not quality_ok:
                skipped.update(reasons or ["quality_rejected"])
                continue
            destination = output_dir / f"frame_{current_index:08d}.jpg"
            if not cv2.imwrite(str(destination), frame):
                raise OSError(f"Unable to write extracted frame: {destination}")
            saved += 1
    finally:
        capture.release()
    return saved, skipped


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Extract quality-checked face frames from a video.")
    parser.add_argument("video", type=Path)
    parser.add_argument("out_dir", type=Path)
    parser.add_argument("--every", type=int, default=5)
    args = parser.parse_args(argv)
    try:
        saved, skipped = extract_frames(
            args.video,
            args.out_dir,
            every=args.every,
        )
    except (FileNotFoundError, ValueError, OSError, cv2.error) as exc:
        parser.exit(1, f"{exc}\n")
    print(f"Saved frames: {saved}")
    print(f"Skipped frames by reason: {dict(sorted(skipped.items()))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
