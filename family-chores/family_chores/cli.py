import argparse
import os
from pathlib import Path

import segno

from .app import init_db


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("init-db", "make-qr"))
    args = parser.parse_args()
    path = os.environ.get("DATABASE_PATH", str(Path(__file__).resolve().parent.parent / "data" / "chores.sqlite3"))
    if args.command == "init-db":
        init_db(path)
        print("initialized", path)
    else:
        output = Path(__file__).resolve().parent.parent / "static" / "qr.svg"
        segno.make("https://home.moralife.uk").save(output, kind="svg", scale=6)
        print("wrote", output)


if __name__ == "__main__":
    main()
