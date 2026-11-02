# Design

## Scope

Chongming is a research system for small airborne targets in daylight. Fixed camera stations send observations to a local center algorithm. Synthetic scenes supply images, virtual observations, and separate truth data.

## Runtime and ownership

```text
Authoring JSON and local assets
  -> Python scene state and experiment clock
       -> Godot sensor images -> device algorithms -> center algorithm
       -> virtual ray events -----------------------> center algorithm
       -> truth ------------------------------------> display and evaluation

Algorithm inputs and outputs -> evidence files
Algorithm results + time-matched truth -> Godot diagnostics
```

Python owns experiment time, scene state, algorithm state, and evidence. Godot renders images and displays results. A persistent Godot process communicates with Python over a serialized local TCP connection. Device and center algorithms run in the Python process.

| Mode               | Driver           | Algorithm input                                             |
| ------------------ | ---------------- | ----------------------------------------------------------- |
| 1: preview         | `PreviewRuntime` | No center algorithm; auxiliary calibration can be inspected |
| 2: device test     | `CenterSession`  | Rendered main and auxiliary images                          |
| 3: center test     | `CenterSession`  | Virtual ray events                                          |
| 4: integrated test | `CenterSession`  | Rendered images, then actual device events                  |

Modes 2 and 4 require explicit device policies. They render every scheduled sensor frame even when display and recording are off. Missing images fail the run.

`CenterSession` is the working experiment driver. The general `SystemRunnerModule` is a stub. The reference plugin builds its own active center through `declare_center()`.

One step processes one exposure group. It records raw inputs and results before adapting them for display. With a UI, it waits for Godot to accept the update before taking the next step. Reset creates a new session and output directory. Shutdown closes the algorithm, source, files, socket, and renderer.

## Source layout

All paths below are under `src/chongming/`.

| Path                                       | Responsibility                                               |
| ------------------------------------------ | ------------------------------------------------------------ |
| `contracts/`                               | Immutable records, policies, units, and states               |
| `ports/`                                   | Typed behavior interfaces using `Protocol`                   |
| `modules/`                                 | Algorithms and explicit business stubs                       |
| `composition/`                             | Dependency construction and lifecycle bindings               |
| `scenarios/`                               | Authoring, assets, OSM import, export, and playback          |
| `rendering/`                               | Geometry, occlusion index, Python adapter, and Godot project |
| `development.py`, `edge_runtime.py`        | Synchronous experiment and device execution                  |
| `calibration.py`                           | Reference recognition, pose estimation, history, and state   |
| `reference.py`                             | Reference center plugin and engine adapter                   |
| `preview.py`, `center_ui.py`               | Preview editing and algorithm display control                |
| `visualization.py`, `voxel_diagnostics.py` | Read-only display conversion and raw voxel capture           |
| `vision/`                                  | Separate frame-difference experiments and tuning tools       |

Construction does not start business work. `ModuleShell` separates lifecycle state from implementation status. An unimplemented module can enter `RUNNING` with reason `algorithm_unimplemented`; its business methods raise `NotImplementedError`.

## Data contracts

The online records are in [contracts/model.py](../src/chongming/contracts/model.py). They use frozen dataclasses, tuples, typed identifiers, and enums. Images use bytes or memory views with explicit dimensions, pixel format, and row stride.

### Coordinates and time

- World coordinates are right-handed ENU: east, north, up, in meters.
- Camera axes are right, down, forward. Pixel `(0, 0)` is the top-left pixel center.
- `R_A_from_B` maps a vector from frame B to frame A. Ray directions are unit vectors.
- Godot receives ENU as `(east, up, -north)`.
- `CaptureTime` stores an exposure interval. The event time is its integer midpoint.
- `ProcessingTime` describes processing or arrival. Cross-domain comparisons require an explicit clock mapping.

Main and auxiliary cameras have independent sample schedules. Frames with the same exposure midpoint form a group.

### Identity and truth

An event ID is `(station_id, station_session_id, sequence_no)`. All device event types share one sequence per image session. Virtual events use a different session and sequence. New runs use fresh session IDs, even with the same random seed.

Local track IDs, center track IDs, and truth target IDs have different scopes. They cannot be used as interchangeable associations.

