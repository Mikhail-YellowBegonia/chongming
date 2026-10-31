"""Build local release artifacts and check them independently; never publish them."""

from __future__ import annotations

import argparse
import configparser
import gzip
import hashlib
import io
import shlex
import subprocess
import sys
import tarfile
import tempfile
import tomllib
from email.parser import BytesParser
from pathlib import Path, PurePosixPath
from zipfile import BadZipFile, ZipFile

SOURCE_ROOT = Path(__file__).resolve().parents[1]
REQUIRED_SOURCE = {
    "pyproject.toml",
    "uv.lock",
    "flake.nix",
    "flake.lock",
    "treefmt.toml",
    ".editorconfig",
    ".gitignore",
    "README.md",
    "AGENTS.md",
    "COPYING",
    "docs/user-guide.md",
    "docs/development.md",
    "docs/design.md",
    "scripts/distribute.py",
    "scripts/check_installed.py",
    "tests/test_distribution.py",
}
FORBIDDEN_PARTS = {
    ".git",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    ".godot",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    "build",
    "dist",
    "tmp",
    "temp",
    "output",
    "results",
}


def artifact_paths(directory: Path, version: str) -> dict[str, Path]:
    stem = f"chongming-{version}"
    return {
        "sdist": directory / f"{stem}.tar.gz",
        "wheel": directory / f"{stem}-py3-none-any.whl",
        "bundle": directory / f"{stem}-user.tar.gz",
        "requirements": directory / "requirements.txt",
        "checksums": directory / "SHA256SUMS",
    }


def run_uv(*arguments: str, quiet: bool = False) -> None:
    print(f"$ uv {shlex.join(arguments)}", flush=True)
    subprocess.run(
        ["uv", *arguments],
        check=True,
        stdout=subprocess.DEVNULL if quiet else None,
    )


def require_files(contents: dict[str, bytes], required: set[str], label: str) -> None:
    missing = required - contents.keys()
    if missing:
        raise ValueError(f"{label} is missing: {', '.join(sorted(missing))}")


