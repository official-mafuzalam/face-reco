import argparse
import sys

import cv2
import numpy as np

from app.camera.reader import capture_source, mask_credentials


def inspect_frame_quality(frame: np.ndarray) -> list[str]:
    """Inspect a camera frame for placement and image quality issues.
    
    Returns a list of advisory keys (e.g. 'cam_advisory_low_res', 'cam_advisory_low_light').
    """
    advisories: list[str] = []
    if frame is None or frame.size == 0:
        return ["cam_advisory_blurry"]

    height, width = frame.shape[:2]
    # Resolution check (minimum 720p recommended for reliable face recognition)
    if width < 1280 or height < 720:
        advisories.append("cam_advisory_low_res")

    # Grayscale conversion for brightness and contrast
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if len(frame.shape) == 3 else frame
    mean_brightness = float(np.mean(gray))

    if mean_brightness < 45.0:
        advisories.append("cam_advisory_low_light")
    elif mean_brightness > 215.0:
        advisories.append("cam_advisory_glare")

    std_contrast = float(np.std(gray))
    if std_contrast < 20.0:
        advisories.append("cam_advisory_low_contrast")

    # Focus / blur check via Laplacian variance
    laplacian_var = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    if laplacian_var < 50.0:
        advisories.append("cam_advisory_blurry")

    return advisories


def probe_source(source: str) -> dict:
    """Probe an RTSP, file, or webcam source and return video stats & quality advisories."""
    capture = None
    try:
        capture, _is_file = capture_source(source)
        if not capture.isOpened():
            return {"ok": False, "error": f"Unable to open source: {mask_credentials(source)}"}
        ok, frame = capture.read()
        if not ok or frame is None:
            return {"ok": False, "error": f"No frame received from source: {mask_credentials(source)}"}
        height, width = frame.shape[:2]
        fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0)
        codec_val = int(capture.get(cv2.CAP_PROP_FOURCC) or 0)
        codec = "".join(chr((codec_val >> (8 * i)) & 0xFF) for i in range(4)).rstrip("\x00") or "unknown"
        advisories = inspect_frame_quality(frame)
        return {
            "ok": True,
            "width": width,
            "height": height,
            "fps": round(fps, 2),
            "codec": mask_credentials(codec),
            "advisories": advisories,
        }
    except Exception as exc:
        return {"ok": False, "error": mask_credentials(str(exc))}
    finally:
        if capture is not None:
            capture.release()


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe a webcam, video file, or RTSP source.")
    parser.add_argument("source")
    parser.add_argument("--out", default="frame.jpg")
    args = parser.parse_args()

    result = probe_source(args.source)
    if not result["ok"]:
        print(f"Error: {result['error']}", file=sys.stderr)
        return 1

    print(
        f"Source: {mask_credentials(args.source)}\n"
        f"Resolution: {result['width']}x{result['height']}\n"
        f"FPS: {result['fps']:.2f}\n"
        f"Codec: {result['codec']}"
    )
    if result["advisories"]:
        print("\nQuality Advisories:")
        for adv in result["advisories"]:
            print(f" - {adv}")
    else:
        print("\nQuality: Good for face and surveillance recognition.")

    capture, _ = capture_source(args.source)
    try:
        ok, frame = capture.read()
        if ok and frame is not None:
            cv2.imwrite(args.out, frame)
            print(f"Frame saved to: {mask_credentials(args.out)}")
    finally:
        capture.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
