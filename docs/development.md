# Development

Run source commands from the repository root. For system behavior and data contracts, see [Design](design.md) and [Wire Protocol](protocol.md). For a nontechnical overview, see [Introduction](introduction.md).

## Environment

Use Nix with `nix-command` and `flakes` enabled:

```sh
nix develop --no-update-lock-file
uv sync --frozen
```

The shell provides Python 3.12, uv, Godot 4.7.2 stable, FFmpeg, Ruff, Rust (`rustc`, `cargo`, `rustfmt`, `clippy`), Protocol Buffers (`protoc`), Git, and treefmt. Linux also has `xvfb-run`. `flake.lock`, `uv.lock`, and `Cargo.lock` pin dependencies. The package supports Python 3.12 through 3.14.

Use `uv run --frozen` for Python commands. Use the shell's `ruff`, not `uv run ruff`, on NixOS. Do not update dependencies, lock files, or tools without approval. Build the Rust `V-Switch` binary with `cargo build -p vswitch`.

### Graphics

Godot Producer recording and the passive Godot Viewer require a graphics session and OpenGL 3.3. `CHONGMING_GODOT` selects the Godot executable and `CHONGMING_FFMPEG` selects FFmpeg. The Nix shell sets both.

For Linux without a desktop, use software rendering:

```sh
nix develop --no-update-lock-file .#software --command xvfb-run -a ...
```

### Install a release

A user archive contains a wheel, source archive, hashed dependency list, scenarios/policies, and documentation. Check its hash against a trusted release source (`sha256sum -c SHA256SUMS`) before extraction.

Inside the extracted user directory, with one release version present:

```sh
uv venv --python python3 .venv
uv pip install --python .venv/bin/python --require-hashes --no-deps --requirements requirements.txt
uv pip install --python .venv/bin/python --no-deps ./chongming-*-py3-none-any.whl
uv pip check --python .venv/bin/python
source .venv/bin/activate
chongming-center-bus --help
chongming-edge --help
chongming-scorer --help
```

To build `vswitch` / `edge` or run `godot_producer/`, extract the sdist and run `cargo build -p vswitch -p edge` in the source directory.

## Commands and modes

| Command                | Purpose                                                                                 |
| ---------------------- | --------------------------------------------------------------------------------------- |
| `vswitch`              | Rust hub (`record`, `replay`, `live` modes; build from `crates/vswitch/`)               |
| `edge`                 | Rust online edge bus client (`--policy` `DevicePolicy` JSON; build from `crates/edge/`) |
| `chongming-edge`       | Python online edge bus client (`--policy` `DevicePolicy` JSON)                          |
| `chongming-center-bus` | Online center bus client (`--policy` `FusionPolicy` JSON)                               |
| `chongming-scorer`     | Independent `Test Scene` / `Run Output` verifier, offline scorer, and `--subscribe`     |

Use `--help` on any binary or script for the complete option list.

### Test Scene recording and replay

1. **Record a `Test Scene`** from Godot Producer (`godot_producer/`):

   Start `vswitch record` (example addresses):

   ```sh
   ./target/debug/vswitch record \
     --output-scene results/clean-range-scene \
     --scenario-id clean-range \
     --world-frame-id demo-enu \
     --clock-domain-id sim-clock \
     --bus-bind 127.0.0.1:7000 \
     --truth-bind 127.0.0.1:7001
   ```

   Then launch Godot Producer pointing at those sockets (`clean_range.tscn` or `scenic_ridge.tscn`):

   ```sh
   "$CHONGMING_GODOT" --path godot_producer -- \
     --scene-tscn res://scenes/clean_range.tscn \
     --bus 127.0.0.1:7000 --truth 127.0.0.1:7001 --steps 30
   ```

   Wait for `vswitch record` to finish encoding videos and writing hashes, then verify the immutable scene:

   ```sh
   uv run --frozen chongming-scorer verify-scene results/clean-range-scene --verify-videos
   ```

2. **Replay in lock-step (`integrated`, `edge`, or `center`)** with optional passive `Godot Viewer` and online scorer:

   ```sh
   ./target/debug/vswitch replay \
     --scene results/clean-range-scene \
     --run-output results/clean-range-run \
     --run-id run-1 \
     --mode integrated \
     --stepping lock-step \
     --bus-bind 127.0.0.1:7000 \
     --truth-bind 127.0.0.1:7001 \
     --viewer-bind 127.0.0.1:7002 \
     --wait-scorer
   ```

   Connect peers:

   ```sh
   uv run --frozen chongming-edge --host 127.0.0.1 --port 7000 --policy <device-policy.json>
   uv run --frozen chongming-center-bus --host 127.0.0.1 --port 7000 --policy <fusion-policy.json> --timeout-basis capture_time
   uv run --frozen chongming-scorer subscribe --host 127.0.0.1 --port 7001 --scene results/clean-range-scene --output results/clean-range-run
   "$CHONGMING_GODOT" --path src/chongming/rendering/godot -- --viewer 127.0.0.1:7002
   ```

   Then score offline if desired:

   ```sh
   uv run --frozen chongming-scorer inspect-run results/clean-range-run
   uv run --frozen chongming-scorer score --scene results/clean-range-scene --run results/clean-range-run
   ```

