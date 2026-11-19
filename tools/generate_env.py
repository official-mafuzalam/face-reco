"""Create .env from .env.example with freshly generated secrets.

Usage: python -m tools.generate_env [--force]
"""

import argparse
import base64
import secrets
import sys
from pathlib import Path

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from py_vapid import Vapid


ROOT = Path(__file__).resolve().parents[1]


def generate_vapid_keypair() -> tuple[str, str]:
    v = Vapid()
    v.generate_keys()
    priv_num = v.private_key.private_numbers().private_value
    priv_bytes = priv_num.to_bytes(32, "big")
    b64priv = base64.urlsafe_b64encode(priv_bytes).decode().rstrip("=")
    pub_bytes = v.public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    b64pub = base64.urlsafe_b64encode(pub_bytes).decode().rstrip("=")
    return b64priv, b64pub


def build_env(template: str) -> str:
    priv_vapid, pub_vapid = generate_vapid_keypair()
    replacements = {
        "SECRET_KEY": secrets.token_urlsafe(48),
        "FERNET_KEY": Fernet.generate_key().decode(),
        "VAPID_PRIVATE_KEY": priv_vapid,
        "VAPID_PUBLIC_KEY": pub_vapid,
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
