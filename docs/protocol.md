# Wire Protocol and Storage Specification (Issue #49)

> Status: **Stage 1 complete** for the Issue #49 modularization (`chongming_proto` schema bindings + `.pb.zst` storage + independent `chongming_scorer` package). The currently active synchronous Python–Godot runtime is still documented in [Design](design.md).

## Scope

This document defines the cross-process wire protocol, storage layout, and display-push boundary for the Issue #49 target architecture:

- **Business processes**: `Edge` and `Center` (deployable on real hardware).
- **Hub and adapter**: `V-Switch` (Rust), handling recording, playback, forwarding, lock-step gating, and display fan-out.
- **Test framework**: `Godot Producer`, `Scorer` (independent Python package), `Godot Viewer`, immutable `Test Scene` packages, and per-run `Run Output` packages.

## Schema separation and dependency rules

Shared data contracts live in [proto/chongming/v1/](../proto/chongming/v1/):

| File           | Contents                                                                                                                               | Allowed consumers                              |
| -------------- | -------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------- |
| `common.proto` | Geometry primitives, 64-bit ns timestamps, enums, envelopes                                                                            | All components                                 |
| `online.proto` | `StationConfig`, `FramedVideoPacket`, `RayEvent`, `FusionMeasurement`, `TrackEvent`, `ProgressReport`, `SparseVoxelGrid`, `BusMessage` | Edge, Center, V-Switch, Scorer, Godot Producer |
| `truth.proto`  | `FrameIndexEntry`, `TruthFrame`, `TargetTruth`, `StationTargetProjection`, `ScoreEvent`, `TestFrameworkMessage`                        | Godot Producer, V-Switch, Scorer               |

Rules:

1. Business packages (`Edge`, `Center`, `chongming`) must never import or generate code from `truth.proto` (`chongming_proto.truth`).
2. `Scorer` (`chongming_scorer`) must never import business algorithm packages (`chongming`). It depends only on generated schema bindings (`chongming_proto`) and test-framework utilities. Architecture tests enforce both rules.
3. All timestamps and truth coordinates use 64-bit scalars (`int64` nanoseconds, `double` ENU meters). Godot Producer computes truth and virtual rays in 64-bit scalars and projects to 32-bit scene nodes for rendering only.
4. `OcclusionStatus` in-FOV invariant: `OCCLUSION_STATUS_OUT_OF_FOV` means the target center is outside the camera FOV; `OCCLUSION_STATUS_VISIBLE`, `OCCLUSION_STATUS_OCCLUDED`, and `OCCLUSION_STATUS_UNKNOWN` all mean the target center is inside the camera FOV. `TargetTruth.body_aabb_diagonal_m` stores the physical body-space model AABB diagonal in meters for size-aware center metrics and range-to-size difficulty strata. `StationTargetProjection.frame_id` links each projection directly to `FrameIndexEntry.frame_id` and `DetectionBatch.main_frame_ref.frame_id`.

## Wire transport and framing

### Online bus (`V-Switch <-> Edge / Center / Scorer / Producer`)

All structured cross-process links use **TCP** with **4-byte big-endian unsigned length-prefixed Protobuf frames**:

```text
+-----------------------+-----------------------------------+
| uint32 length (BE)    | serialized Protobuf message bytes |
+-----------------------+-----------------------------------+
```

- Maximum single frame payload: `64 MiB` (to allow optional raw `RAW` frames during debugging).
- Connection handshake: client sends `SessionHello` (`role`, `instance_id`, `session_id`, `station_ids`); V-Switch replies with `SessionAck`.
- Business flow is **unidirectional**: `Edge -> V-Switch -> Center`. V-Switch forwards `RayEvent`, `CalibrationStateEvent`, `DiagnosticEvent`, and `ProgressReport` verbatim.
- `ProgressReport` is part of the normal online protocol:
  - In **wall-clock / real hardware mode**, V-Switch streams frames or forwards live events without waiting.
  - In **lock-step benchmark mode**, V-Switch waits for the expected `ProgressReport` from Edge (and Center in integrated runs) before releasing the next exposure group.
- Business timeouts (such as station idle/offline detection in Center) must use experiment/capture time (`CaptureTime`), or support disabling wall-clock timeouts in lock-step mode.

### Video packets (`V-Switch -> Edge` and `Producer -> V-Switch`)

`FramedVideoPacket` carries exact per-frame metadata (`station_id`, `camera_role`, `frame_id`, `frame_index`, `capture_time`, `codec`, `pixel_format`, `is_keyframe`) alongside `payload`:

- `VIDEO_CODEC_H264`: default for recorded Test Scenes (lower-bitrate `yuv420p` configurable profile).
- `VIDEO_CODEC_RAW`: supported for Godot Producer -> V-Switch recording and lossless debugging.

Edge abstracts a `FrameSource` interface:

- Real hardware (target: Raspberry Pi / Linux SBC with MIPI-CSI main and DVP/USB auxiliary cameras) reads local camera frames via V4L2 / libcamera.
- Virtual testing receives `FramedVideoPacket` from V-Switch and decodes `H264` when needed.
- Per D7, input pixel SHA-256 hashes are computed on the **decoded frame**.
- Per D3b, Edge reports `decode_duration_ns` and `cv_duration_ns` separately in `DetectionBatch` so test-scene decode overhead is never conflated with local MIPI/DVP algorithm latency.

### Display push (`V-Switch -> Godot Viewer`)

`V-Switch -> Godot Viewer` uses a separate, lightweight, **non-persistent** push channel:

