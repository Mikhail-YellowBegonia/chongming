"""Pack a local asset directory into a deterministic .tar.gz.

The archive bytes depend only on file names and contents: entries are sorted,
and mtime, owner and permission bits are normalized. The output digest can
therefore be pinned in flake.nix and re-checked after a restore.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import tarfile
from pathlib import Path


def pack(source: Path, output: Path) -> str:
    if output.exists():
        raise FileExistsError(f"refusing to overwrite {output}")
    files = sorted(p for p in source.rglob("*") if p.is_file())
    if not files:
        raise ValueError(f"no files under {source}")
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for path in files:
            info = tarfile.TarInfo(f"{source.name}/{path.relative_to(source).as_posix()}")
            info.size = path.stat().st_size
            info.mode = 0o644
            info.mtime = 0
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            with path.open("rb") as stream:
                archive.addfile(info, stream)
    compressed = gzip.compress(buffer.getvalue(), compresslevel=9, mtime=0)
    output.write_bytes(compressed)
    return hashlib.sha256(compressed).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="asset directory")
    parser.add_argument("output", type=Path, help="new .tar.gz path; never overwritten")
    args = parser.parse_args()
    print(pack(args.source, args.output))


if __name__ == "__main__":
    main()
