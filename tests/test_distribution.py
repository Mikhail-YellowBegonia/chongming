"""Release inventories, integrity checks and staged build behavior without network access."""

from __future__ import annotations

import io
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

from scripts.distribute import (
    REQUIRED_SOURCE,
    artifact_paths,
    build,
    bundle_contents,
    check,
    check_contents,
    check_wheel,
    checksum_text,
    read_tar,
    read_wheel,
    write_bundle,
)


class DistributionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.version = "0.0.0"
        self.root = f"chongming-{self.version}"
        self.paths = artifact_paths(self.directory, self.version)
        self.sources = {name: b"fixture\n" for name in REQUIRED_SOURCE}
        self.sources["pyproject.toml"] = b"""[project]
name = "chongming"
version = "0.0.0"
[project.scripts]
chongming-center = "chongming.development:main"
"""
        self.sources.update(
            {
                "src/chongming/__init__.py": b"",
                "src/chongming/py.typed": b"",
                "src/chongming/development.py": b"def main(): pass\n",
                "src/chongming/web/preview.html": b"<html></html>\n",
                "src/chongming/rendering/godot/project.godot": b"config_version=5\n",
                "src/chongming/rendering/godot/worker.gd": b"extends Node\n",
                "src/chongming/rendering/godot/worker.tscn": b"[gd_scene format=3]\n",
                "src/chongming/rendering/godot/terrain.gdshader": b"shader_type spatial;\n",
                "src/chongming/rendering/godot/ui.gd": b"extends Control\n",
                "src/chongming/rendering/godot/visualization.gd": b"extends RefCounted\n",
                "src/chongming/rendering/godot/evaluation.gd": b"extends RefCounted\n",
                "scenarios/center_clean.json": b'{"scenario_id": "fixture"}\n',
                "scenarios/assets/grid.json": b'{ "heights_m": [[0, 1], [2, 3]] }\n',
            }
        )
        self.metadata_root = f"chongming-{self.version}.dist-info/"
        self.wheel = {
            name.removeprefix("src/"): data
            for name, data in self.sources.items()
            if name.startswith("src/chongming/")
        }
        self.wheel.update(
            {
                f"{self.metadata_root}METADATA": (
                    b"Metadata-Version: 2.4\nName: chongming\nVersion: 0.0.0\nLicense-File: COPYING\n\n"
                ),
                f"{self.metadata_root}WHEEL": b"Wheel-Version: 1.0\n",
                f"{self.metadata_root}RECORD": b"",
                f"{self.metadata_root}entry_points.txt": (
                    b"[console_scripts]\nchongming-center = chongming.development:main\n"
                ),
                f"{self.metadata_root}licenses/COPYING": self.sources["COPYING"],
            }
        )

    def write_release(self) -> None:
        write_bundle(self.paths["sdist"], self.root, self.sources)
        with ZipFile(self.paths["wheel"], "w") as archive:
            for name, data in self.wheel.items():
                archive.writestr(name, data)
        self.paths["requirements"].write_text("numpy==2.1.0 --hash=sha256:fixture\n")
        write_bundle(self.paths["bundle"], self.root, bundle_contents(self.sources, self.paths))
        self.paths["checksums"].write_text(checksum_text(self.paths))

    def test_complete_release_preserves_documentation_and_asset_bytes(self) -> None:
        self.write_release()
        self.assertEqual(check_contents(self.directory, self.version), self.sources)
        bundle = read_tar(self.paths["bundle"], self.root)
        for name in (
            "README.md",
            "AGENTS.md",
            "COPYING",
            "docs/user-guide.md",
            "docs/development.md",
            "docs/design.md",
            "scenarios/assets/grid.json",
        ):
            self.assertEqual(bundle[name], self.sources[name])
        self.assertIn(self.paths["sdist"].name, bundle)
        self.assertIn(self.paths["wheel"].name, bundle)
        self.assertIn("requirements.txt", bundle)
        self.assertFalse(any(name.startswith(("src/", "tests/", "scripts/")) for name in bundle))

    def test_source_resource_missing_from_wheel_is_rejected(self) -> None:
        del self.wheel["chongming/rendering/godot/terrain.gdshader"]
        with self.assertRaisesRegex(ValueError, "differ from the sdist"):
            check_wheel(self.sources, self.wheel, self.version)

    def test_required_resource_missing_from_both_archives_is_rejected(self) -> None:
        del self.sources["src/chongming/web/preview.html"]
        del self.wheel["chongming/web/preview.html"]
        with self.assertRaisesRegex(ValueError, "missing.*preview.html"):
            check_wheel(self.sources, self.wheel, self.version)

    def test_visualization_resources_are_required_even_if_both_archives_omit_them(self) -> None:
        for filename in ("ui.gd", "visualization.gd", "evaluation.gd"):
            with self.subTest(filename=filename):
                name = f"chongming/rendering/godot/{filename}"
                sources = {
                    key: value for key, value in self.sources.items() if key != f"src/{name}"
                }
                wheel = {key: value for key, value in self.wheel.items() if key != name}
                with self.assertRaisesRegex(ValueError, f"missing.*{filename}"):
                    check_wheel(sources, wheel, self.version)

    def test_wheel_license_and_entry_points_are_checked(self) -> None:
        for name in ("licenses/COPYING", "entry_points.txt"):
            with self.subTest(name=name):
                wheel = {**self.wheel, f"{self.metadata_root}{name}": b""}
                with self.assertRaises(ValueError):
                    check_wheel(self.sources, wheel, self.version)

    def test_wheel_name_and_version_are_checked(self) -> None:
        self.wheel[f"{self.metadata_root}METADATA"] = b"Name: other\nVersion: 9\n\n"
        with self.assertRaisesRegex(ValueError, "name or version"):
            check_wheel(self.sources, self.wheel, self.version)

    def test_unexpected_wheel_top_level_is_rejected(self) -> None:
        self.wheel["scripts/unrelated.py"] = b""
        with self.assertRaisesRegex(ValueError, "top-level"):
            check_wheel(self.sources, self.wheel, self.version)

    def test_missing_required_document_is_rejected(self) -> None:
        for name in (
            "AGENTS.md",
            "docs/user-guide.md",
            "docs/development.md",
            "docs/design.md",
        ):
            with self.subTest(name=name):
                content = self.sources.pop(name)
                self.write_release()
                with self.assertRaisesRegex(ValueError, "sdist is missing"):
                    check_contents(self.directory, self.version)
                self.sources[name] = content

    def test_tampered_release_file_is_rejected_by_checksums(self) -> None:
        self.write_release()
        self.paths["wheel"].write_bytes(b"modified")
        with self.assertRaisesRegex(ValueError, "SHA256SUMS"):
            check_contents(self.directory, self.version)

    def test_user_archive_cannot_change_assets_even_with_updated_outer_checksums(self) -> None:
        self.write_release()
        contents = bundle_contents(self.sources, self.paths)
        contents["scenarios/assets/grid.json"] = b"{}"
        write_bundle(self.paths["bundle"], self.root, contents)
        self.paths["checksums"].write_text(checksum_text(self.paths))
        with self.assertRaisesRegex(ValueError, "User archive differs"):
            check_contents(self.directory, self.version)

    def test_contents_stage_does_not_build_or_install(self) -> None:
        self.write_release()
        with patch("scripts.distribute.run_uv") as run:
            check(self.directory, self.version, "contents", False)
        run.assert_not_called()

    def test_check_rejects_unrelated_release_files(self) -> None:
        self.write_release()
        (self.directory / "unrelated.whl").write_bytes(b"not part of this release")
        with self.assertRaisesRegex(ValueError, "unrelated"):
            check_contents(self.directory, self.version)

    def test_user_archive_is_deterministic(self) -> None:
        contents = {"README.md": b"example\n", "scenarios/scene.json": b"{}\n"}
        write_bundle(self.paths["bundle"], self.root, contents)
        first = self.paths["bundle"].read_bytes()
        write_bundle(self.paths["bundle"], self.root, contents)
        self.assertEqual(first, self.paths["bundle"].read_bytes())

    def test_archive_rejects_unsafe_generated_and_duplicate_paths(self) -> None:
        for name in ("../escape", "tmp/cache", ".venv/bin/python", "__pycache__/module.pyc"):
            with self.subTest(name=name):
                write_bundle(self.paths["sdist"], self.root, {name: b""})
                with self.assertRaises(ValueError):
                    read_tar(self.paths["sdist"], self.root)
        with tarfile.open(self.paths["sdist"], "w:gz") as archive:
            for _ in range(2):
                info = tarfile.TarInfo(f"{self.root}/duplicate")
                archive.addfile(info, io.BytesIO(b""))
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            read_tar(self.paths["sdist"], self.root)

    def test_archive_rejects_symlinks(self) -> None:
        with tarfile.open(self.paths["sdist"], "w:gz") as archive:
            info = tarfile.TarInfo(f"{self.root}/link")
            info.type = tarfile.SYMTYPE
            info.linkname = "target"
            archive.addfile(info)
        with self.assertRaisesRegex(ValueError, "regular file"):
            read_tar(self.paths["sdist"], self.root)

    def test_wheel_reader_rejects_generated_paths(self) -> None:
        with ZipFile(self.paths["wheel"], "w") as archive:
            archive.writestr("chongming/__pycache__/module.pyc", b"")
        with self.assertRaisesRegex(ValueError, "Generated artifact"):
            read_wheel(self.paths["wheel"])

    def test_build_does_not_overwrite_unrelated_files(self) -> None:
        unrelated = self.directory / "notes.txt"
        unrelated.write_text("keep me")
        with (
            patch("scripts.distribute.run_uv") as run,
            self.assertRaisesRegex(ValueError, "unrelated"),
        ):
            build(self.directory, self.version, "all")
        run.assert_not_called()
        self.assertEqual(unrelated.read_text(), "keep me")

    def test_bundle_stage_exports_the_locked_source_snapshot(self) -> None:
        self.write_release()

        def export(*arguments: str, quiet: bool = False) -> None:
            self.assertEqual(arguments[0], "export")
            self.assertIn("--locked", arguments)
            self.assertIn("--no-default-groups", arguments)
            self.assertIn("--no-emit-project", arguments)
            self.assertTrue(quiet)
            snapshot = Path(arguments[arguments.index("--project") + 1])
            self.assertEqual((snapshot / "uv.lock").read_bytes(), self.sources["uv.lock"])
            self.assertEqual(
                (snapshot / "pyproject.toml").read_bytes(), self.sources["pyproject.toml"]
            )
            Path(arguments[arguments.index("--output-file") + 1]).write_text(
                "locked requirements\n"
            )

        with patch("scripts.distribute.run_uv", side_effect=export) as run:
            build(self.directory, self.version, "bundle")
        run.assert_called_once()
        self.assertEqual(check_contents(self.directory, self.version), self.sources)

    def test_wheel_stage_resumes_from_the_existing_sdist(self) -> None:
        write_bundle(self.paths["sdist"], self.root, self.sources)
        with patch("scripts.distribute.run_uv") as run:
            build(self.directory, self.version, "wheel")
        run.assert_called_once()
        self.assertEqual(run.call_args.args[:3], ("build", "--wheel", str(self.paths["sdist"])))


if __name__ == "__main__":
    unittest.main()