- **Framing**: TCP with 4-byte big-endian length-prefixed UTF-8 JSON (matching the existing Godot viewer parser, with 64-bit integer timestamps encoded as decimal strings to avoid GDScript JSON float truncation).
- **Non-persistence rule**: display frames pushed from V-Switch to Godot Viewer are strictly ephemeral and **must never be persisted to disk** by V-Switch or Godot Viewer. This prevents duplicate serialization and archive bloat.
- **Weak dependency**: Godot Viewer is an optional subscriber. If no viewer is connected, or if the viewer disconnects or lags, V-Switch drops display updates and never blocks `Edge -> V-Switch -> Center` or recording.
- **No scoring in Godot**: Godot Viewer renders pushed overlays (`Truth`, `Rays`, `Answer`, `Score`, optional `SparseVoxelGrid`) only. It does not compute scores, run motion, or write run outputs.
- In real-hardware / field-trial mode, `Truth` and `Score` fields are absent.

## Storage format (D6)

Storage uses immutable directory packages on the local filesystem. No external database server is required.

### Stream file format (`.pb.zst`)

High-frequency structured streams use **length-prefixed Protobuf messages compressed with Zstandard (`.pb.zst`)**:

```text
zstd frame(s) -> concatenated [ uint32 length (BE) | Protobuf message bytes ]
```

- Readers stream-decompress sequentially without loading the entire file into memory.
- Writers flush periodic zstd frames so partial logs remain recoverable on crash or forced stop.

### Immutable `Test Scene` package (read-only)

Created once by `Godot Producer -> V-Switch`. Once finalized, it is **strictly read-only** and identified by its content hash in `manifest.json`. Runs must never modify or append to a Test Scene directory.

```text
<test-scene-dir>/
  manifest.json            # Static metadata, StationConfig list, seeds, codec params, file SHA-256 hashes
  video/
    <station>-main.mp4     # H.264 video track
    <station>-aux.mp4      # H.264 auxiliary track (if configured)
  frame_index.pb.zst       # Compact stream of FrameIndexEntry: (station, role, frame_index) -> (frame_id, CaptureTime, video_relpath)
  truth.pb.zst             # Sequence of TruthFrame (64-bit scalars; static camera info NOT repeated)
  virtual_rays.pb.zst      # Sequence of RayEvent for center-only replay
```

`manifest.json` (`format: "chongming-test-scene-v1"`) stores static fields once:

- `format`: `"chongming-test-scene-v1"`
- `scenario_id`, `world_frame_id`, `clock_domain_id`
- `start_time_ns`, `end_time_ns`: 64-bit integer experiment time bounds
- `random_seed`: integer seed (if fixed)
- `occlusion_mode`: `"known"` or `"unknown"`
- `stations`: list of Protobuf `StationConfig` JSON objects (`MessageToDict` with `preserving_proto_field_name=True`)
- `targets`: optional static target metadata mapping (`target_id -> {"body_aabb_diagonal_m": float, ...}`)
- `video`: per-track codec/dimensions metadata keyed by relative path (`video/<station>-<role>.mp4`)
- `files`: mapping from package-relative paths (`frame_index.pb.zst`, `truth.pb.zst`, `virtual_rays.pb.zst`, and video tracks) to `{"sha256": "<hex>", "bytes": <int>}`
- `content_sha256`: SHA-256 hex digest of canonical JSON (`sort_keys=True`, compact separators) of the manifest excluding `"content_sha256"`

### Independent `Run Output` package (one directory per run)

Created fresh for every experiment run. It references the source Test Scene by path and manifest hash without copying video or truth.

```text
<run-output-dir>/
  run_manifest.json        # Run ID, mode, policy/code hashes, Test Scene path + SHA-256 digest
  edge_events.pb.zst       # Sequence of BusMessage (DetectionBatch, CalibrationStateEvent, RayEvent, DiagnosticEvent, ProgressReport)
  center_output.pb.zst     # Sequence of BusMessage (FusionMeasurement, TrackEvent, ProgressReport)
  voxels.pb.zst            # Optional: SparseVoxelGrid stream (disabled by default to avoid bloat)
  scores.pb.zst            # Sequence of ScoreEvent written by Scorer
  report.json              # Human-readable summary metrics and attempt status written by Scorer
```

`run_manifest.json` (`format: "chongming-run-output-v1"`) records:

- `format`: `"chongming-run-output-v1"`
- `run_id`: unique run identifier string
- `mode`: `"edge"`, `"center"`, or `"integrated"`
- `test_scene_path`: path to the immutable `Test Scene` directory
- `test_scene_sha256`: expected `manifest.json["content_sha256"]` of the `Test Scene`
- `provenance`: code/runtime/policy metadata object
- `files`: mapping from present stream relative paths (`edge_events.pb.zst`, `center_output.pb.zst`, optional `voxels.pb.zst`) to `{"sha256": "<hex>", "bytes": <int>}`

## Implementation checklist

This specification tracks the Issue #49 stages:

1. Stage 0 (Done): Shared Schema (`.proto`), wire/storage spec, and Nix Rust/`protoc` toolchain.
2. Stage 1 (Done): Independent `chongming_scorer` package and `chongming_proto` bindings reading/writing immutable `Test Scene` and independent `Run Output` packages (`.pb.zst`).
3. Stage 2: Rust `V-Switch` and standalone Python `Edge` / `Center` processes using `online.proto`.
4. Stage 3: Godot Producer full-stack migration and passive Godot Viewer push integration.
5. Stage 4: Rust `Edge` validated against Issue #51 metrics, QEMU functional/cross-compile CI, and real hardware integration.
