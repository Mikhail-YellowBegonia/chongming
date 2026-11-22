"""Build an unmodified, hash-checked official ViBe library with the Nix C compiler."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


def _sha256(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _write_json(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)


REVISION = "da0c197d36272125589180e20fdd32b6fe3ba548"
FILES = {
    "C/vibe-background-sequential.c": "5cc26dc93365cfd42e81b8f229165dc6048d6f429fefbce26ff750326dedc26d",
    "C/vibe-background-sequential.h": "463aa554b637da1a86ce857856847da13ba8cce47650e2546ed454f53497db3e",
    "LICENSE": "c71d239df91726fc519c6eb72d318ec65820627232b2f796219e87dcf35d0ab4",
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    for name, digest in FILES.items():
        if _sha256(args.source / name) != digest:
            raise ValueError(f"official ViBe source hash mismatch: {name}")
    compiler = shutil.which("cc")
    if compiler is None:
        raise FileNotFoundError("enter the repository Nix environment to obtain its C compiler")
    args.output.mkdir(parents=True, exist_ok=False)
    for name in FILES:
        target = args.output / "source" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(args.source / name, target)
    library = args.output / ("libvibe.dylib" if sys.platform == "darwin" else "libvibe.so")
    command = [
        compiler,
        "-std=c99",
        "-O2",
        "-fPIC",
        "-dynamiclib" if sys.platform == "darwin" else "-shared",
        str((args.output / "source/C/vibe-background-sequential.c").resolve()),
        "-o",
        str(library.resolve()),
    ]
    subprocess.run(command, check=True, capture_output=True, text=True)
    manifest = {
        "source": "https://github.com/vandroogenbroeckmarc/vibe",
        "revision": REVISION,
        "license": "Apache-2.0 (current repository grant)",
        "source_sha256": FILES,
        "source_modified": False,
        "command": command,
        "compiler": subprocess.check_output([compiler, "--version"], text=True),
        "platform": platform.platform(),
        "library": str(library.resolve()),
        "library_sha256": _sha256(library),
        "parameters": {"samples": 30, "threshold": 10, "minimum_matches": 2, "update_factor": 8},
        "history": "new pinned official baseline; not a reproduction of the unrecovered historical project attempt",
    }
    _write_json(args.output / "manifest.json", manifest)
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