`ScenarioStep` has three outputs: `video`, `packets`, and `truth`. Online ports exclude scene truth types. `SceneDefinition` stores generation parameters; `StationConfig` stores the estimates available to algorithms.

This separation is an interface rule, not a security sandbox. Plugins are trusted Python code in the same process.

## Scene generation

`compile_authoring()` converts `scenario-authoring-v2` JSON into `ScenarioSpec`. The compiler uses demo defaults for omitted fields. Configuration checks reject unknown fields and unsupported features.

`ScenarioModule` schedules exposures, advances the clock, samples geometry, requests images, and emits online events and truth separately. Motion, wind, clouds, and jitter use experiment time and fixed seeds. The behavior registry provides versioned constant-velocity and orbit motion without importing code from JSON.

Known visibility tests the finite segment from the optical center to the target reference point. Objects behind the target do not block that segment. `OcclusionIndex` uses a CPU bounding-volume hierarchy to filter static blockers before exact proxy tests.

With `occlusion_mode: "unknown"`, the scene checks the field of view but does not determine occlusion. This mode cannot supply occlusion-corrected detection scores.

The renderer supports distortion-free cameras, square pixels, zero skew, a single exposure sample, and global shutter. Configured rain, transport faults, rolling shutter, sensor noise, and continuous pose drift are rejected.

## Device processing

`EdgeRun` processes auxiliary frames before main frames within the same exposure group. It journals actual results and input pixel hashes.

### Detection and local tracking

`MotionDetectionModule` converts images to grayscale and applies MOG2. Only mask value 255 is foreground. Eight-connected components pass inclusive area limits; their centroids become candidates.

`Tracking2DModule` uses constant-velocity prediction and distance-ordered greedy one-to-one assignment. Velocity comes from the last two actual detections. Consecutive hits confirm a track; missing frames break the hit streak.

### Auxiliary calibration

The reference is a landscape A4 checkerboard with 7 by 5 squares and 6 by 4 inner corners. OpenCV detects and refines corners, with an SB detector fallback.

When the full main-to-auxiliary rigid transform is available, the estimator fixes the main optical center and fits rotation. Otherwise it uses planar IPPE candidates. Positive depth, reprojection error, and an installation rotation prior filter the candidates.

```text
R_enu_from_main = R_enu_from_aux * R_aux_from_main
```

The relative rotation stays fixed.

`CalibrationHistoryStore` returns only solutions and states known at the main exposure time. Trusted solutions are immutable. Failed recognition retains the last solution in `FROZEN`; timeout makes it `STALE`. Recovery requires the configured number of successful observations.

### World rays

`RayGenerationModule` inverts the main intrinsics, removes distortion, normalizes the camera direction, and rotates it into ENU. It uses the inverse projection Jacobian and normalization derivative to propagate pixel covariance into a two-dimensional tangent plane.

Each ray separates independent angular and origin covariance from shared installation and calibration references. The fixed-center model has zero independent translation covariance.

Missing centroid covariance can use an explicit policy fallback. Unknown required residual errors stop ray generation. Nonzero jitter or flex terms require a stated independence basis. Every legal main exposure reports completion.

## Center fusion

`ReferenceFusionAlgorithm` registers online configurations and calibration solutions. `CenterFusionModule` runs these stages for sealed observation windows:

```text
Admission -> exposure window -> voxel candidates -> cross-station association
          -> continuous position estimate -> track update
```

### Admission and windows

Registries retain immutable snapshots. Admission checks identity, configuration, coordinate frame, exposure, calibration evidence, and uncertainty. Missing references can quarantine an event for retry; its original arrival time remains unchanged.

Exposure midpoints select half-open windows `[start, end)`. Active station progress controls the watermark, which determines when a window can close. Explicit frame completion lets empty detections advance it. Idle and offline rules prevent a silent station from blocking all work.

### Candidates and association

The voxel search clips forward rays to the search box and range. Sparse grid traversal includes boundary-touching cells. Independent uncertainty can expand support. Each cell counts independent stations, not the number of rays from one station. Cells with identical support form a coarse candidate.

Association enumerates station subsets and ray choices. A group uses at most one ray per station. Bounded exclusive search preserves unresolved alternatives. Candidate, hypothesis, or search limits produce truncation diagnostics.

### Geometry and uncertainty

