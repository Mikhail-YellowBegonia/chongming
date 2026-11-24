# Design

## Scope

Chongming is a research system for detecting, locating, and tracking small airborne targets with fixed daylight camera stations. Online algorithms (`Edge` and `Center`) run in standalone processes over a binary Protobuf bus (`V-Switch`), completely decoupled from scene generation, truth storage, and evaluation.

## Architecture and ownership

```text
Godot Producer (godot_producer/)
  |-- FramedVideoPacket (RAW RGB8) + BusMessage (StationConfig, virtual rays)
  |-- TruthFrame + StationTargetProjection (64-bit)
  v
V-Switch Record (crates/vswitch/)
  --> Immutable Test Scene (.pb.zst + H.264 MP4s)

Test Scene ---> V-Switch Replay (Lock-Step / Wall-Clock) <---> Scorer (chongming-scorer)
                    |      ^          |
         Video/     |      | Bus      | DisplayFrame JSON (chongming-viewer-v1)
       Progress     v      |          v
             chongming-edge     Godot Viewer (passive)
                    |      ^
                    v      |
             chongming-center-bus
```

Process responsibilities:

| Component              | Ownership                                                                                        |
| ---------------------- | ------------------------------------------------------------------------------------------------ |
| `Godot Producer`       | Scene authoring/presets, 64-bit kinematics, truth, physics raycast occlusion, raw RGB8 rendering |
| `V-Switch` (`vswitch`) | TCP hub, `Test Scene` recording, Lock-Step / Wall-Clock replay, live relay, Run Output `.pb.zst` |
| `chongming-edge`       | Auxiliary calibration, main image detection, 2D tracking, tangent-Gaussian ray generation        |
| `chongming-center-bus` | Event admission, capture-clock windows, coarse voxel search, association, GLS geometry, tracking |
| `chongming-scorer`     | Offline and online evaluation (`DetectionMetrics`, `CenterSizeMetrics`, timing, attribution)     |
| `Godot Viewer`         | Passive `chongming-viewer-v1` TCP display client (`--viewer <host:port>`)                        |

Wire protocols and `.pb.zst` package formats are specified in [Wire Protocol and Storage Specification](protocol.md).

## Source layout

`src/` contains three Python packages with strict import boundaries verified by [test_architecture.py](../tests/test_architecture.py):

| Package                 | Responsibility                                                                               | Import boundary                                                 |
| ----------------------- | -------------------------------------------------------------------------------------------- | --------------------------------------------------------------- |
| `src/chongming/`        | Online contracts, ports, algorithms, policy codecs, `bus/` processes, passive viewer project | Must never import `chongming_proto.truth` or `chongming_scorer` |
| `src/chongming_proto/`  | Generated Protobuf bindings (`online` / `truth` split) and `.pb.zst` length-prefixed framing | Depends only on `proto/chongming/v1/` and standard framing      |
| `src/chongming_scorer/` | Independent verifier, scorer, attribution, and `Test Scene` / `Run Output` package storage   | Must never import `chongming`                                   |

Within `src/chongming/`, module responsibilities are:

| Path                   | Responsibility                                                      |
| ---------------------- | ------------------------------------------------------------------- |
| `contracts/`           | Immutable online records, policies, units, and strict JSON identity |
| `ports/`               | Typed behavior interfaces using `Protocol`                          |
| `modules/`             | Online device/fusion algorithms and explicit unimplemented stubs    |
| `composition/`         | Dependency wiring (`declare_station`, `declare_center`)             |
| `bus/`                 | TCP bus client, Protobuf codec, `EdgeProcess`, `CenterProcess`      |
| `reference.py`         | Reference center plugin (`ReferenceFusionAlgorithm`)                |
| `conversion/`          | Visualization contract conversions (`SparseVoxelGrid`)              |
| `rendering/godot/`     | Passive Godot Viewer (`worker.gd`, `ui.gd`, `visualization.gd`)     |
| `uncertainty.py`       | Covariance factorization helpers                                    |
| `voxel_diagnostics.py` | Opt-in raw voxel vote snapshot capture                              |
| `vision/`              | Separate frame-difference workbench and tuning tools                |

