from __future__ import annotations

import argparse
import base64
import getpass
import secrets
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from werkzeug.security import generate_password_hash

from .config import load_config
from .storage import init_db
from .worker import run_once


def b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def print_vapid() -> None:
    private = ec.generate_private_key(ec.SECP256R1())
    numbers = private.private_numbers()
    public = private.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    print("PULSE_VAPID_PUBLIC_KEY=" + b64url(public))
    print("PULSE_VAPID_PRIVATE_KEY=" + b64url(numbers.private_value.to_bytes(32, "big")))
    print("PULSE_VAPID_SUBJECT=mailto:you@example.com")


def main() -> None:
    parser = argparse.ArgumentParser(description="Pulse maintenance commands")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("secret")
    sub.add_parser("password")
    sub.add_parser("vapid")
    sub.add_parser("init-db")
    sub.add_parser("check")
    args = parser.parse_args()
    if args.command == "secret":
        print("PULSE_SECRET_KEY=" + secrets.token_urlsafe(48))
    elif args.command == "password":
        password = getpass.getpass("Pulse password: ")
        if len(password) < 8:
            raise SystemExit("password must be at least 8 characters")
        print("PULSE_PASSWORD_HASH=" + generate_password_hash(password))
    elif args.command == "vapid":
        print_vapid()
    else:
        config = load_config()
        init_db(config["DATABASE_PATH"])
        if args.command == "check":
            print(run_once(config))
        else:
            print(config["DATABASE_PATH"])


if __name__ == "__main__":
    main()
