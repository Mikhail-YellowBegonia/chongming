"""Probe a regular wheel installation from an isolated interpreter and working directory."""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
from importlib.metadata import distribution
from importlib.resources import files
from pathlib import Path
from zipfile import ZipFile


def _package_digests(package_root: Path) -> dict[str, str]:
    digests: dict[str, str] = {}
    prefixes = ("chongming", "chongming_proto", "chongming_scorer")
    site_packages = package_root.parent
    for prefix in prefixes:
        root = site_packages / prefix
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if (
                path.is_file()
                and "__pycache__" not in path.parts
                and not path.name.endswith(".pyc")
            ):
                rel = path.relative_to(site_packages).as_posix()
                digests[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return digests


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--scenarios", type=Path, required=True)
    parser.add_argument("--render", action="store_true")
    args = parser.parse_args()
    if not sys.flags.isolated or sys.prefix == sys.base_prefix:
        raise RuntimeError("Run this probe with a virtual environment's Python and -I")

    import chongming
    from chongming.composition.center import reference_policy
    from chongming.contracts.model import VoxelSearchSpec, WorldFrameId
    from chongming.reference import create_algorithm

    if chongming.__file__ is None or not Path(chongming.__file__).is_relative_to(sys.prefix):
        raise RuntimeError("chongming was not imported from the isolated environment")
    package_root = Path(chongming.__file__).resolve().parent
    metadata = distribution("chongming")
    with ZipFile(args.wheel) as wheel:
        expected = {
            name: hashlib.sha256(wheel.read(name)).hexdigest()
            for name in wheel.namelist()
            if name.startswith(("chongming/", "chongming_proto/", "chongming_scorer/"))
            and not name.endswith("/")
        }
    if _package_digests(package_root) != expected:
        raise RuntimeError("Installed package files differ from the checked wheel")

    for entry in metadata.entry_points:
        if entry.group == "console_scripts":
            if not callable(entry.load()):
                raise RuntimeError(f"Invalid command entry point: {entry.name}")
            command = Path(sys.executable).parent / entry.name
            if not command.is_file():
                raise RuntimeError(f"Missing installed command: {entry.name}")
            subprocess.run(
                [str(command), "--help"], check=True, stdout=subprocess.DEVNULL, timeout=30
            )

    _ = reference_policy(
        search=VoxelSearchSpec(
            world_frame_id=WorldFrameId("enu"),
            min_enu_m=(-100.0, -100.0, 0.0),
            max_enu_m=(100.0, 150.0, 100.0),
            step_enu_m=(10.0, 10.0, 10.0),
            forward_range_m=None,
        ),
        clock_domain_id="sim-clock",
        window_width_ns=20_000_000,
    )
    _ = create_algorithm()
    godot_dir = files("chongming.rendering").joinpath("godot")
    for resource in ("project.godot", "worker.tscn", "worker.gd", "ui.gd", "visualization.gd"):
        if not godot_dir.joinpath(resource).is_file():
            raise RuntimeError(f"Missing bundled Godot viewer resource: {resource}")

    if _package_digests(package_root) != expected:
        raise RuntimeError("Runtime checks modified the installed package files")
    print(
        "PASS: isolated wheel import, package bytes, viewer assets and CLI entry points", flush=True
    )


if __name__ == "__main__":
    main()