External components live in workspace directories alongside `src/`:

| Path              | Responsibility                                                            |
| ----------------- | ------------------------------------------------------------------------- |
| `godot_producer/` | Decoupled Godot 4.7.2 virtual scene generator and `V-Switch` record peer  |
| `crates/vswitch/` | Rust `V-Switch` library and CLI binary                                    |
| `crates/edge/`    | Rust `Edge` library and CLI binary (`online.proto` / `common.proto` only) |
| `proto/`          | Source `.proto` schemas for `common`, `online`, and `truth`               |

Construction does not start business work. `ModuleShell` separates lifecycle state from implementation status (`DirectGeometryCandidateModule` remains an explicit unimplemented stub whose method raises `NotImplementedError`).

## Data contracts

Online records live in [contracts/model.py](../src/chongming/contracts/model.py) using frozen dataclasses, tuples, typed identifiers, and enums.

### Coordinates and time

- World coordinates are right-handed ENU: east, north, up, in meters.
- Camera axes are right, down, forward. Pixel `(0, 0)` is the top-left pixel center.
- `R_A_from_B` maps a vector from frame B to frame A. Ray directions are unit vectors.
- Godot converts ENU `(E, N, U)` to scene `(E, U, -N)`.
- `CaptureTime` stores an exposure interval; the event time is its integer midpoint `(exposure_start_ns + exposure_end_ns) // 2`.
- `ProcessingTime` describes processing or arrival. Cross-domain comparisons require `EventClockMappingSpec`.

### Identity and truth

An event ID is `(station_id, station_session_id, sequence_no)`. All device event types share one sequence per session. Local track IDs, center track IDs, and truth target IDs belong to separate scopes.

`test_architecture.py` statically and dynamically enforces that `chongming` (especially `chongming.bus` and `chongming.vision`) never imports `chongming_proto.truth` or `chongming_scorer`, and that `chongming_scorer` never imports `chongming`.

## Scene generation (`godot_producer/`)

Scene scripts inherit `ProducerSceneContract` (`godot_producer/core/scene_contract.gd`), such as `airspace_base_scene.gd`, `clean_range.tscn`, and `scenic_ridge.tscn`:

- Kinematics, virtual rays (`event-v1` / `tangent_gaussian_v1`), and `TruthFrame` projections are computed in 64-bit `float64` (`PackedFloat64Array`) before setting 32-bit `Node3D` transforms for rendering.
- Known visibility uses Godot 3D physics raycasts against terrain/blocker colliders toward target reference points. Objects behind the target do not block the sight segment.
- `producer_runner.gd` streams `StationConfig`, auxiliary calibration baseline updates, raw RGB8 `FramedVideoPacket`s, `TruthFrame`s, and optional virtual `RayEvent`s to `V-Switch Record`, which writes immutable `Test Scene` packages (encoding MP4s via FFmpeg using the D3b `yuv420p` preset).

## Device processing

Python `EdgeProcess` (`chongming-edge`) and Rust `EdgeProcess` (`crates/edge`) share the same `V-Switch` TCP `BusMessage` protocol and `DevicePolicy` contract: both decode H.264/raw frames from `V-Switch` (or an abstract `FrameSource`) and process auxiliary frames before main frames within an exposure group, separating `decode_duration_ns` from `cv_duration_ns`.

### Detection and local tracking

`MotionDetectionModule` converts images to grayscale and applies MOG2 (mask value 255 -> 8-connected components). `Tracking2DModule` uses constant-velocity prediction and distance-ordered greedy assignment.

The explicit `FrameDifferencePolicy` / `KalmanTrackingPolicy` selects `WorkbenchMotionModule` and `WorkbenchTrackingModule`, sharing frame difference, optional MOG2 or pinned ViBe, occupancy filtering, and Kalman tracking with `chongming.vision`. See [Algorithm evaluation](algorithm-evaluation.md).

