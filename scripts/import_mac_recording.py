"""Import a single Mac camera recording as an image-only Test Scene."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from fractions import Fraction
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from rtk_calibrate import load_intrinsics_json

from chongming_proto.online import common_pb2, online_pb2
from chongming_proto.truth import truth_pb2
from chongming_scorer.storage import TestSceneWriter, sha256_file


def read_timestamps(path: Path, width: int, height: int) -> tuple[str, list[int]]:
    required = {
        "station_id",
        "output_frame_index",
        "monotonic_ns",
        "actual_width_px",
        "actual_height_px",
    }
    station = ""
    times: list[int] = []
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = reader.fieldnames or []
        if not required.issubset(fields) or len(fields) != len(set(fields)):
            raise ValueError("Expected a recording timestamp CSV with unique column names")
        for index, row in enumerate(reader):
            try:
                if int(row["output_frame_index"]) != index:
                    raise ValueError("output_frame_index must be consecutive starting at zero")
                if (int(row["actual_width_px"]), int(row["actual_height_px"])) != (width, height):
                    raise ValueError("recorded frame dimensions do not match the camera parameters")
                stamp = int(row["monotonic_ns"])
                if not 0 <= stamp < 2**63 or (times and stamp <= times[-1]):
                    raise ValueError("monotonic_ns must be a strictly increasing nonnegative int64")
                if index == 0:
                    station = row["station_id"]
                    if not re.fullmatch(r"[A-Za-z0-9_-]+", station):
                        raise ValueError("invalid station_id")
                elif row["station_id"] != station:
                    raise ValueError("timestamp CSV must contain exactly one station")
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{path}:{index + 2}: {exc}") from exc
            times.append(stamp)
    if not times:
        raise ValueError("timestamp CSV contains no frames")
    return station, [stamp - times[0] for stamp in times]


def run_media(command: list[str]) -> str:
    result = subprocess.run(command, check=False, text=True, capture_output=True)
    if result.returncode:
        raise ValueError(f"{Path(command[0]).name} failed: {result.stderr.strip()}")
    return result.stdout


def import_recording(
    video: Path, timestamps: Path, intrinsics_path: Path, output: Path
) -> dict[str, Any]:
    if output.exists() or output.is_symlink():
        raise FileExistsError(f"Test Scene output already exists: {output}")
    if not video.is_file():
        raise FileNotFoundError(f"Recording not found: {video}")
    intrinsics = load_intrinsics_json(intrinsics_path)
    width, height = intrinsics.image_width_px, intrinsics.image_height_px
    station_id, times = read_timestamps(timestamps, width, height)
    timestamp_digest = sha256_file(timestamps)
    session_id = f"{station_id}-{timestamp_digest[:16]}"
    clock_domain = f"{session_id}-host-receipt"
    world_frame = "image-only"
    distortion = intrinsics.distortion_model.lower().replace("-", "_")
    camera = online_pb2.CameraModel(
        intrinsics=online_pb2.CameraIntrinsics(
            image_width_px=width,
            image_height_px=height,
            matrix_px=common_pb2.Mat3(values=intrinsics.matrix_px.ravel().tolist()),
        ),
        distortion=online_pb2.CameraDistortion(
            model="none" if distortion in {"none", "pinhole", "zero"} else "brown_conrady",
            coefficients=intrinsics.distortion_coefficients,
        ),
    )
    config = online_pb2.StationConfig(
        station_id=station_id, config_version="1", world_frame_id=world_frame, main_camera=camera
    )
    config.config_digest = hashlib.sha256(config.SerializeToString(deterministic=True)).hexdigest()
    ffmpeg = shutil.which(os.environ.get("CHONGMING_FFMPEG") or "ffmpeg")
    if ffmpeg is None:
        raise FileNotFoundError("FFmpeg is unavailable. Enter the repository Nix environment.")
    ffprobe = Path(ffmpeg).with_name("ffprobe")
    relpath = f"video/{station_id}-main.mp4"

    with TemporaryDirectory(prefix="chongming-mac-import-") as directory:
        encoded = Path(directory) / "main.mp4"
        run_media(
            [
                ffmpeg,
                "-nostdin",
                "-v",
                "error",
                "-xerror",
                "-noautorotate",
                "-i",
                str(video.resolve()),
                "-map",
                "0:v:0",
                "-fps_mode",
                "passthrough",
                "-c:v",
                "libx264",
                "-crf",
                "18",
                "-bf",
                "0",
                "-pix_fmt",
                "yuv420p",
                str(encoded),
            ]
        )
        streams = json.loads(
            run_media(
                [
                    str(ffprobe),
                    "-v",
                    "error",
                    "-select_streams",
                    "v:0",
                    "-count_frames",
                    "-show_entries",
                    "stream=width,height,avg_frame_rate,nb_read_frames",
                    "-of",
                    "json",
                    str(encoded),
                ]
            )
        )["streams"]
        if not streams:
            raise ValueError("video does not contain a decodable video track")
        stream = streams[0]
        if (stream["width"], stream["height"]) != (width, height):
            raise ValueError("video dimensions do not match the camera parameters")
        frame_count = int(stream.get("nb_read_frames", 0))
        if frame_count != len(times):
            raise ValueError(
                f"video has {frame_count} frames but timestamp CSV has {len(times)} rows"
            )

        with TestSceneWriter(
            output,
            scenario_id=output.name,
            world_frame_id=world_frame,
            clock_domain_id=clock_domain,
            start_time_ns=times[0],
            end_time_ns=times[-1],
            stations=[config],
            occlusion_mode="unknown",
        ) as writer:
            writer.add_video_file(
                encoded,
                relpath,
                {
                    "station_id": station_id,
                    "camera_role": "main",
                    "codec": "h264",
                    "container": "mp4",
                    "fps": float(Fraction(stream["avg_frame_rate"])),
                    "frame_count": frame_count,
                    "width_px": width,
                    "height_px": height,
                    "encoded_pixel_format": "yuv420p",
                    "timestamp_source": "host_receipt_monotonic_ns",
                    "source_video_sha256": sha256_file(video),
                    "source_timestamps_sha256": timestamp_digest,
                    "source_intrinsics_sha256": sha256_file(intrinsics_path),
                },
            )
            for index, time_ns in enumerate(times):
                writer.add_frame_index(
                    truth_pb2.FrameIndexEntry(
                        station_id=station_id,
                        station_session_id=session_id,
                        camera_role=common_pb2.CAMERA_ROLE_MAIN,
                        frame_index=index,
                        frame_id=f"{session_id}:{index}",
                        station_config_digest=config.config_digest,
                        capture_time=common_pb2.CaptureTime(
                            clock_domain_id=clock_domain,
                            exposure_start_ns=time_ns,
                            exposure_end_ns=time_ns,
                            rolling_shutter_model="unknown",
                        ),
                        video_relpath=relpath,
                        width_px=width,
                        height_px=height,
                    )
                )
            return writer.finalize()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video", type=Path, required=True)
    parser.add_argument("--timestamps", type=Path, required=True)
    parser.add_argument("--intrinsics", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        scene = import_recording(args.video, args.timestamps, args.intrinsics, args.output)
    except (OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    track = next(iter(scene["video"].values()))
    print(f"Imported {track['frame_count']} frames for {track['station_id']} into {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
