#!/usr/bin/env python3
"""Set or rotate a PoE2 Companion user's password.

Usage:
    python set_password.py <username>

Prompts for a new password and writes a fresh scrypt hash into data/secrets.json.
The user record is created if it doesn't exist.
"""

import getpass
import hashlib
import json
import os
import secrets
import sys
from pathlib import Path

SECRETS_PATH = Path(__file__).parent / "data" / "secrets.json"


def hash_pw(pw: str, n: int = 2**14, r: int = 8, p: int = 1) -> str:
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=n, r=r, p=p, dklen=32)
    return f"scrypt$1${n}${r}${p}${salt.hex()}${h.hex()}"


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    username = sys.argv[1].strip().lower()

    if SECRETS_PATH.exists():
        data = json.loads(SECRETS_PATH.read_text())
    else:
        data = {"users": {}}
    users = data.setdefault("users", {})

    pw = getpass.getpass(f"New password for {username}: ")
    pw2 = getpass.getpass("Confirm: ")
    if pw != pw2:
        print("Passwords do not match.")
        return 1
    if len(pw) < 4:
        print("Password too short.")
        return 1

    user = users.setdefault(username, {})
    user["password_hash"] = hash_pw(pw)

    SECRETS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SECRETS_PATH.write_text(json.dumps(data, indent=2))
    os.chmod(SECRETS_PATH, 0o600)
    print(f"Updated password for {username} in {SECRETS_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
