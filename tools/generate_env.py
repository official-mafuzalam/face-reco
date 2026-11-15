"""Create .env from .env.example with freshly generated secrets.

Usage: python -m tools.generate_env [--force]
"""

import argparse
import secrets
import sys
from pathlib import Path

from cryptography.fernet import Fernet


ROOT = Path(__file__).resolve().parents[1]


def build_env(template: str) -> str:
    replacements = {
        "SECRET_KEY": secrets.token_urlsafe(48),
        "FERNET_KEY": Fernet.generate_key().decode(),
    }
    lines = []
    for line in template.splitlines():
        key = line.split("=", 1)[0].strip()
        lines.append(f"{key}={replacements[key]}" if key in replacements else line)
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="overwrite an existing .env")
    args = parser.parse_args()
    target = ROOT / ".env"
    if target.exists() and not args.force:
        print(".env already exists; refusing to overwrite (changing FERNET_KEY makes saved "
              "camera URLs and face templates unreadable). Use --force only on a fresh install.")
        return 1
    target.write_text(build_env((ROOT / ".env.example").read_text(encoding="utf-8")), encoding="utf-8")
    print(f"Wrote {target} with new SECRET_KEY and FERNET_KEY. Back up FERNET_KEY safely.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