Position estimation fits the original rays in their tangent planes. For depth `d` and tangent basis `T`, independent transverse covariance is:

```text
C_perp = d^2 C_angle + T C_origin T.T
```

The solver iterates depth-dependent generalized least squares. Optional Huber weighting acts on whitened residual norms. Zero covariance directions are exact constraints in this geometry solver.

Acceptance requires enough stations, a useful intersection angle, acceptable conditioning, forward depth, and bounded residuals. Failed convergence or inconsistent exact constraints do not produce a valid position.

Calibration usage changes independent precision weights, shared errors stay referenced. Reported covariance is conditional on the configuration, calibration, and final weighting model.

### Motion and tracking

A new target without velocity information needs observations close enough in exposure time. A valid track prior can align asynchronous rays to the earliest supporting exposure.

Aligned measurements retain their dependency on that prior. The tracker uses the correlated observation model below instead of treating prior velocity as new independent evidence:

```text
z + B v_prior observes [I B] x
```

Tracking uses state `[E, N, U, vE, vN, vU]`. One measurement creates a seed with unknown velocity. A later exposure initializes velocity and covariance. One-to-one assignment handles confirmation, coasting, and deletion.

Process noise models constant acceleration over the prediction interval:

```text
G = [dt^2 / 2 I; dt I]
Q = G Cov(a) G.T
```

The current tracker also clamps position variances to at least `25 m^2` in prediction and after filter updates. This hard-coded floor affects gates and reported uncertainty.

## Rendering and display

`GodotRenderer` copies package resources into a writable temporary project. A lock serializes requests. Length-prefixed JSON describes a scene or UI state; raw RGB8 bytes carry images. The process uses the Compatibility backend and a graphics context.

Preview keeps the latest display state and owns an edit draft. Editing acquires a token, compiles the draft, checks the file's byte hash, and atomically saves it. Algorithm modes disable editing and time jumps.

`CenterRunUi` sends one acknowledged increment per algorithm call. Integer fields in visualization records use decimal strings on the wire to preserve signed 64-bit values through Godot JSON parsing. The receiver validates a private copy before changing caches.

Five layers show truth, rays, measurements, tracks, and raw voxels, using a free-view-only render layer. Measurement selection highlights cached supporting rays.

Rays remain for 1 s of experiment time, measurements for 3 s, and deleted tracks for 3 s. Track history holds 100 time-ordered points. Raw voxels describe the last exported sealed window, their votes count stations.

### Error diagnostics

`evaluation.gd` groups rays by station and exposure time, and measurements by reference time. It compares them with independently sampled truth at those times. Matching first maximizes the number of legal pairs, then minimizes total error.

The development gates are `0.01 rad` for pointing and `10 m` for position. Statistics obey:

```text
outputs = matched + unmatched + unevaluable
valid = matched + unmatched
```

Mean error, RMSE, and maximum error use matched samples only. No samples produce `null`.

## Evidence and playback

`SceneEvidenceWriter` separates ordinary inputs from privileged truth. It stores unchanged scene fields in `truth-static.json` and references them by digest. If those fields change, a record stores them inline. The reader checks references and restores a full snapshot.

Input frame hashes identify pixels but cannot recover them. Output files and interpretation are listed in [Development](development.md#evidence-and-playback).

Recorded preview restores images and selected truth records, still needs Godot. At recording end it can switch to a live scene source. It is not a complete algorithm replay system.

## Technical references

- [OpenCV PnP](https://docs.opencv.org/4.x/d5/d1f/calib3d_solvePnP.html) describes the pose-estimation APIs.
- [godot-openstreetmap](https://github.com/RodZill4/godot-openstreetmap/tree/a3a9ca5c46ddcc956a7d4798ee835d749d711e42) is an MIT-licensed implementation reference, not a runtime dependency.
- [Pixeltovoxelprojector](https://github.com/ConsistentlyInconsistentYT/Pixeltovoxelprojector) is a conceptual reference. Its [pinned license](https://github.com/ConsistentlyInconsistentYT/Pixeltovoxelprojector/blob/011722ac4e7403de4dbf764b6877a6561a0cf45c/LICENSE) has extra restrictions; do not treat it as standard Apache-2.0 code.

These references do not validate this project's accuracy or performance.
