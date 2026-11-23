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

- `VIDEO_CODEC_H264`: default for recorded `Test Scene` packages using the D3b `yuv420p` preset (`d3b-yuv420p-crf18-max6m-v1`: `libx264`, `-pix_fmt yuv420p`, `-preset medium`, `-crf 18`, `-maxrate 6M`, `-bufsize 12M`, `-movflags +faststart`). On the 30 s Hannover 3-station 1080p30 benchmark, this reduces main-camera video bitrate from `19.41 Mbps` (`218.34 MB`, `crf-8-yuv444p`) to `5.36 Mbps` (`60.35 MB`) and cuts H.264 decode latency roughly 3x while preserving small-target recall (`100.00%` on `[8, 16) px`, `95.99%` on `extreme-far-small`) and keeping detector F1 within `1.93 pp` (`frame-difference-occupancy-v1`) / `0.02 pp` (`mog2-workbench-v1`). Lower bitrates (`crf 23+`, `<= 2.4 Mbps`) degrade distant targets and trigger compression-artifact false positives on frame-difference detectors. Test Scene generators allow overriding this preset via `manifest.json` for long-range small-target scenes.
- `VIDEO_CODEC_RAW`: supported for Godot Producer -> V-Switch recording and lossless debugging.

Edge abstracts a `FrameSource` interface:

- Real hardware (target: Raspberry Pi / Linux SBC with MIPI-CSI main and DVP/USB auxiliary cameras) reads local camera frames via V4L2 / libcamera.
- Virtual testing receives `FramedVideoPacket` from V-Switch and decodes `H264` when needed.
- Per D7, input pixel SHA-256 hashes are computed on the **decoded frame**.
- Per D3b, Edge reports `decode_duration_ns` and `cv_duration_ns` separately in `DetectionBatch` so test-scene decode overhead is never conflated with local MIPI/DVP algorithm latency.

### Display push (`V-Switch -> Godot Viewer`)

`V-Switch -> Godot Viewer` uses a separate, lightweight, **non-persistent** TCP push channel carrying UTF-8 JSON display frames (`schema_version: "chongming-viewer-v1"`).

#### Transport, framing, and lifecycle rules

