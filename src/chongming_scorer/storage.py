"""Immutable Test Scene and independent Run Output package readers and writers."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path, PurePosixPath
from types import TracebackType
from typing import Any, Self

from google.protobuf.json_format import MessageToDict, ParseDict

from chongming_proto.online import PbZstWriter, iter_pb_zst, online_pb2
from chongming_proto.truth import truth_pb2

TEST_SCENE_FORMAT = "chongming-test-scene-v1"
RUN_OUTPUT_FORMAT = "chongming-run-output-v1"
VALID_RUN_MODES = frozenset({"edge", "center", "integrated"})
_REQUIRED_SCENE_STREAMS = ("frame_index.pb.zst", "truth.pb.zst", "virtual_rays.pb.zst")


def sha256_file(path: Path) -> str:
    """Return the lowercase SHA-256 hex digest of a file."""
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def write_json_atomic(path: Path, value: Any, *, exclusive: bool = False) -> None:
    """Write JSON atomically via a temporary file in the same directory."""
    if exclusive and path.exists():
        raise FileExistsError(path)
    payload = json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("x" if exclusive else "w", encoding="utf-8") as handle:
        handle.write(payload)
    if exclusive and path.exists():
        temporary.unlink(missing_ok=True)
        raise FileExistsError(path)
    temporary.replace(path)


def canonical_manifest_sha256(manifest: Mapping[str, Any]) -> str:
    """Hash canonical UTF-8 JSON of a manifest excluding `content_sha256`."""
    payload = {key: value for key, value in manifest.items() if key != "content_sha256"}
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validate_relpath(relpath: str) -> PurePosixPath:
    if not relpath or "\\" in relpath:
        raise ValueError(f"invalid package relative path: {relpath!r}")
    posix = PurePosixPath(relpath)
    if posix.is_absolute() or any(part in {"", ".", ".."} for part in posix.parts):
        raise ValueError(f"unsafe package relative path: {relpath!r}")
    return posix


def _resolve_package_file(
    root: Path,
    relpath: str,
    *,
    allow_symlink: bool = False,
) -> Path:
    posix = _validate_relpath(relpath)
    root_resolved = root.resolve()
    candidate = root.joinpath(*posix.parts)
    if not allow_symlink and candidate.is_symlink():
        raise ValueError(f"package file must not be a symlink: {relpath}")
    if allow_symlink:
        if not candidate.parent.resolve().is_relative_to(root_resolved):
            raise ValueError(f"package path escapes root: {relpath}")
    else:
        if not candidate.resolve().is_relative_to(root_resolved):
            raise ValueError(f"package path escapes root: {relpath}")
    if not candidate.is_file():
        raise FileNotFoundError(f"missing package file: {candidate}")
    return candidate


def _verify_file_entry(
    root: Path,
    relpath: str,
    entry: Mapping[str, Any],
    *,
    allow_symlink: bool = False,
    check_digest: bool = True,
) -> Path:
    path = _resolve_package_file(root, relpath, allow_symlink=allow_symlink)
    if check_digest:
        expected_bytes = entry.get("bytes")
        expected_sha = entry.get("sha256")
        if type(expected_bytes) is not int or expected_bytes < 0:
            raise ValueError(f"invalid byte count in manifest for {relpath}")
        if not isinstance(expected_sha, str) or len(expected_sha) != 64:
            raise ValueError(f"invalid sha256 in manifest for {relpath}")
        actual_bytes = path.stat().st_size
        if actual_bytes != expected_bytes:
            raise ValueError(
                f"byte count mismatch for {relpath}: expected {expected_bytes}, got {actual_bytes}"
            )
        actual_sha = sha256_file(path)
        if actual_sha != expected_sha:
            raise ValueError(
                f"sha256 mismatch for {relpath}: expected {expected_sha}, got {actual_sha}"
            )
    return path


class TestSceneWriter:
    """Create a fresh immutable Test Scene directory package."""

    def __init__(
        self,
        root: Path,
        *,
        scenario_id: str,
        world_frame_id: str,
        clock_domain_id: str,
        start_time_ns: int,
        end_time_ns: int,
        stations: Sequence[online_pb2.StationConfig],
        occlusion_mode: str = "known",
        random_seed: int | None = None,
        targets_meta: Mapping[str, Mapping[str, Any]] | None = None,
        flush_every: int = 256,
    ) -> None:
        if occlusion_mode not in {"known", "unknown"}:
            raise ValueError(f"unsupported occlusion_mode: {occlusion_mode!r}")
        self.root = root
        self.root.mkdir(parents=True, exist_ok=False)
        (self.root / "video").mkdir()
        self.scenario_id = scenario_id
        self.world_frame_id = world_frame_id
        self.clock_domain_id = clock_domain_id
        self.start_time_ns = int(start_time_ns)
        self.end_time_ns = int(end_time_ns)
        self.stations = list(stations)
        self.occlusion_mode = occlusion_mode
        self.random_seed = random_seed
        self.targets_meta: dict[str, dict[str, Any]] = {
            k: dict(v) for k, v in (targets_meta or {}).items()
        }
        self._video_meta: dict[str, dict[str, Any]] = {}
        self._video_files: dict[str, dict[str, Any]] = {}
        self._frame_index_writer = PbZstWriter(
            self.root / "frame_index.pb.zst", flush_every=flush_every
        )
        self._truth_writer = PbZstWriter(self.root / "truth.pb.zst", flush_every=flush_every)
        self._virtual_rays_writer = PbZstWriter(
            self.root / "virtual_rays.pb.zst", flush_every=flush_every
        )
        self._frame_index_writer.__enter__()
        self._truth_writer.__enter__()
        self._virtual_rays_writer.__enter__()
        self._finalized = False
        self._closed = False

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if exc_type is None and not self._finalized:
            self.finalize()
        else:
            self.close()

    def add_frame_index(self, entry: truth_pb2.FrameIndexEntry) -> None:
        self._ensure_open()
        self._frame_index_writer.write(entry)

    def add_truth_frame(self, frame: truth_pb2.TruthFrame) -> None:
        self._ensure_open()
        self._truth_writer.write(frame)

    def add_virtual_ray(self, ray: online_pb2.RayEvent) -> None:
        self._ensure_open()
        self._virtual_rays_writer.write(ray)

    def add_video_file(
        self,
        source: Path,
        relpath: str,
        metadata: Mapping[str, Any],
        *,
        symlink: bool = False,
    ) -> None:
        self._ensure_open()
        posix = _validate_relpath(relpath)
        if not posix.parts or posix.parts[0] != "video":
            raise ValueError(f"video file must reside under video/: {relpath}")
        if not source.is_file():
            raise FileNotFoundError(f"missing source video file: {source}")
        dest = self.root.joinpath(*posix.parts)
        if dest.exists() or dest.is_symlink():
            raise FileExistsError(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if symlink:
            os.symlink(source.resolve(), dest)
        else:
            shutil.copyfile(source, dest)
        self._video_meta[relpath] = dict(metadata)
        self._video_files[relpath] = {
            "sha256": sha256_file(dest),
            "bytes": dest.stat().st_size,
        }

    def _ensure_open(self) -> None:
        if self._finalized or self._closed:
            raise RuntimeError("TestSceneWriter is already closed or finalized")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for writer in (
            self._frame_index_writer,
            self._truth_writer,
            self._virtual_rays_writer,
        ):
            writer.close()

    def finalize(self) -> dict[str, Any]:
        if self._finalized:
            raise RuntimeError("TestSceneWriter already finalized")
        self.close()
        files: dict[str, dict[str, Any]] = {}
        for relpath in _REQUIRED_SCENE_STREAMS:
            path = self.root / relpath
            files[relpath] = {
                "sha256": sha256_file(path),
                "bytes": path.stat().st_size,
            }
        files.update(self._video_files)
        manifest: dict[str, Any] = {
            "format": TEST_SCENE_FORMAT,
            "scenario_id": self.scenario_id,
            "world_frame_id": self.world_frame_id,
            "clock_domain_id": self.clock_domain_id,
            "start_time_ns": self.start_time_ns,
            "end_time_ns": self.end_time_ns,
            "random_seed": self.random_seed,
            "occlusion_mode": self.occlusion_mode,
            "stations": [
                MessageToDict(station, preserving_proto_field_name=True)
                for station in self.stations
            ],
            "targets": self.targets_meta,
            "video": self._video_meta,
            "files": files,
        }
        manifest["content_sha256"] = canonical_manifest_sha256(manifest)
        write_json_atomic(self.root / "manifest.json", manifest, exclusive=True)
        self._finalized = True
        return manifest


class TestScenePackage:
    """Read-only accessor for an immutable Test Scene package."""

    def __init__(self, root: Path, manifest: dict[str, Any]) -> None:
        self.root = root
        self.manifest = manifest
        self._stations = tuple(
            ParseDict(item, online_pb2.StationConfig()) for item in manifest.get("stations", ())
        )

    @classmethod
    def open(
        cls,
        path: Path,
        *,
        verify_hashes: bool = True,
        verify_videos: bool = False,
    ) -> TestScenePackage:
        root = path
        if not root.is_dir():
            raise FileNotFoundError(f"Test Scene directory not found: {root}")
        manifest_path = root / "manifest.json"
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise FileNotFoundError(f"Test Scene manifest.json missing: {manifest_path}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict) or manifest.get("format") != TEST_SCENE_FORMAT:
            raise ValueError(f"invalid Test Scene manifest format in {manifest_path}")
        expected_digest = manifest.get("content_sha256")
        if expected_digest != canonical_manifest_sha256(manifest):
            raise ValueError("Test Scene manifest content_sha256 mismatch")
        files = manifest.get("files")
        if not isinstance(files, dict):
            raise TypeError("Test Scene manifest missing files mapping")
        for required in _REQUIRED_SCENE_STREAMS:
            if required not in files:
                raise ValueError(f"Test Scene missing required stream {required}")
        for relpath, entry in files.items():
            if not isinstance(relpath, str) or not isinstance(entry, dict):
                raise TypeError("Test Scene files entries must map relative paths to objects")
            posix = _validate_relpath(relpath)
            is_video = bool(posix.parts and posix.parts[0] == "video")
            check_digest = verify_hashes and (verify_videos or not is_video)
            _verify_file_entry(
                root,
                relpath,
                entry,
                allow_symlink=is_video,
                check_digest=check_digest,
            )
        return cls(root, manifest)

    @property
    def content_sha256(self) -> str:
        value = self.manifest["content_sha256"]
        assert isinstance(value, str)
        return value

    @property
    def scenario_id(self) -> str:
        value = self.manifest["scenario_id"]
        assert isinstance(value, str)
        return value

    @property
    def start_time_ns(self) -> int:
        return int(self.manifest["start_time_ns"])

    @property
    def end_time_ns(self) -> int:
        return int(self.manifest["end_time_ns"])

    @property
    def occlusion_mode(self) -> str:
        return str(self.manifest.get("occlusion_mode", "known"))

    def station_configs(self) -> tuple[online_pb2.StationConfig, ...]:
        return self._stations

    def iter_frame_index(self) -> Iterator[truth_pb2.FrameIndexEntry]:
        return iter_pb_zst(self.root / "frame_index.pb.zst", truth_pb2.FrameIndexEntry)

    def iter_truth_frames(self) -> Iterator[truth_pb2.TruthFrame]:
        return iter_pb_zst(self.root / "truth.pb.zst", truth_pb2.TruthFrame)

    def iter_virtual_rays(self) -> Iterator[online_pb2.RayEvent]:
        return iter_pb_zst(self.root / "virtual_rays.pb.zst", online_pb2.RayEvent)


class RunOutputWriter:
    """Create a fresh per-run Run Output directory package."""

    def __init__(
        self,
        root: Path,
        *,
        run_id: str,
        mode: str,
        test_scene_path: Path,
        test_scene_sha256: str,
        provenance: Mapping[str, Any] | None = None,
        flush_every: int = 256,
    ) -> None:
        if mode not in VALID_RUN_MODES:
            raise ValueError(f"unsupported run mode: {mode!r}")
        if not run_id:
            raise ValueError("run_id must be non-empty")
        if not test_scene_sha256:
            raise ValueError("test_scene_sha256 must be non-empty")
        self.root = root
        self.root.mkdir(parents=True, exist_ok=False)
        self.run_id = run_id
        self.mode = mode
        self.test_scene_path = str(test_scene_path)
        self.test_scene_sha256 = test_scene_sha256
        self.provenance = dict(provenance or {})
        self.flush_every = flush_every
        self._edge_writer: PbZstWriter | None = None
        self._center_writer: PbZstWriter | None = None
        self._voxel_writer: PbZstWriter | None = None
        self._written_relpaths: list[str] = []
        self._finalized = False
        self._closed = False

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if exc_type is None and not self._finalized:
            self.finalize()
        else:
            self.close()

    def _ensure_open(self) -> None:
        if self._finalized or self._closed:
            raise RuntimeError("RunOutputWriter is already closed or finalized")

    def write_edge_message(self, message: online_pb2.BusMessage) -> None:
        self._ensure_open()
        if self._edge_writer is None:
            self._edge_writer = PbZstWriter(
                self.root / "edge_events.pb.zst", flush_every=self.flush_every
            )
            self._edge_writer.__enter__()
            self._written_relpaths.append("edge_events.pb.zst")
        self._edge_writer.write(message)

    def write_center_message(self, message: online_pb2.BusMessage) -> None:
        self._ensure_open()
        if self._center_writer is None:
            self._center_writer = PbZstWriter(
                self.root / "center_output.pb.zst", flush_every=self.flush_every
            )
            self._center_writer.__enter__()
            self._written_relpaths.append("center_output.pb.zst")
        self._center_writer.write(message)

    def write_voxel_grid(self, grid: online_pb2.SparseVoxelGrid) -> None:
        self._ensure_open()
        if self._voxel_writer is None:
            self._voxel_writer = PbZstWriter(
                self.root / "voxels.pb.zst", flush_every=self.flush_every
            )
            self._voxel_writer.__enter__()
            self._written_relpaths.append("voxels.pb.zst")
        self._voxel_writer.write(grid)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for writer in (self._edge_writer, self._center_writer, self._voxel_writer):
            if writer is not None:
                writer.close()

    def finalize(self) -> dict[str, Any]:
        if self._finalized:
            raise RuntimeError("RunOutputWriter already finalized")
        self.close()
        files: dict[str, dict[str, Any]] = {}
        for relpath in self._written_relpaths:
            path = self.root / relpath
            files[relpath] = {
                "sha256": sha256_file(path),
                "bytes": path.stat().st_size,
            }
        manifest: dict[str, Any] = {
            "format": RUN_OUTPUT_FORMAT,
            "run_id": self.run_id,
            "mode": self.mode,
            "test_scene_path": self.test_scene_path,
            "test_scene_sha256": self.test_scene_sha256,
            "provenance": self.provenance,
            "files": files,
        }
        write_json_atomic(self.root / "run_manifest.json", manifest, exclusive=True)
        self._finalized = True
        return manifest


def persist_scores_output(
    dest: Path,
    scores: Sequence[truth_pb2.ScoreEvent],
    report: Mapping[str, Any],
) -> tuple[Path, Path]:
    """Write scores.pb.zst and report.json exclusively into dest without overwriting."""
    dest.mkdir(parents=True, exist_ok=True)
    scores_path = dest / "scores.pb.zst"
    report_path = dest / "report.json"
    if scores_path.exists() or report_path.exists():
        raise FileExistsError(f"refusing to overwrite existing scoring output in {dest}")
    with PbZstWriter(scores_path, overwrite=False) as writer:
        for event in scores:
            writer.write(event)
    write_json_atomic(report_path, dict(report), exclusive=True)
    return scores_path, report_path


class RunOutputPackage:
    """Reader and score-writer for an independent Run Output directory."""

    def __init__(self, root: Path, manifest: dict[str, Any]) -> None:
        self.root = root
        self.manifest = manifest

    @classmethod
    def open(cls, path: Path, *, verify_hashes: bool = True) -> RunOutputPackage:
        root = path
        if not root.is_dir():
            raise FileNotFoundError(f"Run Output directory not found: {root}")
        manifest_path = root / "run_manifest.json"
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise FileNotFoundError(f"Run Output run_manifest.json missing: {manifest_path}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict) or manifest.get("format") != RUN_OUTPUT_FORMAT:
            raise ValueError(f"invalid Run Output manifest format in {manifest_path}")
        mode = manifest.get("mode")
        if mode not in VALID_RUN_MODES:
            raise ValueError(f"invalid Run Output mode: {mode!r}")
        if not isinstance(manifest.get("run_id"), str) or not manifest["run_id"]:
            raise ValueError("Run Output manifest missing run_id")
        if (
            not isinstance(manifest.get("test_scene_sha256"), str)
            or not manifest["test_scene_sha256"]
        ):
            raise ValueError("Run Output manifest missing test_scene_sha256")
        files = manifest.get("files")
        if not isinstance(files, dict):
            raise TypeError("Run Output manifest missing files mapping")
        for relpath, entry in files.items():
            if not isinstance(relpath, str) or not isinstance(entry, dict):
                raise TypeError("Run Output files entries must map relative paths to objects")
            _verify_file_entry(
                root,
                relpath,
                entry,
                allow_symlink=False,
                check_digest=verify_hashes,
            )
        return cls(root, manifest)

    @property
    def run_id(self) -> str:
        return str(self.manifest["run_id"])

    @property
    def mode(self) -> str:
        return str(self.manifest["mode"])

    @property
    def test_scene_sha256(self) -> str:
        return str(self.manifest["test_scene_sha256"])

    @property
    def has_edge_events(self) -> bool:
        return (self.root / "edge_events.pb.zst").is_file()

    @property
    def has_center_output(self) -> bool:
        return (self.root / "center_output.pb.zst").is_file()

    @property
    def has_voxels(self) -> bool:
        return (self.root / "voxels.pb.zst").is_file()

    @property
    def has_scores(self) -> bool:
        return (self.root / "scores.pb.zst").is_file()

    def verify_matches_scene(self, scene: TestScenePackage) -> None:
        if self.test_scene_sha256 != scene.content_sha256:
            raise ValueError(
                "Run Output test_scene_sha256 does not match Test Scene content_sha256: "
                f"{self.test_scene_sha256} != {scene.content_sha256}"
            )

    def iter_edge_events(self) -> Iterator[online_pb2.BusMessage]:
        path = _resolve_package_file(self.root, "edge_events.pb.zst")
        return iter_pb_zst(path, online_pb2.BusMessage)

    def iter_center_output(self) -> Iterator[online_pb2.BusMessage]:
        path = _resolve_package_file(self.root, "center_output.pb.zst")
        return iter_pb_zst(path, online_pb2.BusMessage)

    def iter_voxels(self) -> Iterator[online_pb2.SparseVoxelGrid]:
        path = _resolve_package_file(self.root, "voxels.pb.zst")
        return iter_pb_zst(path, online_pb2.SparseVoxelGrid)

    def iter_scores(self) -> Iterator[truth_pb2.ScoreEvent]:
        path = _resolve_package_file(self.root, "scores.pb.zst")
        return iter_pb_zst(path, truth_pb2.ScoreEvent)

    def write_scores(
        self,
        scores: Sequence[truth_pb2.ScoreEvent],
        report: Mapping[str, Any],
        *,
        output_dir: Path | None = None,
    ) -> tuple[Path, Path]:
        dest = self.root if output_dir is None else output_dir
        return persist_scores_output(dest, scores, report)
