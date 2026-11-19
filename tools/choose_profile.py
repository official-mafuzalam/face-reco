"""Pick the detector hardware profile for this PC (used by scripts/install.ps1).

python -m tools.choose_profile --cameras 4           print the recommended profile name
python -m tools.choose_profile --cameras 4 --notes   explain the choice
python -m tools.choose_profile --cameras 4 --write   store it in .env (never overrides an existing choice)
python -m tools.choose_profile --set KEY=VALUE       store any setting in .env unless it is already set
"""

import argparse
import sys
from pathlib import Path

from app.surveillance.detector import PROFILES, detect_hardware

ROOT = Path(__file__).resolve().parents[1]
GPU_MODEL = "qwen2.5vl:7b"


def recommend_profile(cameras: int, hardware: dict) -> str:
    """A GPU or OpenVINO profile is only recommended when onnxruntime can really use it."""
    providers = hardware.get("providers", [])
    if "CUDAExecutionProvider" in providers:
        return "cuda"
    if "OpenVINOExecutionProvider" in providers:
        return "openvino"
    if hardware.get("cpu_cores", 4) >= 8 or cameras > PROFILES["cpu_small"].max_cameras:
        return "cpu_large"
    return "cpu_small"


def notes(cameras: int, hardware: dict, profile: str) -> list[str]:
    found = []
    if hardware.get("has_cuda") and profile != "cuda":
        found.append("An NVIDIA GPU was found, but onnxruntime cannot use it. For GPU speed run "
                     "'pip uninstall onnxruntime' then 'pip install onnxruntime-gpu', and set HARDWARE_PROFILE=cuda.")
    limit = PROFILES[profile].max_cameras
    if cameras > limit:
        found.append(f"{cameras} cameras is more than the {profile} profile suits (about {limit}). "
                     "Expect delays; use fewer cameras or a GPU.")
    return found


def set_env_value(env_file: Path, key: str, value: str) -> bool:
    """Append key=value unless the file already sets that key. True when it was written."""
    lines = env_file.read_text(encoding="utf-8").splitlines()
    if any(line.split("=", 1)[0].strip() == key for line in lines if not line.lstrip().startswith("#")):
        return False
    env_file.write_text("\n".join([*lines, f"{key}={value}"]) + "\n", encoding="utf-8")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cameras", type=int, default=4)
    parser.add_argument("--notes", action="store_true")
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--set", metavar="KEY=VALUE", action="append", default=[])
    args = parser.parse_args(argv)
    if args.set:
        env_file = ROOT / ".env"
        if not env_file.exists():
            print(".env not found", file=sys.stderr)
            return 1
        for item in args.set:
            key, _, value = item.partition("=")
            print(f"{key}={value}" if set_env_value(env_file, key, value) else f"{key} already set in .env; kept")
        return 0
    hardware = detect_hardware()
    profile = recommend_profile(args.cameras, hardware)
    if args.notes:
        print("\n".join(notes(args.cameras, hardware, profile)))
    elif args.write:
        env_file = ROOT / ".env"
        if not env_file.exists():
            print(".env not found", file=sys.stderr)
            return 1
        written = set_env_value(env_file, "HARDWARE_PROFILE", profile)
        if written and profile == "cuda":
            set_env_value(env_file, "OLLAMA_MODEL", GPU_MODEL)  # a GPU can afford the larger model
        print(f"HARDWARE_PROFILE={profile}" if written else "HARDWARE_PROFILE already set in .env; kept")
    else:
        print(profile)
    return 0


if __name__ == "__main__":
    sys.exit(main())
