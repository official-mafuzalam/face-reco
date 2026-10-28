import hashlib
import os
import tempfile
import urllib.request
from pathlib import Path


MODELS = {
    "face_detection_yunet_2023mar.onnx": (
        "https://github.com/opencv/opencv_zoo/raw/refs/heads/main/"
        "models/face_detection_yunet/face_detection_yunet_2023mar.onnx"
    ),
    "face_recognition_sface_2021dec.onnx": (
        "https://github.com/opencv/opencv_zoo/raw/refs/heads/main/"
        "models/face_recognition_sface/face_recognition_sface_2021dec.onnx"
    ),
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as model_file:
        for chunk in iter(lambda: model_file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_model(url: str, destination: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "gate-face-model-downloader"})
    with urllib.request.urlopen(request, timeout=60) as response:
        first_chunk = response.read(4096)
        if first_chunk.startswith(b"version https://git-lfs.github.com/spec/"):
            raise RuntimeError(f"URL returned a Git LFS pointer, not model data: {url}")
        if not first_chunk:
            raise RuntimeError(f"URL returned an empty model: {url}")

        destination.parent.mkdir(parents=True, exist_ok=True)
        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.",
            suffix=".download",
            dir=destination.parent,
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(file_descriptor, "wb") as output:
                output.write(first_chunk)
                while chunk := response.read(1024 * 1024):
                    output.write(chunk)
            os.replace(temporary_path, destination)
        except Exception:
            temporary_path.unlink(missing_ok=True)
            raise


def main() -> None:
    models_dir = Path(__file__).resolve().parents[1] / "models"
    for filename, url in MODELS.items():
        destination = models_dir / filename
        if not destination.exists():
            download_model(url, destination)
            print(f"Downloaded {destination}")
        else:
            print(f"Skipped existing file {destination}")
        print(f"SHA256 {filename}: {sha256_file(destination)}")


if __name__ == "__main__":
    main()
