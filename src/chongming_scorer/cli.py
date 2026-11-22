"""CLI entry point for the independent chongming-scorer test-framework package."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from chongming_scorer.scorer import score_run
from chongming_scorer.storage import RunOutputPackage, TestScenePackage


def _verify_scene(scene_dir: Path, *, verify_videos: bool) -> dict[str, Any]:
    scene = TestScenePackage.open(scene_dir, verify_hashes=True, verify_videos=verify_videos)
    frame_count = sum(1 for _ in scene.iter_frame_index())
    truth_count = sum(1 for _ in scene.iter_truth_frames())
    ray_count = sum(1 for _ in scene.iter_virtual_rays())
    return {
        "status": "valid",
        "scenario_id": scene.scenario_id,
        "content_sha256": scene.content_sha256,
        "station_count": len(scene.station_configs()),
        "frame_index_entries": frame_count,
        "truth_frames": truth_count,
        "virtual_rays": ray_count,
        "videos_verified": verify_videos,
    }


def _inspect_run(run_dir: Path, *, verify_hashes: bool) -> dict[str, Any]:
    run = RunOutputPackage.open(run_dir, verify_hashes=verify_hashes)
    edge_count = sum(1 for _ in run.iter_edge_events()) if run.has_edge_events else 0
    center_count = sum(1 for _ in run.iter_center_output()) if run.has_center_output else 0
    voxel_count = sum(1 for _ in run.iter_voxels()) if run.has_voxels else 0
    score_count = sum(1 for _ in run.iter_scores()) if run.has_scores else 0
    return {
        "run_id": run.run_id,
        "mode": run.mode,
        "test_scene_sha256": run.test_scene_sha256,
        "edge_messages": edge_count,
        "center_messages": center_count,
        "voxel_grids": voxel_count,
        "score_events": score_count,
        "files": run.manifest["files"],
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="chongming-scorer", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    score_cmd = sub.add_parser("score", help="Score a Run Output against an immutable Test Scene")
    score_cmd.add_argument("--scene", required=True, type=Path)
    score_cmd.add_argument("--run", required=True, type=Path)
    score_cmd.add_argument(
        "--output",
        type=Path,
        help="Optional directory for scores.pb.zst and report.json (defaults to --run)",
    )
    score_cmd.add_argument("--mode", choices=("edge", "center", "integrated"))
    score_cmd.add_argument("--gate-px", type=float, default=10.0)
    score_cmd.add_argument("--size-k-px", type=float, default=0.5)
    score_cmd.add_argument("--fixed-gate-px", action="store_true")
    score_cmd.add_argument("--gate-m", type=float, default=10.0)
    score_cmd.add_argument("--size-k-m", type=float)
    score_cmd.add_argument("--miss-k", type=float, default=3.0)
    score_cmd.add_argument("--warmup-s", type=float)
    score_cmd.add_argument("--warmup-origin-ns", type=int)
    score_cmd.add_argument("--minimum-stations", type=int, default=2)
    score_cmd.add_argument("--no-verify-hashes", action="store_true")

    verify_cmd = sub.add_parser(
        "verify-scene", help="Verify an immutable Test Scene package integrity"
    )
    verify_cmd.add_argument("scene", type=Path)
    verify_cmd.add_argument("--verify-videos", action="store_true")

    inspect_cmd = sub.add_parser("inspect-run", help="Inspect a Run Output package")
    inspect_cmd.add_argument("run", type=Path)
    inspect_cmd.add_argument("--no-verify-hashes", action="store_true")

    args = parser.parse_args(argv)
    if args.command == "verify-scene":
        print(json.dumps(_verify_scene(args.scene, verify_videos=args.verify_videos), indent=2))
        return
    if args.command == "inspect-run":
        print(json.dumps(_inspect_run(args.run, verify_hashes=not args.no_verify_hashes), indent=2))
        return
    report = score_run(
        args.scene,
        args.run,
        output_dir=args.output,
        mode=args.mode,
        gate_px=args.gate_px,
        size_k_px=None if args.fixed_gate_px else args.size_k_px,
        gate_m=args.gate_m,
        size_k_m=args.size_k_m,
        miss_k=args.miss_k,
        warmup_s=args.warmup_s,
        warmup_origin_ns=args.warmup_origin_ns,
        minimum_stations=args.minimum_stations,
        verify_hashes=not args.no_verify_hashes,
    )
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