3. **Live hardware mode** (`Edge -> V-Switch Live -> Center`, no truth or scene):

   ```sh
   ./target/debug/vswitch live --bus-bind 127.0.0.1:7000 --viewer-bind 127.0.0.1:7002
   ```

Note that `EventWindowPolicy.timeout_basis` supports `processing_time` (wall-clock default), `capture_time`, and `disabled`. Lock-step TCP replay over `V-Switch` should use `capture_time` or `disabled` so real-time pauses do not mark stations idle or offline.

4. **Legacy JSONL conversion**:

   ```sh
   uv run --frozen python scripts/convert_legacy_recording.py test-scene \
     --recording <legacy-recording-dir> --output <test-scene-dir> --link-video
   uv run --frozen python scripts/convert_legacy_recording.py run-output \
     --scene <test-scene-dir> --output <run-output-dir> --mode integrated \
     --device-dir <legacy-device-dir> --center-dir <legacy-center-dir>
   ```

### Manual RTK control-point calibration

[scripts/rtk_calibrate.py](../scripts/rtk_calibrate.py) is a standalone tool for clicking surveyed local-ENU control points on a recorded camera frame, saving/reloading correspondences, and fitting camera pose (`optical_center_enu_m` and `rotation_enu_from_cam`) with held-out checkpoint residuals:

```sh
# Interactive GUI (with MacBook 1080p DFOV 122 deg pinhole preset or --intrinsics <json>)
uv run --frozen python scripts/rtk_calibrate.py \
  --image results/frame.png --csv results/points.csv --preset macbook-1080p-dfov122

# Reopen saved correspondences or solve headless from saved correspondences
uv run --frozen python scripts/rtk_calibrate.py --correspondences results/correspondences.json
uv run --frozen python scripts/rtk_calibrate.py --solve \
  --correspondences results/correspondences.json --preset macbook-1080p-dfov122 \
  --output results/calibration.json
```

- **Control-point CSV**: UTF-8 CSV with columns `point_id,east_m,north_m,up_m` and optional `role` (`fit` or `check`, default `fit`) and `note`. Convert RTK measurements into the shared local ENU frame before loading.
- **Intrinsics JSON**: Either standalone (`image_width_px`, `image_height_px`, `fx`/`fy`/`cx`/`cy` or `matrix_px`, `distortion_model`, `distortion_coefficients`) or repository `CameraModel` / `StationConfig` JSON. The loaded image dimensions must match `image_width_px x image_height_px`.
- **GUI limitation**: The locked Python environment installs `opencv-python-headless` (which omits cross-platform HighGUI widgets and interactive point-list controls), so the GUI uses standard-library `tkinter` and `Pillow` for display/picking while keeping OpenCV for PnP and reprojection.
- Keep camera frames, survey CSVs, correspondences, and exported JSONs in ignored directories such as `results/` or outside the repository.

### macOS camera recording and timestamp audit

[scripts/record_camera.py](../scripts/record_camera.py) records raw camera video without overlays (`--width 1920 --height 1080 --fps 30` by default) paired 1-to-1 with per-frame timestamps, session metadata, and a post-capture timing report:

```sh
# Setup once from repo root: nix develop --no-update-lock-file --command uv sync --frozen
# Record raw footage + per-frame timestamps + read-only NTP check + reopen verification
uv run --frozen python scripts/record_camera.py record \
  --station-id station-a --camera 0 --duration 10 --output-dir results/recordings

# Probe camera indices, run read-only NTP check, or re-verify an existing recording
uv run --frozen python scripts/record_camera.py list-cameras
uv run --frozen python scripts/record_camera.py ntp-check --server time.apple.com
uv run --frozen python scripts/record_camera.py verify \
  --video results/recordings/<clip>.mp4 --timestamps results/recordings/<clip>_timestamps.csv
```