- **Framing**: TCP stream of **4-byte big-endian (`uint32` BE)** length-prefixed UTF-8 JSON frames (`2 <= length <= 64 MiB`):

  ```text
  +-----------------------+-----------------------------------+
  | uint32 length (BE)    | UTF-8 JSON DisplayFrame bytes     |
  +-----------------------+-----------------------------------+
  ```

  > **Migration note (Issue #57):** The legacy synchronous Python–Godot renderer ([godot.py](../src/chongming/rendering/godot.py) `_REQUEST_HEADER = struct.Struct("<I")` and [worker.gd](../src/chongming/rendering/godot/worker.gd) `_buffer.decode_u32(0)`) currently reads a 4-byte **little-endian** length prefix. Issue #57 must update Godot Viewer's passive display-push reader in `worker.gd` to **big-endian (`uint32` BE)** to match this frozen wire contract and the rest of the Issue #49 bus.

- **Non-persistence rule**: Display frames pushed from V-Switch to Godot Viewer are strictly ephemeral and **must never be persisted to disk** by V-Switch or Godot Viewer. This prevents duplicate serialization and archive bloat.
- **Weak dependency and drop-on-lag**: Godot Viewer is an optional subscriber. If no viewer is connected, or if a viewer disconnects or lags (slow read / full TCP send buffer), V-Switch drops display updates (keeping at most the latest state or dropping on backpressure) and **never blocks** `Edge -> V-Switch -> Center`, lock-step gating, or `.pb.zst` recording.
- **No scoring in Godot**: Godot Viewer renders pushed overlays (`truth`, `rays`, `answer`, `score`, optional `sparse_voxel_grid`) only. It does not compute scores, run motion/fusion, or write run outputs.
- **Real-hardware / field-trial mode (`mode: "live"`)**: `has_truth` MUST be `false`, and top-level `truth` and `score` keys MUST be **omitted (absent)** from the JSON object.
- **64-bit integer encoding rule**: GDScript's `JSON.parse_string()` parses JSON numbers as IEEE-754 `float64` (exact only up to `2^53`). Therefore, **every `int64` and `uint64` field** — all nanosecond timestamps (`experiment_time_ns`, `capture_time_ns`, `exposure_start_ns`, `exposure_end_ns`, `reference_time_ns`, `event_time_ns`, `window_start_ns`, `window_end_ns`), all sequence/update/step counters (`update_id`, `sequence_no`, `step_index`, `EventId.sequence_no`, `CalibrationState.state_version`), and all 64-bit counts (`matched_count`, `unmatched_count`, `unevaluable_count`) — MUST be encoded as **base-10 decimal strings** (e.g., `"1763784000123456789"`), never JSON numbers. Readers validate integer strings and reject floats or out-of-range values.

#### Top-level `DisplayFrame` JSON envelope

| Field                | JSON type           | Presence / Rule                                                                                                                 | Description                                                                |
| -------------------- | ------------------- | ------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------- |
| `schema_version`     | `string`            | Required                                                                                                                        | Fixed `"chongming-viewer-v1"`                                              |
| `run_id`             | `string`            | Required                                                                                                                        | Run identifier (`SessionAck.run_id` / `run_manifest.json["run_id"]`)       |
| `mode`               | `string`            | Required                                                                                                                        | `"edge"`, `"center"`, `"integrated"`, or `"live"`                          |
| `update_id`          | `string` (`uint64`) | Required                                                                                                                        | Monotonic logical display update ID (`>= "1"`) encoded as base-10 string   |
| `sequence_no`        | `string` (`uint64`) | Required                                                                                                                        | Monotonic viewer-stream push counter (`>= "1"`) to detect drop-on-lag gaps |
| `experiment_time_ns` | `string` (`int64`)  | Required                                                                                                                        | Current experiment/capture timestamp in ns encoded as base-10 string       |
| `has_truth`          | `boolean`           | Required                                                                                                                        | `true` when Test Scene truth is active; `false` in `"live"` mode           |
| `stations`           | `array[object]`     | Required                                                                                                                        | Array of `StationDisplayState` (`[]` if none registered yet)               |
| `truth`              | `object`            | Present iff `has_truth == true` and truth is available for `experiment_time_ns`; **omitted (absent)** when `has_truth == false` | `TruthOverlay` mapped from `TruthFrame`                                    |
| `rays`               | `object`            | Required                                                                                                                        | `RaysOverlay` (`items`, `calibration_states`, `diagnostics`)               |
| `answer`             | `object`            | Required                                                                                                                        | `AnswerOverlay` (`measurements`, `tracks`)                                 |
| `score`              | `object`            | Present iff `has_truth == true` and Scorer has emitted `ScoreEvent`(s); **omitted (absent)** when `has_truth == false`          | `ScoreOverlay` mapped from `ScoreEvent`                                    |
| `sparse_voxel_grid`  | `object`            | Optional; **omitted (absent)** unless Center exported a `SparseVoxelGrid`                                                       | `SparseVoxelGridOverlay` mapped from `SparseVoxelGrid`                     |

#### Nested object schemas and Protobuf mappings

Geometry vectors use JSON arrays of finite `number`s (`[x, y]` for `Vec2`, `[x, y, z]` for `Vec3`, and `[[r00, r01, r02], [r10, r11, r12], [r20, r21, r22]]` row-major for 3x3 rotations). Protobuf enums map to lowercase snake-case strings without the enum prefix:

- `CalibrationHealth`: `"initializing" | "valid" | "frozen" | "stale" | "recovering" | "invalid"`
- `CalibrationUsage`: `"valid" | "frozen" | "stale"`
- `OcclusionStatus`: `"visible" | "occluded" | "out_of_fov" | "unknown"`
- `FusionStatus`: `"located" | "geometry_degenerate" | "association_ambiguous" | "insufficient_stations" | "no_candidate_in_volume" | "rejected_input"`
- `TrackLifecycle`: `"tentative" | "confirmed" | "coasting" | "deleted"`

1. **`stations[]` (`StationDisplayState`, from `StationConfig` + latest `CalibrationStateEvent`)**:
   - `station_id` (`string`, required)
   - `main_camera_position_enu_m` (`[number, number, number]`, required)
   - `baseline_orientation_enu_from_main` (`number[3][3]`, required)
   - `calibrated_orientation_enu_from_main` (`number[3][3] | null`, from `CalibrationSolution.rotation_enu_from_main`)
   - `calibration_health` (`string | null`, `CalibrationHealth`)
   - `calibration_usage` (`string | null`, `CalibrationUsage`)
   - `state_version` (`string | null`, `uint64` decimal string from `CalibrationState.state_version`)
   - `solution_id` (`string | null`, from `CalibrationSolutionRef.solution_id`)

2. **`truth` (`TruthOverlay`, from `truth.proto` `TruthFrame`; omitted when `has_truth == false`)**:
   - `world_frame_id` (`string`, required)
   - `experiment_time_ns` (`string`, required, `int64` decimal string)
   - `step_index` (`string`, required, `uint64` decimal string)
   - `targets` (`array[TargetTruthItem]`, required):
     - `target_id` (`string`, required)
     - `target_class` (`string`, required)
     - `position_enu_m` (`[number, number, number]`, required)
     - `velocity_enu_mps` (`[number, number, number]`, required)
     - `bounding_radius_m` (`number`, required, `>= 0`)
     - `body_aabb_diagonal_m` (`number | null`, required)
   - `projections` (`array[StationTargetProjectionItem]`, required):
     - `station_id` (`string`, required)
     - `target_id` (`string`, required)
     - `occlusion_status` (`string`, required, `OcclusionStatus`)
     - `projected_centroid_px` (`[number, number] | null`, required)
     - `projected_diagonal_px` (`number | null`, required)
     - `range_m` (`number | null`, required)
     - `frame_id` (`string | null`, required)

3. **`rays` (`RaysOverlay`, from `online.proto` `RayEvent`, `CalibrationStateEvent`, `DiagnosticEvent`)**:
   - `items` (`array[RayItem]`, required):
     - `event_id` (`[string, string, string]`, required: `[station_id, station_session_id, sequence_no_u64_str]`)
     - `frame_id` (`string`, required)
     - `capture_time_ns` (`string`, required, `int64` decimal string midpoint `(exposure_start_ns + exposure_end_ns) // 2`)
     - `exposure_start_ns` (`string`, required, `int64` decimal string)
     - `exposure_end_ns` (`string`, required, `int64` decimal string)
     - `origin_enu_m` (`[number, number, number]`, required, from `ray_origin_enu_m`)
     - `direction_enu_unit` (`[number, number, number]`, required, from `ray_direction_enu_unit`)
     - `calibration_usage` (`string`, required, `CalibrationUsage`)
     - `detection_score` (`number`, required)
     - `angular_std_rad` (`number | null`, required: spectral norm / major std of `uncertainty.independent_angular_covariance_rad2`)
     - `local_track_id` (`string | null`, required)
     - `quality_flags` (`array[string]`, required)
   - `calibration_states` (`array[CalibrationStateItem]`, required):
     - `event_id` (`[string, string, string]`, required)
     - `station_id` (`string`, required)
     - `state_version` (`string`, required, `uint64` decimal string)
     - `health` (`string`, required, `CalibrationHealth`)
     - `solution_id` (`string | null`, required)
     - `observed_at_ns` (`string`, required, `int64` decimal string)
     - `reason_codes` (`array[string]`, required)
   - `diagnostics` (`array[DiagnosticItem]`, required):
     - `event_id` (`[string, string, string]`, required)
     - `station_id` (`string`, required)
     - `capture_time_ns` (`string | null`, required, `int64` decimal string midpoint if `capture_time` is present)
     - `category` (`string`, required)
     - `reason_codes` (`array[string]`, required)

4. **`answer` (`AnswerOverlay`, from `online.proto` `FusionMeasurement` and `TrackEvent`)**:
   - `measurements` (`array[MeasurementItem]`, required):
     - `center_session_id` (`string`, required)
     - `measurement_id` (`string`, required)
     - `reference_time_ns` (`string`, required, `int64` decimal string)
     - `support_exposure_span_ns` (`string`, required, `int64` decimal string)
     - `status` (`string`, required, `FusionStatus`)
     - `position_enu_m` (`[number, number, number] | null`, required; `null` when `status != "located"`)
     - `position_std_enu_m` (`[number, number, number] | null`, required; square root of diagonal of `conditional_position_covariance_enu_m2`, `null` when `status != "located"` or covariance absent)
     - `supporting_ray_event_ids` (`array[[string, string, string]]`, required)
     - `supporting_station_ids` (`array[string]`, required)
     - `minimum_intersection_angle_deg` (`number | null`, required; converted from `minimum_intersection_angle_rad`)
     - `geometry_condition` (`number | null`, required)
     - `reason_codes` (`array[string]`, required)
   - `tracks` (`array[TrackItem]`, required):
     - `center_session_id` (`string`, required)
     - `track_id` (`string`, required)
     - `event_time_ns` (`string`, required, `int64` decimal string)
     - `lifecycle` (`string`, required, `TrackLifecycle`)
     - `position_enu_m` (`[number, number, number] | null`, required)
     - `velocity_enu_mps` (`[number, number, number] | null`, required)
     - `position_std_enu_m` (`[number, number, number] | null`, required; square root of diagonal `[0..2]` of `conditional_state_covariance`)
     - `velocity_std_enu_mps` (`[number, number, number] | null`, required; square root of diagonal `[3..5]` of `conditional_state_covariance`)
     - `source_measurement_ids` (`array[string]`, required, from `source_measurement_refs[].measurement_id`)
     - `reason_codes` (`array[string]`, required)

5. **`score` (`ScoreOverlay`, from `truth.proto` `ScoreEvent`; omitted when `has_truth == false` or before any `ScoreEvent` arrives)**:
   - `events` (`array[ScoreEventItem]`, required, non-empty when `score` key is present; integrated runs may carry both `"px"` and `"m"` items):
     - `run_id` (`string`, required)
     - `experiment_time_ns` (`string`, required, `int64` decimal string)
     - `matched_count` (`string`, required, `uint64` decimal string)
     - `unmatched_count` (`string`, required, `uint64` decimal string)
     - `unevaluable_count` (`string`, required, `uint64` decimal string)
     - `mean_error` (`number | null`, required)
     - `rmse` (`number | null`, required)
     - `max_error` (`number | null`, required)
     - `error_unit` (`string`, required: `"px"` or `"m"`)
     - `extra_metrics` (`array[{"name": string, "value": number | null, "unit": string}]`, required)

6. **`sparse_voxel_grid` (`SparseVoxelGridOverlay`, from `online.proto` `SparseVoxelGrid`; omitted unless exported)**:
   - `center_session_id` (`string`, required)
   - `world_frame_id` (`string`, required)
   - `window_start_ns` (`string`, required, `int64` decimal string)
   - `window_end_ns` (`string`, required, `int64` decimal string)
   - `min_enu_m` (`[number, number, number]`, required)
   - `step_enu_m` (`[number, number, number]`, required)
   - `max_score` (`integer`, required: `max(station_votes)`, fits in `uint32`)
   - `cells` (`array[{"ix": integer, "iy": integer, "iz": integer, "station_votes": integer}]`, required)

#### Example `DisplayFrame` JSON (benchmark run with truth & score)

```json
{
  "schema_version": "chongming-viewer-v1",
  "run_id": "run-01",
  "mode": "integrated",
  "update_id": "42",
  "sequence_no": "42",
  "experiment_time_ns": "1763784000123456789",
  "has_truth": true,
  "stations": [
    {
      "station_id": "station-west",
      "main_camera_position_enu_m": [-250.0, 0.0, 12.0],
      "baseline_orientation_enu_from_main": [
        [1.0, 0.0, 0.0],
        [0.0, 1.0, 0.0],
        [0.0, 0.0, 1.0]
      ],
      "calibrated_orientation_enu_from_main": [
        [1.0, 0.0, 0.0],
        [0.0, 1.0, 0.0],
        [0.0, 0.0, 1.0]
      ],
      "calibration_health": "valid",
      "calibration_usage": "valid",
      "state_version": "3",
      "solution_id": "sol-3"
    }
  ],
  "truth": {
    "world_frame_id": "enu",
    "experiment_time_ns": "1763784000123456789",
    "step_index": "42",
    "targets": [
      {
        "target_id": "ultra-far-aircraft",
        "target_class": "f86",
        "position_enu_m": [120.5, 840.0, 210.0],
        "velocity_enu_mps": [18.0, -4.0, 0.5],
        "bounding_radius_m": 6.5,
        "body_aabb_diagonal_m": 13.0
      }
    ],
    "projections": [
      {
        "station_id": "station-west",
        "target_id": "ultra-far-aircraft",
        "occlusion_status": "visible",
        "projected_centroid_px": [964.2, 412.8],
        "projected_diagonal_px": 14.3,
        "range_m": 939.4,
        "frame_id": "station-west-main-0042"
      }
    ]
  },
  "rays": {
    "items": [
      {
        "event_id": ["station-west", "sess-w1", "105"],
        "frame_id": "station-west-main-0042",
        "capture_time_ns": "1763784000123456789",
        "exposure_start_ns": "1763784000123456789",
        "exposure_end_ns": "1763784000123456789",
        "origin_enu_m": [-250.0, 0.0, 12.0],
        "direction_enu_unit": [0.394, 0.894, 0.211],
        "calibration_usage": "valid",
        "detection_score": 0.88,
        "angular_std_rad": 0.0012,
        "local_track_id": "1",
        "quality_flags": []
      }
    ],
    "calibration_states": [],
    "diagnostics": []
  },
  "answer": {
    "measurements": [
      {
        "center_session_id": "sess-c1",
        "measurement_id": "m-42",
        "reference_time_ns": "1763784000123456789",
        "support_exposure_span_ns": "0",
        "status": "located",
        "position_enu_m": [121.1, 839.2, 209.6],
        "position_std_enu_m": [1.8, 2.4, 1.5],
        "supporting_ray_event_ids": [["station-west", "sess-w1", "105"]],
        "supporting_station_ids": ["station-west", "station-east"],
        "minimum_intersection_angle_deg": 28.4,
        "geometry_condition": 2.1,
        "reason_codes": []
      }
    ],
    "tracks": []
  },
  "score": {
    "events": [
      {
        "run_id": "run-01",
        "experiment_time_ns": "1763784000123456789",
        "matched_count": "1",
        "unmatched_count": "0",
        "unevaluable_count": "0",
        "mean_error": 1.077,
        "rmse": 1.077,
        "max_error": 1.077,
        "error_unit": "m",
        "extra_metrics": [
          { "name": "missed_targets", "value": 0.0, "unit": "count" }
        ]
      }
    ]
  }
}
```

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
- `encoding_preset`: optional scene-wide H.264 encoding preset object used when generating or re-encoding tracks (defaults to D3b `d3b-yuv420p-crf18-max6m-v1` when omitted on converted legacy scenes):
  ```json
  {
    "preset_id": "d3b-yuv420p-crf18-max6m-v1",
    "codec": "libx264",
    "container": "mp4",
    "encoded_pixel_format": "yuv420p",
    "x264_preset": "medium",
    "crf": 18,
    "maxrate_kbps": 6000,
    "bufsize_kbps": 12000,
    "target_bitrate_kbps": null
  }
  ```
  Future high-quality or long-range small-target scenes can override `crf`, `encoded_pixel_format` (e.g., `"yuv444p"`), `x264_preset`, `maxrate_kbps`, `bufsize_kbps`, or `target_bitrate_kbps` here or per track.
- `video`: per-track codec/dimensions metadata keyed by relative path (`video/<station>-<role>.mp4`), including `station_id`, `camera_role`, `codec`, `container`, `fps`, `frame_count`, `width_px`, `height_px`, `pixel_format` (pre-encode source format such as `"rgb8"`), `encoded_pixel_format` (e.g., `"yuv420p"`), `quality` label, optional per-track `encoding_preset` override object, and measured `average_bitrate_kbps`.
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
