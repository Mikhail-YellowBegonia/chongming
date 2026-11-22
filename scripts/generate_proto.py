"""Generate and verify chongming_proto Python and type-stub bindings from proto/."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PROTO_ROOT = REPO_ROOT / "proto"
PROTO_FILES = (
    "chongming/v1/common.proto",
    "chongming/v1/online.proto",
    "chongming/v1/truth.proto",
)
OUT_DIR = REPO_ROOT / "src" / "chongming_proto" / "v1"


def _rewrite_imports(text: str) -> str:
    text = text.replace("from chongming.v1 import ", "from chongming_proto.v1 import ")
    text = text.replace("import chongming.v1.", "import chongming_proto.v1.")
    text = text.replace(", 'chongming.v1.", ", 'chongming_proto.v1.")
    return text


def render_proto_files(protoc: str) -> dict[str, bytes]:
    with tempfile.TemporaryDirectory(prefix="chongming-proto-") as tmp:
        tmp_path = Path(tmp)
        cmd = [
            protoc,
            f"-I{PROTO_ROOT}",
            f"--python_out={tmp_path}",
            f"--pyi_out={tmp_path}",
            *(str(PROTO_ROOT / rel) for rel in PROTO_FILES),
        ]
        subprocess.run(cmd, check=True)
        generated_dir = tmp_path / "chongming" / "v1"
        rendered: dict[str, bytes] = {}
        for path in sorted(generated_dir.iterdir()):
            if path.is_file() and (path.name.endswith("_pb2.py") or path.name.endswith("_pb2.pyi")):
                text = _rewrite_imports(path.read_text(encoding="utf-8"))
                rendered[path.name] = text.encode("utf-8")
        return rendered


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify generated files match proto/ without writing changes.",
    )
    args = parser.parse_args()

    protoc = shutil.which("protoc")
    if protoc is None:
        print("protoc executable not found on PATH; enter the Nix shell first.", file=sys.stderr)
        return 2

    rendered = render_proto_files(protoc)
    expected_names = set(rendered)

    if args.check:
        if not OUT_DIR.is_dir():
            print(f"Missing generated proto directory: {OUT_DIR}", file=sys.stderr)
            return 1
        existing = {
            p.name
            for p in OUT_DIR.iterdir()
            if p.is_file() and (p.name.endswith("_pb2.py") or p.name.endswith("_pb2.pyi"))
        }
        if existing != expected_names:
            print(
                f"Proto file set mismatch: expected {sorted(expected_names)}, "
                f"found {sorted(existing)}",
                file=sys.stderr,
            )
            return 1
        mismatches = [
            name
            for name, content in sorted(rendered.items())
            if (OUT_DIR / name).read_bytes() != content
        ]
        if mismatches:
            print(
                "Generated proto bindings are out of date for: "
                + ", ".join(mismatches)
                + ". Run scripts/generate_proto.py.",
                file=sys.stderr,
            )
            return 1
        print(f"Proto bindings up to date ({len(rendered)} files).", flush=True)
        return 0

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    init_file = OUT_DIR / "__init__.py"
    if not init_file.exists():
        init_file.write_text("", encoding="utf-8")
    for stale in OUT_DIR.iterdir():
        if (
            stale.is_file()
            and (stale.name.endswith("_pb2.py") or stale.name.endswith("_pb2.pyi"))
            and stale.name not in expected_names
        ):
            stale.unlink()
    for name, content in sorted(rendered.items()):
        (OUT_DIR / name).write_bytes(content)
    print(f"Generated {len(rendered)} proto files in {OUT_DIR}.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
