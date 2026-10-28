import argparse
import sys

from app.camera.reader import capture_source, mask_credentials
import cv2


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe a webcam, video file, or RTSP source.")
    parser.add_argument("source")
    parser.add_argument("--out", default="frame.jpg")
    args = parser.parse_args()

    capture = None
    try:
        capture, _is_file = capture_source(args.source)
        if not capture.isOpened():
            print(f"Unable to open source: {mask_credentials(args.source)}", file=sys.stderr)
            return 1
        ok, frame = capture.read()
        if not ok or frame is None:
            print(f"No frame received from source: {mask_credentials(args.source)}", file=sys.stderr)
            return 1
        height, width = frame.shape[:2]
        fps = capture.get(cv2.CAP_PROP_FPS)
        codec_value = int(capture.get(cv2.CAP_PROP_FOURCC))
        codec = "".join(chr((codec_value >> (8 * index)) & 0xFF) for index in range(4)).rstrip("\x00")
        print(
            f"Source: {mask_credentials(args.source)}\n"
            f"Resolution: {width}x{height}\n"
            f"FPS: {fps:.2f}\n"
            f"Codec: {mask_credentials(codec or 'unknown')}"
        )
        if not cv2.imwrite(args.out, frame):
            print("Unable to save frame.", file=sys.stderr)
            return 1
        print(f"Frame saved to: {mask_credentials(args.out)}")
        return 0
    except Exception as exc:
        print(mask_credentials(str(exc)), file=sys.stderr)
        return 1
    finally:
        if capture is not None:
            capture.release()


if __name__ == "__main__":
    raise SystemExit(main())