def check_path(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or "\\" in name:
        raise ValueError(f"Unsafe archive path: {name}")
    if FORBIDDEN_PARTS.intersection(path.parts) or path.suffix in {".pyc", ".pyo", ".uid"}:
        raise ValueError(f"Generated artifact in distribution: {name}")
    return path


def read_tar(path: Path, root: str) -> dict[str, bytes]:
    contents = {}
    with tarfile.open(path, "r:gz") as archive:
        for member in archive:
            name = check_path(member.name)
            if not name.parts or name.parts[0] != root:
                raise ValueError(f"Unexpected archive root: {member.name}")
            if member.isdir():
                continue
            if not member.isfile() or len(name.parts) < 2:
                raise ValueError(f"Expected a regular file: {member.name}")
            relative = name.relative_to(root).as_posix()
            if relative in contents:
                raise ValueError(f"Duplicate archive member: {member.name}")
            stream = archive.extractfile(member)
            assert stream is not None
            with stream:
                contents[relative] = stream.read()
    return contents


def read_wheel(path: Path) -> dict[str, bytes]:
    contents = {}
    with ZipFile(path) as archive:
        for member in archive.infolist():
            check_path(member.filename)
            if member.is_dir():
                continue
            if member.filename in contents:
                raise ValueError(f"Duplicate wheel member: {member.filename}")
            contents[member.filename] = archive.read(member)
    return contents


def check_wheel(sources: dict[str, bytes], wheel: dict[str, bytes], version: str) -> None:
    package = {
        name.removeprefix("src/"): data
        for name, data in sources.items()
        if name.startswith("src/chongming/")
    }
    require_files(
        package,
        {
            "chongming/__init__.py",
            "chongming/py.typed",
            "chongming/web/preview.html",
            "chongming/rendering/godot/project.godot",
            "chongming/rendering/godot/worker.gd",
            "chongming/rendering/godot/worker.tscn",
            "chongming/rendering/godot/terrain.gdshader",
            "chongming/rendering/godot/ui.gd",
            "chongming/rendering/godot/visualization.gd",
            "chongming/rendering/godot/evaluation.gd",
        },
        "Source package",
    )
    installed = {name: data for name, data in wheel.items() if name.startswith("chongming/")}
    if package != installed:
        raise ValueError("Wheel package contents differ from the sdist")
    metadata_root = f"chongming-{version}.dist-info/"
    require_files(
        wheel,
        {
            f"{metadata_root}{name}"
            for name in ("METADATA", "WHEEL", "entry_points.txt", "RECORD", "licenses/COPYING")
        },
        "Wheel",
    )
    if any(not name.startswith(("chongming/", metadata_root)) for name in wheel):
        raise ValueError("Unexpected top-level wheel contents")
    metadata = BytesParser().parsebytes(wheel[f"{metadata_root}METADATA"])
    if (metadata["Name"], metadata["Version"]) != ("chongming", version):
        raise ValueError("Wheel name or version does not match the source project")
    if metadata.get_all("License-File") != ["COPYING"]:
        raise ValueError("Wheel license metadata does not declare COPYING")
    if wheel[f"{metadata_root}licenses/COPYING"] != sources["COPYING"]:
        raise ValueError("Wheel license differs from the source license")
    entry_points = configparser.ConfigParser()
    entry_points.read_string(wheel[f"{metadata_root}entry_points.txt"].decode())
    project = tomllib.loads(sources["pyproject.toml"].decode())["project"]
    if (project["name"], project["version"]) != ("chongming", version):
        raise ValueError("sdist project name or version does not match the release")
    if (
        "console_scripts" not in entry_points
        or dict(entry_points["console_scripts"]) != project["scripts"]
    ):
        raise ValueError("Installed command entry points differ from pyproject.toml")


def bundle_contents(
    sources: dict[str, bytes],
    paths: dict[str, Path],
) -> dict[str, bytes]:
    contents = {
        name: data
        for name, data in sources.items()
        if name.endswith(".md") or name.startswith("scenarios/") or name == "COPYING"
    }
    for key in ("sdist", "wheel", "requirements"):
        contents[paths[key].name] = paths[key].read_bytes()
    return contents


def checksum_text(paths: dict[str, Path]) -> str:
    return "".join(
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n"
        for key, path in sorted(paths.items())
        if key != "checksums"
    )


def write_bundle(path: Path, root: str, contents: dict[str, bytes]) -> None:
    with (
        path.open("wb") as output,
        gzip.GzipFile(filename="", fileobj=output, mode="wb", mtime=0) as compressed,
        tarfile.open(fileobj=compressed, mode="w") as archive,
    ):
        for name, data in sorted(contents.items()):
            info = tarfile.TarInfo(f"{root}/{name}")
            info.size = len(data)
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(data))


def check_directory(directory: Path, paths: dict[str, Path]) -> None:
    allowed = {path.name for path in paths.values()}
    for path in directory.iterdir():
        if path.name not in allowed or path.is_symlink() or not path.is_file():
            raise ValueError(
                f"Output directory contains unrelated or non-regular files: {path.name}"
            )


def build(directory: Path, version: str, stage: str) -> None:
    paths = artifact_paths(directory, version)
    directory.mkdir(parents=True, exist_ok=True)
    check_directory(directory, paths)
    if stage in {"all", "sdist"}:
        run_uv(
            "build",
            "--sdist",
            str(SOURCE_ROOT),
            "--python",
            sys.executable,
            "--out-dir",
            str(directory),
            "--no-create-gitignore",
        )
    if stage in {"all", "wheel"}:
        run_uv(
            "build",
            "--wheel",
            str(paths["sdist"]),
            "--python",
            sys.executable,
            "--out-dir",
            str(directory),
            "--no-create-gitignore",
        )
    if stage in {"all", "bundle"}:
        root = f"chongming-{version}"
        sources = read_tar(paths["sdist"], root)
        require_files(sources, REQUIRED_SOURCE, "sdist")
        check_wheel(sources, read_wheel(paths["wheel"]), version)
        # Export from the built source snapshot, not a possibly changed checkout.
        with tempfile.TemporaryDirectory(prefix="chongming-export-") as temporary:
            with tarfile.open(paths["sdist"], "r:gz") as archive:
                archive.extractall(temporary, filter="data")
            run_uv(
                "export",
                "--project",
                str(Path(temporary) / root),
                "--locked",
                "--no-default-groups",
                "--no-emit-project",
                "--no-header",
                "--no-annotate",
                "--output-file",
                str(paths["requirements"]),
                quiet=True,
            )
        write_bundle(paths["bundle"], root, bundle_contents(sources, paths))
        paths["checksums"].write_text(checksum_text(paths), encoding="utf-8")
    print(f"Build stage '{stage}' complete: {directory}", flush=True)