### Auxiliary calibration

The reference is a landscape A4 checkerboard (7x5 squares, 6x4 inner corners). OpenCV detects and refines corners, with an SB detector fallback. With full auxiliary-to-main rigid transform, the estimator fixes the main optical center and fits rotation via IPPE/PnP constrained by the installation prior:

```text
R_enu_from_main = R_enu_from_aux * R_aux_from_main
```

Failed recognition transitions solutions from `VALID` -> `FROZEN` -> `STALE`, requiring configured consecutive matches to recover.

### World rays

`RayGenerationModule` unprojects main detections into ENU unit vectors and propagates pixel covariance via the inverse projection Jacobian into tangent basis `(tangent_u_enu_unit, tangent_v_enu_unit)`, preserving independent vs shared installation/calibration uncertainty references.

## Center fusion

`CenterProcess` wraps `ReferenceFusionAlgorithm` over `V-Switch`:

```text
Admission -> exposure window -> voxel candidates -> cross-station association
          -> continuous position estimate -> track update
```

### Admission and windows

Registries store immutable station configs and calibration solutions. Exposure midpoints map events into half-open windows `[origin + k * width, origin + (k + 1) * width)` aligned to `origin_ns`. Explicit `ProgressReport` messages advance watermarks even for empty frames. `timeout_basis` supports `processing_time`, `capture_time`, and `disabled` (recommended in lock-step `V-Switch` replay).

### Candidates, association, geometry, and tracking

- Coarse voxel search clips forward rays to `VoxelSearchSpec` and counts distinct supporting stations per cell.
- Cross-station association groups at most one ray per station (or `--postintersection` enumerates subsets with 3 m complete-link deduplication).
- Generalized least squares fits transverse planes `C_perp = d^2 C_angle + T C_origin T.T` with optional Huber loss; exact zero-covariance directions act as hard constraints.
- Constant-velocity Kalman tracking (`[E, N, U, vE, vN, vU]`) aligns asynchronous measurements via `[I B]` motion-alignment evidence without double-counting prior velocity, and clamps position variances to at least `25 m^2`.

## Rendering and display

The package under [src/chongming/rendering/godot/](../src/chongming/rendering/godot/) is strictly the passive **Godot Viewer** (`--viewer <host:port>`). `V-Switch` pushes ephemeral 4-byte BE length-prefixed `chongming-viewer-v1` JSON `DisplayFrame`s with drop-on-lag backpressure.

- 64-bit integers cross GDScript `JSON.parse_string()` as base-10 decimal strings.
- Five toggleable 3D layers (`truth`, `rays`, `measurements`, `tracks`, `voxels`) display cached state without altering outputs:
  - Rays decay after 1 s of experiment time; measurements and deleted tracks decay after 3 s; track history retains up to 100 points.
  - Voxel votes `< 2` (or `<= 2` where gated) render as wireframe alpha `0.06..0.18`.
- **No scoring in Godot**: `evaluation.gd` was removed; online `ScoreEvent`s come solely from `chongming-scorer` over `V-Switch`.

## Technical references

- [OpenCV PnP](https://docs.opencv.org/4.x/d5/d1f/calib3d_solvePnP.html) describes pose estimation APIs.
- [godot-openstreetmap](https://github.com/RodZill4/godot-openstreetmap/tree/a3a9ca5c46ddcc956a7d4798ee835d749d711e42) is an MIT-licensed implementation reference, not a runtime dependency.
- [Pixeltovoxelprojector](https://github.com/ConsistentlyInconsistentYT/Pixeltovoxelprojector) is a conceptual reference only; its [license](https://github.com/ConsistentlyInconsistentYT/Pixeltovoxelprojector/blob/011722ac4e7403de4dbf764b6877a6561a0cf45c/LICENSE) has extra restrictions.
