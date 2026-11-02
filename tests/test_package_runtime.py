"""Package resources and explicit data paths, independent of a source checkout."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import struct
import tempfile
import unittest
from contextlib import chdir
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

from chongming.contracts.scene import SphereGeometry
from chongming.preview import PreviewRuntime
from chongming.rendering.godot import GODOT_VERSION, GodotRenderer
from chongming.scenarios.authoring import compile_authoring, demo_authoring, save_authoring
from chongming.scenarios.demo import demo_source
from chongming.scenarios.json_content import content_sha256, parse_json
from chongming.scenarios.run import _package_digests, _provenance
from tests.test_scene_rendering import snapshot, test_scene

ROOT = Path(__file__).resolve().parents[1]


class PackageProvenanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.enterContext(
            patch("chongming.scenarios.run.platform.platform", return_value="test-platform")
        )

    def test_package_digest_includes_resources_but_not_caches(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory)
            contents = {
                "__init__.py": b"",
                "py.typed": b"",
                "web/preview.html": b"<html></html>",
                "rendering/godot/worker.gd": b"extends Node\n",
                "rendering/godot/terrain.gdshader": b"shader_type spatial;\n",
            }
            for name, content in contents.items():
                path = package / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
            for name in (
                "__pycache__/example.pyc",
                "rendering/godot/.godot/cache",
                "worker.gd.uid",
            ):
                path = package / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"cache")
            self.assertEqual(
                _package_digests(package),
                {
                    f"chongming/{name}": hashlib.sha256(content).hexdigest()
                    for name, content in contents.items()
                },
            )
            with (
                patch("chongming.scenarios.run.files", return_value=package),
                patch("chongming.scenarios.run.distribution") as metadata,
            ):
                metadata.return_value.read_text.return_value = None
                first = _provenance()
                (package / "web/preview.html").write_text("<html>changed</html>")
                second = _provenance()
            self.assertNotEqual(first["package_digest"], second["package_digest"])

    def test_installed_package_does_not_use_the_callers_repository(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".git").mkdir()
            (root / "uv.lock").write_text("unrelated lock")
            with (
                chdir(root),
                patch("chongming.scenarios.run.distribution") as metadata,
                patch("chongming.scenarios.run.subprocess.check_output") as git,
            ):
                metadata.return_value.read_text.return_value = None
                result = _provenance()
            git.assert_not_called()
            self.assertEqual(result["installation"], "installed")
            self.assertIsNone(result["code_revision"])
            self.assertIsNone(result["worktree_dirty"])
            self.assertIsNone(result["dependency_lock_digest"])
            self.assertTrue(result["package_digests"])

    def test_editable_metadata_identifies_the_workspace_without_parent_guessing(self) -> None:
        with tempfile.TemporaryDirectory(prefix="workspace with spaces ") as directory:
            root = Path(directory)
            (root / ".git").write_text("gitdir: elsewhere\n")
            lock = b"workspace lock\n"
            (root / "uv.lock").write_bytes(lock)
            with (
                patch("chongming.scenarios.run.distribution") as metadata,
                patch("chongming.scenarios.run.subprocess.check_output") as git,
            ):
                metadata.return_value.read_text.return_value = json.dumps(
                    {"url": root.as_uri(), "dir_info": {"editable": True}}
                )
                git.side_effect = ["revision\n", " M file.py\n"]
                result = _provenance()
            self.assertEqual(result["installation"], "editable")
            self.assertEqual(result["code_revision"], "revision")
            self.assertTrue(result["worktree_dirty"])
            self.assertEqual(result["dependency_lock_digest"], hashlib.sha256(lock).hexdigest())
            self.assertEqual(git.call_count, 2)
            self.assertTrue(all(call.kwargs["cwd"] == root for call in git.call_args_list))

    def test_source_without_git_or_a_lock_reports_unavailable_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch("chongming.scenarios.run.distribution") as metadata,
                patch("chongming.scenarios.run.subprocess.check_output") as git,
            ):
                metadata.return_value.read_text.return_value = json.dumps(
                    {"url": Path(directory).as_uri(), "dir_info": {"editable": True}}
                )
                result = _provenance()
            git.assert_not_called()
            self.assertIsNone(result["code_revision"])
            self.assertIsNone(result["worktree_dirty"])
            self.assertIsNone(result["dependency_lock_digest"])
            self.assertTrue(result["package_digest"])


class PreviewPathTests(unittest.TestCase):
    def test_landscape_is_not_discovered_beside_the_scenario(self) -> None:
        document = demo_authoring(stations=1, duration_s=1)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "scene.json"
            save_authoring(document, path)
            (path.parent / "landscape-dem.json").write_text("{}")
            runtime = PreviewRuntime(
                compile_authoring(document),
                authoring_document=document,
                authoring_path=path,
            )
            self.assertEqual(runtime.editor_status()["landscapes"], [])
            token = runtime.begin_edit()["token"]
            with self.assertRaisesRegex(ValueError, "not provisioned"):
                runtime.edit_landscape("hannover", token=token)
            runtime.cancel_edit(token=token)

    def test_explicit_landscape_and_scenario_paths_survive_a_working_directory_change(self) -> None:
        document = demo_authoring(stations=1, duration_s=1)
        content = (ROOT / "scenarios/assets/osm_landscape_demo.json").read_bytes()
        digest = content_sha256(parse_json(content))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scene = root / "scenes/scene.json"
            asset = root / "assets/landscape.json"
            asset.parent.mkdir()
            asset.write_bytes(content)
            save_authoring(document, scene)
            with patch("chongming.preview._HANNOVER_CONTENT_SHA256", digest):
                with chdir(root):
                    runtime = PreviewRuntime(
                        compile_authoring(document),
                        authoring_document=document,
                        authoring_path=Path("scenes/scene.json"),
                        landscape_asset=Path("assets/landscape.json"),
                    )
                self.assertEqual(runtime.editor_status()["path"], str(scene))
                self.assertEqual(runtime.editor_status()["landscapes"], ["hannover"])
                token = runtime.begin_edit()["token"]
                asset.write_text(json.dumps(parse_json(content), indent=2) + "\n")
                result = runtime.edit_landscape("hannover", token=token)
                self.assertEqual(
                    result["document"]["osm"]["asset"],
                    {
                        "uri": "../assets/landscape.json",
                        "content_sha256": digest,
                    },
                )
                runtime.commit_edit(token=token)
                self.assertEqual(json.loads(scene.read_text())["osm"], result["document"]["osm"])

    def test_landscape_requires_a_scenario_and_a_matching_digest(self) -> None:
        document = demo_authoring(stations=1, duration_s=1)
        spec = compile_authoring(document)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            asset = root / "landscape.json"
            asset.write_text("{}")
            with self.assertRaisesRegex(ValueError, "requires --scenario"):
                PreviewRuntime(spec, landscape_asset=asset)
            with self.assertRaisesRegex(ValueError, "digest mismatch"):
                PreviewRuntime(spec, authoring_path=root / "scene.json", landscape_asset=asset)
            with self.assertRaises(FileNotFoundError):
                PreviewRuntime(
                    spec, authoring_path=root / "scene.json", landscape_asset=root / "missing.json"
                )

    def test_landscape_is_revalidated_before_editing(self) -> None:
        document = demo_authoring(stations=1, duration_s=1)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "scene.json"
            save_authoring(document, path)
            asset = root / "landscape.json"
            content = b"{}"
            asset.write_bytes(content)
            with patch(
                "chongming.preview._HANNOVER_CONTENT_SHA256", content_sha256(parse_json(content))
            ):
                runtime = PreviewRuntime(
                    compile_authoring(document),
                    authoring_document=document,
                    authoring_path=path,
                    landscape_asset=asset,
                )
                token = runtime.begin_edit()["token"]
                asset.write_bytes(b'{"changed":true}')
                with self.assertRaisesRegex(ValueError, "digest mismatch"):
                    runtime.edit_landscape("hannover", token=token)
                runtime.cancel_edit(token=token)


class GodotWorkspaceTests(unittest.TestCase):
    def test_each_render_sends_current_entities_and_reuses_resource_definitions(self) -> None:
        scene = snapshot(test_scene(SphereGeometry(1.0)))
        camera = scene.cameras[0]
        pixel_count = (
            camera.actual_intrinsics.image_width_px * camera.actual_intrinsics.image_height_px
        )
        pixels = b"\x10\x20\x30" * pixel_count
        header = struct.pack("<QQ", len(pixels), 1_000_000)
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "godot"
            project.mkdir()
            (project / "project.godot").write_text("config_version=5\n")
            (project / "worker.gd").write_text("extends Node\n")
            with (
                patch("chongming.rendering.godot.files", return_value=project.parent),
                patch("chongming.rendering.godot._godot_executable", return_value="godot"),
                patch("chongming.rendering.godot.subprocess.run") as version,
                patch("chongming.rendering.godot.subprocess.Popen") as process,
                patch("chongming.rendering.godot.socket.create_connection") as connect,
                patch("chongming.rendering.godot.uuid.uuid4", return_value=UUID(int=1)),
                patch(
                    "chongming.rendering.godot._read_line",
                    return_value=f"READY {UUID(int=1).hex}\n".encode(),
                ),
            ):
                version.return_value.stdout = f"{GODOT_VERSION}.stable.test\n"
                process.return_value.poll.return_value = None
                process.return_value.stdout = process.return_value.stderr = None
                connect.return_value.recv.side_effect = [header, pixels, header, pixels]
                renderer = GodotRenderer()
                try:
                    self.assertEqual(renderer.render(scene, camera), (pixels, 1.0))
                    self.assertEqual(renderer.render(scene, camera), (pixels, 1.0))
                    calls = connect.return_value.sendall.call_args_list
                    self.assertEqual(len(calls), 2)
                    first = json.loads(calls[0].args[0][4:])
                    second = json.loads(calls[1].args[0][4:])
                    self.assertTrue(first["entities"])
                    self.assertEqual(second["entities"], first["entities"])
                    self.assertTrue(first["resources"])
                    self.assertEqual(second["resources"], [])
                finally:
                    renderer.close()

    def test_read_only_package_project_uses_a_separate_working_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "godot"
            source.mkdir()
            (source / "project.godot").write_text("config_version=5\n")
            (source / "worker.gd").write_text("extends Node\n")
            source.chmod(0o555)
            try:
                with (
                    patch("chongming.rendering.godot.files", return_value=source.parent),
                    patch("chongming.rendering.godot._godot_executable", return_value="godot"),
                    patch("chongming.rendering.godot.subprocess.run") as version,
                    patch("chongming.rendering.godot.subprocess.Popen") as process,
                    patch("chongming.rendering.godot.socket.create_connection"),
                    patch("chongming.rendering.godot.uuid.uuid4", return_value=UUID(int=1)),
                    patch(
                        "chongming.rendering.godot._read_line",
                        return_value=f"READY {UUID(int=1).hex}\n".encode(),
                    ),
                ):
                    version.return_value.stdout = f"{GODOT_VERSION}.stable.test\n"
                    process.return_value.poll.return_value = None
                    process.return_value.stdout = process.return_value.stderr = None
                    renderer = GodotRenderer()
                    call = process.call_args
                    assert call is not None
                    project = Path(call.args[0][2])
                    workspace = Path(call.kwargs["cwd"])
                    try:
                        self.assertEqual(project, source)
                        self.assertNotEqual(workspace, source)
                        self.assertEqual((project / "worker.gd").read_text(), "extends Node\n")
                        (workspace / "cache").mkdir()
                    finally:
                        renderer.close()
                    self.assertFalse(workspace.exists())
                    renderer.close()
            finally:
                source.chmod(0o755)

    def test_failed_handshake_cleans_up_the_process_and_workspace(self) -> None:
        with (
            patch("chongming.rendering.godot._godot_executable", return_value="godot"),
            patch("chongming.rendering.godot.subprocess.run") as version,
            patch("chongming.rendering.godot.subprocess.Popen") as process,
            patch("chongming.rendering.godot.socket.create_connection"),
            patch("chongming.rendering.godot._read_line", return_value=b"invalid greeting\n"),
        ):
            version.return_value.stdout = f"{GODOT_VERSION}.stable.test\n"
            process.return_value.poll.return_value = None
            process.return_value.stdout = process.return_value.stderr = None
            with self.assertRaisesRegex(RuntimeError, "handshake failed"):
                GodotRenderer()
            call = process.call_args
            assert call is not None
            self.assertFalse(Path(call.kwargs["cwd"]).exists())
            process.return_value.wait.assert_called_once()

    @unittest.skipUnless(
        os.environ.get("CHONGMING_GODOT") or shutil.which("godot4") or shutil.which("godot"),
        "Godot 4 executable is required",
    )
    def test_renderer_works_outside_the_checkout(self) -> None:
        document = demo_authoring(stations=1, duration_s=0.01)
        spec = compile_authoring(document)
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
        with tempfile.TemporaryDirectory() as directory, chdir(directory):
            renderer = GodotRenderer()
            try:
                pixels, _ = renderer.render(scene, camera)
                self.assertEqual(len(pixels), 64 * 48 * 3)
            finally:
                renderer.close()


if __name__ == "__main__":
    unittest.main()