- On macOS, grant Camera permission (`System Settings -> Privacy & Security -> Camera`) to the terminal or IDE running the recorder.
- **Timestamps and clock audit**: When the capture backend does not expose advancing hardware timestamps (`CAP_PROP_POS_MSEC`), rows are explicitly labeled `host_receipt_time` with both `CLOCK_REALTIME` and `monotonic_ns` recorded so clock jumps stay visible. Read-only UDP SNTP queries (`ntp-check`) measure offset and half-RTT uncertainty without modifying the system clock.
- **33 ms threshold**: A single-camera audit can identify a timing failure or report `provisional`. An advancing backend timestamp and an SNTP result do not establish cross-camera alignment. Compare both clips against a shared physical event before applying the threshold.
- Keep all recorded clips, timestamp CSVs, and reports under ignored directories such as `results/` or outside the repository.

### Replay a single Mac recording

Use the original video, the matching `record_camera.py` timestamps CSV, and an intrinsics JSON for the recorded resolution. The importer uses the monotonic host receipt times relative to the first frame. Those times are a single-station replay timeline. Exposure duration and camera-to-camera synchronization are unknown.

From the repository Nix shell:

```sh
cargo build --locked -p vswitch -p edge
uv run --frozen python scripts/import_mac_recording.py \
  --video results/recordings/clip.mp4 \
  --timestamps results/recordings/clip_timestamps.csv \
  --intrinsics results/intrinsics.json \
  --output results/mac-field-scene
./target/debug/vswitch verify-scene results/mac-field-scene --verify-videos
./target/debug/vswitch replay --scene results/mac-field-scene \
  --run-output results/mac-field-run --run-id mac-field-run --mode edge
```

Once `vswitch` reports its listening address, run this in a second Nix shell:

```sh
./target/debug/edge --host 127.0.0.1 --port 7000 \
  --policy scenarios/policies/mac_field_edge.json
```

The example MOG2 policy uses station ID `station-a` and a 15-frame background warmup. Record with `--station-id station-a` or copy the policy to your results directory and change its top-level key to match the CSV. Include some background before the first target enters the frame.

The importer creates an H.264 MP4 without B-frames and checks that its frame count matches the CSV. Use a fresh output directory for each import and run. The scene has main-camera intrinsics and frame timing. The two required test streams for truth and virtual rays contain zero records. Rust Edge writes its `DetectionBatch` messages to `results/mac-field-run/edge_events.pb.zst`. `chongming-scorer inspect-run results/mac-field-run` lists the saved streams. Scoring the run reports `unavailable` because it has no labels.

## Device policy

A policy file maps each station ID to a complete [DevicePolicy](../src/chongming/contracts/device.py), or uses `FrameDifferencePolicy` / `KalmanTrackingPolicy` workbench settings (see [scenarios/policies/](../scenarios/policies/)). Station IDs must match the active `StationConfig`s exactly. The loader rejects duplicate keys, unknown fields, invalid values, and unsupported algorithms.

```json
{
  "example-station": {
    "detection": {
      "algorithm_id": "mog2-components-v1",
      "score_definition": {
        "definition_id": "foreground-component-fill-ratio-v1",
        "kind": "ranking",
        "minimum": 0.0,
        "maximum": 1.0,
        "cross_station_comparison_group": null
      },
      "minimum_component_area_px2": 1.0,
      "maximum_component_area_px2": null,
      "background_warmup_frame_count": 1,
      "background_history_frame_count": 20,
      "background_variance_threshold": 16.0,
      "background_learning_rate": 0.0,
      "background_detect_shadows": false
    },
    "tracking": {
      "algorithm_id": "constant-velocity-nearest-neighbor-v1",
      "association_gate_px": 6.0,
      "confirmation_frame_count": 2,
      "maximum_missing_duration_ns": 500000000,
      "quality_definition": {
        "definition_id": "consecutive-detection-fraction-v1",
        "kind": "ranking",
        "minimum": 0.0,
        "maximum": 1.0,
        "cross_station_comparison_group": null
      }
    },
    "calibration": {
      "update_period_ns": 0,
      "stale_after_ns": 2000000000,
      "recovery_success_count": 1,
      "minimum_reference_count": 4,
      "maximum_reprojection_rmse_px": 2.0,
      "maximum_rotation_only_parallax_px": null,
      "estimator_id": "opencv-checkerboard-v1"
    },
    "rays": {
      "uncertainty_model_id": "tangent_gaussian_v1",
      "fallback_centroid_covariance_px2": null,
      "unmodelled_high_frequency_jitter_covariance_rad2": null,
      "unmodelled_relative_flex_covariance_rad2": null,
      "residual_independence_basis": null
    }
  }
}
```

