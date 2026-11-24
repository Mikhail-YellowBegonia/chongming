"""Compute JSON content SHA-256 (not file-byte SHA-256) for local assets."""

from __future__ import annotations

import argparse
from pathlib import Path

from chongming.contracts.json_codec import content_sha256, parse_json


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", type=Path, nargs="+")
    args = parser.parse_args()
    try:
        for path in args.paths:
            digest = content_sha256(parse_json(path.read_bytes()))
            print(f"{digest}  {path}")
    except (OSError, ValueError) as error:
        parser.exit(1, f"JSON content hashing failed: {error}\n")


if __name__ == "__main__":
    main()
