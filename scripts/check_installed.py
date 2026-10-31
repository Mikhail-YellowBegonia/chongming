"""Probe a regular wheel installation from an isolated interpreter and working directory."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
import threading
from dataclasses import replace
from importlib.metadata import distribution
from pathlib import Path
from typing import Any
from urllib.request import urlopen
from zipfile import ZipFile


def check_preview(scenario: Path, expected_digest: str) -> None:
    from chongming.preview import PreviewRuntime, preview_server
    from chongming.scenarios.serialization import load_scenario

    server = preview_server(PreviewRuntime(load_scenario(scenario)), 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with urlopen(f"http://127.0.0.1:{server.server_port}/", timeout=10) as response:
            if (
                response.status != 200
                or hashlib.sha256(response.read()).hexdigest() != expected_digest
            ):
                raise RuntimeError("Preview did not serve the HTML from the checked wheel")
    finally:
        server.shutdown()
        thread.join(timeout=10)
        server.server_close()
    if thread.is_alive():
        raise RuntimeError("Preview HTTP server did not stop")
    print("PASS: preview HTML served from the installed package", flush=True)


def check_center_ui(scenario: Path) -> None:
    """Run real reference output through the installed UI and its bundled scripts."""
    from chongming.development import CenterSession
    from chongming.reference import create_algorithm
    from chongming.rendering.godot import UiImage, close_default_renderer, default_renderer
    from chongming.scenarios.authoring import save_authoring

    updates: list[dict[str, Any]] = []
    renderer = default_renderer()
    exchange = renderer.ui_exchange

    def record(state: dict[str, Any], images: tuple[UiImage, ...] = ()) -> list[dict[str, Any]]:
        commands = exchange(state, images)
        if "SCRIPT ERROR" in renderer._diagnostics():
            raise RuntimeError("Installed visualization reported a Godot script error")
        if "algorithm_output" in state:
            updates.append(state["algorithm_output"])
        return commands

    renderer.ui_exchange = record
    try:
        with tempfile.TemporaryDirectory(prefix="chongming-center-ui-") as directory:
            root = Path(directory)
            document = json.loads(scenario.read_text())
            document["duration_s"] = 0.12
            path = root / "scene.json"
            save_authoring(document, path)
            session = CenterSession(path, root / "run", algorithm=create_algorithm())
            session.run_ui(tick_hz=240)
            summary = json.loads((root / "run/summary.json").read_text())
            if summary["state"] != "complete" or summary["shutdown_errors"]:
                raise RuntimeError("Installed center UI did not complete and close cleanly")
            if [int(update["update_id"]) for update in updates] != list(
                range(1, session.step_count + 2)
            ):
                raise RuntimeError("Installed UI did not acknowledge every algorithm increment")
            if not any(update["center_events"]["measurements"] for update in updates):
                raise RuntimeError("Installed reference run did not produce measurements")
            if not any(
                update["center_events"]["voxel_space"] is not None
                and update["center_events"]["voxel_space"]["scored_voxels"]
                for update in updates
            ):
                raise RuntimeError("Installed reference run did not export raw voxel votes")
    finally:
        close_default_renderer()
    print(
        "PASS: installed center UI, acknowledged measurements and raw voxel snapshots", flush=True
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--scenarios", type=Path, required=True)
    parser.add_argument("--render", action="store_true")
    args = parser.parse_args()

    if not sys.flags.isolated or sys.prefix == sys.base_prefix:
        raise RuntimeError("Run this probe with a virtual environment's Python and -I")

    import chongming
    from chongming.scenarios.run import _provenance
    from chongming.scenarios.serialization import load_scenario, validate_supported_features

    if chongming.__file__ is None or not Path(chongming.__file__).is_relative_to(sys.prefix):
        raise RuntimeError("chongming was not imported from the isolated environment")
    metadata = distribution("chongming")
    provenance = _provenance()
    if provenance["installation"] != "installed":
        raise RuntimeError("An editable installation cannot validate a wheel")
    if any(
        provenance[key] is not None
        for key in (
            "code_revision",
            "worktree_dirty",
            "dependency_lock_digest",
        )
    ):
        raise RuntimeError("An installed package must not claim workspace provenance")
    with ZipFile(args.wheel) as wheel:
        expected = {
            name: hashlib.sha256(wheel.read(name)).hexdigest()
            for name in wheel.namelist()
            if name.startswith("chongming/") and not name.endswith("/")
        }
    if provenance["package_digests"] != expected:
        raise RuntimeError("Installed package files differ from the checked wheel")
    for entry in metadata.entry_points:
        if entry.group == "console_scripts":
            if not callable(entry.load()):
                raise RuntimeError(f"Invalid command entry point: {entry.name}")
            if not (Path(sys.executable).parent / entry.name).is_file():
                raise RuntimeError(f"Missing installed command: {entry.name}")
    scenarios = sorted(args.scenarios.glob("*.json"))
    if not scenarios:
        raise RuntimeError("No release scenarios were found")
    for scenario in scenarios:
        validate_supported_features(load_scenario(scenario))
    subprocess.run(
        [
            str(Path(sys.executable).parent / "chongming-center"),
            "--scenario",
            str(args.scenarios / "center_clean.json"),
            "--check",
        ],
        check=True,
        timeout=120,
    )
    print(
        f"PASS: isolated wheel import, package bytes, command entry points and {len(scenarios)} scenarios",
        flush=True,
    )

    check_preview(args.scenarios / "center_clean.json", expected["chongming/web/preview.html"])

    if args.render:
        from chongming.rendering.godot import close_default_renderer, default_renderer
        from chongming.scenarios.authoring import compile_authoring, demo_authoring, save_authoring
        from chongming.scenarios.demo import demo_source
        from chongming.scenarios.run import run_scene

        document = demo_authoring(stations=1, duration_s=0.01)
        spec = compile_authoring(document)
        try:
            with tempfile.TemporaryDirectory(prefix="chongming-render-") as directory:
                path = Path(directory) / "scene.json"
                save_authoring(document, path)
                summary = run_scene(path, Path(directory) / "evidence", video_enabled=False)
                if summary["status"] != "complete":
                    raise RuntimeError("Installed finite scene run did not complete")
                scene = next(
                    iter(demo_source(spec, video_enabled=False).generate(spec))
                ).truth.logical_scene
                camera = replace(
                    scene.cameras[0],
                    actual_intrinsics=replace(
                        scene.cameras[0].actual_intrinsics,
                        image_width_px=64,
                        image_height_px=48,
                        focal_x_px=50,
                        focal_y_px=50,
                        principal_x_px=32,
                        principal_y_px=24,
                    ),
                )
                pixels, _ = default_renderer().render(scene, camera)
                if len(pixels) != 64 * 48 * 3:
                    raise RuntimeError("Installed renderer returned an invalid image")
        finally:
            close_default_renderer()
        print("PASS: installed Godot renderer and finite evidence run", flush=True)
        check_center_ui(args.scenarios / "center_clean.json")

    if _provenance()["package_digests"] != expected:
        raise RuntimeError("Runtime checks modified the installed package files")
    print("PASS: installed package bytes unchanged by runtime checks", flush=True)


if __name__ == "__main__":
    main()