def check_contents(directory: Path, version: str) -> dict[str, bytes]:
    paths = artifact_paths(directory, version)
    check_directory(directory, paths)
    if paths["checksums"].read_text(encoding="utf-8") != checksum_text(paths):
        raise ValueError("SHA256SUMS does not match the release files")
    root = f"chongming-{version}"
    sources = read_tar(paths["sdist"], root)
    require_files(sources, REQUIRED_SOURCE, "sdist")
    for prefix in ("tests/", "scripts/", "docs/", "scenarios/"):
        if not any(name.startswith(prefix) for name in sources):
            raise ValueError(f"sdist is missing {prefix}")
    check_wheel(sources, read_wheel(paths["wheel"]), version)
    if read_tar(paths["bundle"], root) != bundle_contents(sources, paths):
        raise ValueError("User archive differs from its source, wheel or dependency list")
    print(
        "PASS: checksums, source contents, wheel resources, license, entry points and user archive",
        flush=True,
    )
    return sources


def check(directory: Path, version: str, stage: str, render: bool) -> None:
    sources = check_contents(directory, version)
    if stage == "contents":
        return
    paths = artifact_paths(directory, version)
    with tempfile.TemporaryDirectory(prefix="chongming-check-") as temporary:
        workspace = Path(temporary)
        if stage == "all":
            rebuilt = workspace / "rebuilt"
            run_uv(
                "build",
                "--wheel",
                str(paths["sdist"]),
                "--python",
                sys.executable,
                "--out-dir",
                str(rebuilt),
                "--no-create-gitignore",
            )
            if read_wheel(rebuilt / paths["wheel"].name) != read_wheel(paths["wheel"]):
                raise ValueError("Rebuilding the sdist produced different wheel contents")
            print("PASS: wheel rebuilt from sdist", flush=True)
        environment = workspace / "venv"
        python = environment / "bin/python"
        run_uv("venv", "--python", sys.executable, str(environment))
        run_uv(
            "pip",
            "install",
            "--python",
            str(python),
            "--require-hashes",
            "--no-deps",
            "--requirements",
            str(paths["requirements"]),
        )
        run_uv("pip", "install", "--python", str(python), "--no-deps", str(paths["wheel"]))
        run_uv("pip", "check", "--python", str(python))
        release = workspace / "release"
        with tarfile.open(paths["bundle"], "r:gz") as archive:
            archive.extractall(release, filter="data")
        probe = workspace / "check_installed.py"
        probe.write_bytes(sources["scripts/check_installed.py"])
        work = workspace / "work"
        work.mkdir()
        command = [
            str(python),
            "-I",
            "-B",
            str(probe),
            "--wheel",
            str(paths["wheel"]),
            "--scenarios",
            str(release / f"chongming-{version}" / "scenarios"),
        ]
        if render:
            command.append("--render")
        subprocess.run(command, cwd=work, check=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    builder = commands.add_parser("build", help="Build local release files without publishing")
    builder.add_argument("--stage", choices=("all", "sdist", "wheel", "bundle"), default="all")
    checker = commands.add_parser("check", help="Verify existing release files independently")
    checker.add_argument("--stage", choices=("all", "contents", "install"), default="all")
    checker.add_argument(
        "--render", action="store_true", help="Also require Godot and a graphics context"
    )
    for command in (builder, checker):
        command.add_argument("--output", type=Path, default=Path("dist"))
    args = parser.parse_args()
    version = tomllib.loads((SOURCE_ROOT / "pyproject.toml").read_text())["project"]["version"]
    try:
        if args.command == "build":
            build(args.output.resolve(), version, args.stage)
        else:
            if args.render and args.stage == "contents":
                parser.error("--render requires an installation check")
            check(args.output.resolve(), version, args.stage, args.render)
    except (
        OSError,
        ValueError,
        tarfile.TarError,
        BadZipFile,
        configparser.Error,
        subprocess.CalledProcessError,
    ) as error:
        parser.exit(1, f"Distribution {args.command} failed: {error}\n")


if __name__ == "__main__":
    main()