Background parameters must be explicit. Learning rate is in `[0, 1]`; automatic rate `-1` is not supported. Ray error terms are symmetric positive semidefinite 2x2 matrices; nonzero residual terms require an explicit `residual_independence_basis`.

### JSON content identity

[json_codec.py](../src/chongming/contracts/json_codec.py) defines compact UTF-8 canonical JSON with sorted object keys and binary64 safe-integer validation (`scripts/hash_json.py`).

## Algorithm development

[Source layout](design.md#source-layout) lists module ownership. `ports.development.CenterAlgorithm` defines the center plugin (`start(registration)`, `process(step)`, `close()`), implemented by `reference.create_algorithm()` (`ReferenceFusionAlgorithm`). Pass `--postintersection` or `--voxel-diagnostics` to `chongming-center-bus` as needed; see [Algorithm evaluation](algorithm-evaluation.md).

`composition/center.py::reference_policy()` builds default fusion policies for explicit search volumes, clock domains, and window widths.

## Checks

In the CI shell (`nix develop --no-update-lock-file .#ci`, as used by `.github/workflows/checks.yml`):

```sh
treefmt --no-cache --fail-on-change
ruff check src tests scripts
uv run --frozen mypy src tests
uv run --frozen python scripts/generate_proto.py --check
cargo fmt --all -- --check
cargo clippy --workspace --all-targets -- -D warnings
cargo test --workspace
```

Full regression including Godot Producer (`test_godot_producer_unit.py`, `test_godot_producer_e2e.py`), Rust Edge (`test_rust_edge_e2e.py`), and Godot Viewer (`test_godot_viewer.py`) requires Godot, compiled `vswitch` / `edge` binaries, and a graphics context:

```sh
nix develop --no-update-lock-file .#software --command xvfb-run -a uv run --frozen python \
  -m unittest discover -s tests -p "test_*.py" -v
```

Note on historical test state: `tests/test_fusion_tracking.py::TrackingTests::test_kalman_update_matches_known_scalar_gain_and_joseph_covariance` remains an unresolved failure (`3.480769` vs `3.42`) introduced with the 5m variance floor (`98d9b98`).

### Formatting

`nix fmt` uses `treefmt` with `nixfmt`, `ruff`, `rustfmt`, and `prettier`. Check formatting with:

```sh
nix develop --no-update-lock-file .#ci --command treefmt --no-cache --fail-on-change
```

## Local release

The release tool builds local archives only and does not publish them:

```sh
uv run --frozen python scripts/distribute.py build --output dist/release
uv run --frozen python scripts/distribute.py check --output dist/release --render
```

Artifacts produced in the output directory:

- Source archive (`sdist`, containing Python packages, `proto/`, `godot_producer/`, `crates/`, `Cargo.*`, tests, scripts, scenarios/policies, and docs).
- Wheel built from that source archive (`chongming`, `chongming_proto`, `chongming_scorer`, and bundled Godot Viewer files).
- User archive (documentation, scenarios, sdist, wheel, and hashed `requirements.txt`).
- `SHA256SUMS` for the four artifacts.

## Third-party assets and licenses

Code licensing is stated in the [README](../README.md#license-and-use). Keep source URLs, versions or dates, byte hashes, conversion commands, and license terms with external inputs.

### Optional official ViBe library

The optional baseline uses the [author's official repository](https://github.com/vandroogenbroeckmarc/vibe), pinned at `da0c197d36272125589180e20fdd32b6fe3ba548` (Apache-2.0):

```sh
revision=da0c197d36272125589180e20fdd32b6fe3ba548
mkdir -p external-projects/vibe-source/C
for file in C/vibe-background-sequential.c C/vibe-background-sequential.h LICENSE; do
  curl --fail --location "https://raw.githubusercontent.com/vandroogenbroeckmarc/vibe/$revision/$file" \
    --output "external-projects/vibe-source/$file"
done
nix develop --no-update-lock-file --command uv run --frozen python scripts/prepare_vibe.py \
  --source external-projects/vibe-source --output external-projects/vibe-build
```

Set `CHONGMING_VIBE_TEST_LIBRARY` to the recorded library path in `manifest.json` to run `test_vibe.NativeVibeTests` (otherwise skipped).

### Maps and external test assets

The flake pins the optional `hannover-500m` archive (OSM data under ODbL 1.0; DEM derived from Mapzen/Copernicus/EU-DEM/GMTED2010/SRTM). Extract only into ignored local directories:

```sh
mkdir -p tmp
nix build --no-update-lock-file .#hannover-500m --out-link tmp/hannover-500m-archive
```

Use `scripts/pack_local_asset.py` for deterministic local `.tar.gz` archives. Do not commit local assets or experiment outputs into Git or release bundles.
