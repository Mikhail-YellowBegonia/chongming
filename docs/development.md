# Development

Run source commands from the repository root. For system behavior and data contracts, see [Design](design.md). For a nontechnical overview, see [Introduction](introduction.md).

## Environment

Use Nix with `nix-command` and `flakes` enabled:

```sh
nix develop --no-update-lock-file
uv sync --frozen
```

The shell provides Python 3.12, uv, Godot 4.7.2 stable, FFmpeg, Ruff, Git, and treefmt. Linux also has Xvfb. `flake.lock` pins Nix inputs; `uv.lock` pins Python dependencies. The package supports Python 3.12 through 3.14.

Use `uv run --frozen` for Python commands. Use the shell's `ruff`, not `uv run ruff`, on NixOS. Do not update dependencies, lock files, or tools without approval. First setup may need network access. The shell supplies native libraries required by Python wheels; using `.venv/bin/python` outside it can fail.

### Graphics

Actual runs need a graphics session and OpenGL 3.3, even without `--ui` or video. `CHONGMING_GODOT` selects the required Godot executable. `CHONGMING_FFMPEG` selects FFmpeg for video export and playback. The Nix shell sets both.

For Linux without a desktop, use software rendering:

```sh
nix develop --no-update-lock-file .#software --command xvfb-run -a uv run --frozen chongming-scene \
  --scenario scenarios/empty.json --output results/software-example --no-video
```

The output directory must not exist. Godot `--headless` uses a dummy renderer.

### Install a release

A user archive contains a wheel, source archive, hashed dependency list, scenarios, and documentation. Check its hash against a trusted release source. If all release files are present, run `sha256sum -c SHA256SUMS` before extraction.

Inside the extracted user directory, with one release version present:

```sh
uv venv --python python3 .venv
uv pip install --python .venv/bin/python --require-hashes --no-deps --requirements requirements.txt
uv pip install --python .venv/bin/python --no-deps ./chongming-*-py3-none-any.whl
uv pip check --python .venv/bin/python
source .venv/bin/activate
chongming-center --scenario scenarios/center_clean.json --check
```

Use direct commands after activation, without the source workflow's `uv run` prefix. Godot and FFmpeg remain external requirements. On NixOS, use the source development shell to supply native libraries.

To follow source links or run development commands, extract the sdist and use its source directory and documentation.

## First run

Validate a bundled scenario:

```sh
uv run --frozen chongming-center --scenario scenarios/center_clean.json --check
```

Open the preview:

```sh
uv run --frozen chongming-preview --scenario scenarios/review.json
```

After closing it, start a center test with the reference algorithm and Godot diagnostics:

```sh
uv run --frozen chongming-center --scenario scenarios/center_clean.json \
  --output results/center-ui --ui
```

## Commands and modes

| Command             | Purpose                                       |
| ------------------- | --------------------------------------------- |
| `chongming-preview` | Scene preview, editing, and browser workbench |
| `chongming-center`  | Configuration checks and modes 2, 3, and 4    |
| `chongming-scene`   | Finite scene export with evidence             |
| `chongming-osm`     | Offline OSM import                            |

Use `--help` for the complete option list. `python -m chongming` starts the preview.

| Center mode | Input                           | Default center algorithm                                 | Required policy           |
| ----------- | ------------------------------- | -------------------------------------------------------- | ------------------------- |
| 2           | Rendered images                 | None                                                     | Per-station device policy |
| 3           | Virtual rays                    | `InputInspector` in CLI; reference algorithm with `--ui` | No device policy          |
| 4           | Rendered images through devices | Reference algorithm                                      | Per-station device policy |

Mode 3 is the default. `InputInspector` checks input and produces no positions. To run reference fusion in the CLI:

```sh
uv run --frozen chongming-center --scenario scenarios/center_clean.json \
  --algorithm chongming.reference:create_algorithm \
  --output results/center-reference --run --tick-hz 10
```

For device or integrated tests, supply a policy file matching every station. Replace the policy path below with a real file:

```sh
uv run --frozen chongming-center --mode 4 --scenario scenarios/center_clean.json \
  --device-policy /path/to/device-policy.json --check
uv run --frozen chongming-center --mode 4 --scenario scenarios/center_clean.json \
  --device-policy /path/to/device-policy.json --output results/integrated-ui --ui
```

Use mode 2 for device-only tests. It rejects `--algorithm`. Mode 3 rejects device policies. `--check` also validates the selected mode's policy requirements.

`--video` enables sensor display and evidence recording. Modes 2 and 4 still render algorithm input when it is absent. `--tick-hz` controls wall-clock pacing in `(0, 240]`.

Without `--ui` or `--run`, the CLI starts paused:

| Command                   | Action                                                  |
| ------------------------- | ------------------------------------------------------- |
| `step`                    | Process one exposure group                              |
| `run 10` or `run`         | Process a fixed count or run to finite end              |
| `video on` or `video off` | Change display and recording between steps              |
| `status`                  | Show current state                                      |
| `reset`                   | Close the old session and create a new output directory |
| `quit`                    | Close resources and exit                                |

During `run`, Ctrl+C requests a stop after the current step.

### Preview controls

The preview opens Godot and a browser workbench at `http://127.0.0.1:8765/`. `--port` changes the port; `--no-browser` suppresses browser launch. Closing Godot or pressing Ctrl+C ends the session.

`--render-fps` limits preview production and pixel requests without changing exposure timestamps. `--no-video` stops preview pixel requests.

Use the toolbar to play, pause, step, reset, seek, or select a station. In free view, hold the right mouse button to look around; use WASD to move, Q/E to move vertically, and Shift for faster motion.

Editing requires an explicit `--scenario` file. It pauses the experiment and acquires an edit token. Submit compiles and saves the draft only if the file has not changed outside the editor. Cancel reloads the file; closing discards the draft. The built-in demo and recorded playback do not support persistent editing.

## Device policy

A policy file maps each station ID to a complete [DevicePolicy](../src/chongming/contracts/device.py). Station IDs must match the scenario exactly. The loader rejects duplicate keys, unknown fields, invalid values, and unsupported algorithms. This one-station template below shows the structure.

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

Background parameters must be explicit. Learning rate is in `[0, 1]`; automatic rate `-1` is not supported. Detection and tracking algorithm IDs and score definitions must match the implementation.

Ray error terms are symmetric positive semidefinite 2 by 2 matrices, in `px^2` or `rad^2` as named. `null` means unknown; zero means a stated zero-error assumption. The detector does not supply centroid covariance, so ray generation needs a pixel fallback and known residual terms. Nonzero residuals require an explicit basis for independence from each other and from pixel error.

## Scenarios

Edit `scenario-authoring-v2`. See [authoring.py](../src/chongming/scenarios/authoring.py) for accepted fields and [scene contracts](../src/chongming/contracts/scene.py) for compiled types.

| Example                                                          | Purpose                                           |
| ---------------------------------------------------------------- | ------------------------------------------------- |
| `scenarios/center_clean.json`                                    | Two-station, single-target center test            |
| `scenarios/empty.json`, `scenarios/constant_velocity.json`       | Empty scene and straight target motion            |
| `scenarios/review.json`, `scenarios/photography.json`            | Preview and visual effects                        |
| `scenarios/heightfield.json`, `scenarios/heightfield_asset.json` | Inline and external height grids                  |
| `scenarios/osm.json`, `scenarios/osm_landscape.json`             | Synthetic map fixtures                            |
| `scenarios/truck_town.json`                                      | External Godot scene; requires a separate project |
| `scenarios/aircraft_model_test.json`                             | Bundled aircraft geometry                         |

A name such as `center_clean` does not establish a clean image background. Omitted fields can retain demo targets, objects, vegetation, or wind. Use empty arrays and `wind: null` when those elements must be absent.

| Field                                                 | Meaning                                                                             |
| ----------------------------------------------------- | ----------------------------------------------------------------------------------- |
| `format`, `scenario_id`                               | Format and scene identity                                                           |
| `random_seed`                                         | Fixed at 42; may be omitted                                                         |
| `duration_s`, `fps`                                   | Positive duration; default 24 s and 30 Hz                                           |
| `stations`                                            | One to four unique IDs, ENU positions, optional yaw/elevation, rate, and start time |
| `targets`                                             | Unique IDs, geometry, and motion                                                    |
| `world_objects`, `vegetation`, `wind`                 | Scene objects and wind settings                                                     |
| `terrain`                                             | Flat ground or a heightfield                                                        |
| `search`                                              | ENU minimum, maximum, and voxel step                                                |
| `environment`, `clouds`, `camera_jitter`, `rendering` | Supported visual effects and pose perturbations                                     |
| `camera_position_offset_enu_m`                        | Real camera displacement; does not change online installation estimates             |
| `occlusion_mode`                                      | `known` for geometric occlusion, or `unknown` for field-of-view checks only         |
| `osm`                                                 | Local landscape reference and optional vegetation policy                            |

Positions are east, north, up in meters. Yaw is zero north and positive east. Main images are 1920 by 1080; auxiliary images are 640 by 480. Station rates are 1 through 60 Hz. Use `--check` after editing.

### Target geometry and motion

Authoring accepts spheres (`radius_m`), boxes (`half_extents_m`), and aircraft. Aircraft use an internal mesh or `model: "f86"` / `"la11"`; arbitrary model paths are not accepted. Python reads bundled GLB geometry for both logic and rendering. Target appearance replaces GLB materials and textures.

A complete constant-velocity motion value is:

```json
{
  "type": "constant_velocity",
  "position_enu_m": [-15, 62, 20],
  "velocity_enu_mps": [1, 0, 0]
}
```

An orbit uses `center_enu_m`, positive `radius_m` and `period_s`, and optional `phase_rad` and `vertical_amplitude_m`. With phase `p = phase_rad + 2*pi*t/period_s`, position is:

```text
E = center_E + radius * sin(p)
N = center_N + radius * cos(p)
U = center_U + vertical_amplitude * sin(2*p)
```

The preview editor supports basic position and constant-velocity changes. Edit orbit parameters in JSON.

### Local assets

JSON asset references use a local URI and a content digest:

```json
{
  "uri": "assets/ridge.json",
  "content_sha256": "6744c8b5dd9c1494b5828de16938651560686bd7b9e155a1a3fd73acfce4bd03"
}
```

Relative paths resolve from the scenario directory. Absolute paths and host-free `file:` URIs are accepted; remote URLs, queries, and fragments are not.

Use the project's content hash:

```sh
uv run --frozen python scripts/hash_json.py scenarios/assets/ridge.json
```

[json_content.py](../src/chongming/scenarios/json_content.py) defines compact UTF-8 JSON with sorted object keys. Array order and string code points remain unchanged. Equivalent integer notation and signed zero normalize to one value. Integers must fit `±(2**53 - 1)`; decimal values must survive the specified binary64 conversion and spelling rule. Duplicate keys, non-finite numbers, precision loss, and invalid Unicode are rejected. Formatting alone does not change the content hash. Source downloads, archives, images, and editor conflict checks use byte hashes for different purposes.

### OSM and elevation

`chongming-osm` reads fixed local OSM XML 0.6; `--source-url` records provenance. Convert PBF before import and retain the source hash and conversion command.

For the bundled synthetic fixture, choose fresh output paths:

```sh
uv run --frozen chongming-osm scenarios/assets/osm_demo.osm \
  --config scenarios/assets/osm_import.json \
  --source-url 'repository:scenarios/assets/osm_demo.osm (synthetic fixture)' \
  --data-date 2026-10-20 --license CC0-1.0 \
  --attribution 'Synthetic fixture, CC0-1.0; not an OpenStreetMap extract' \
  --output output/osm/demo.buildings.json --cleaned-output output/osm/demo.cleaned.osm
```

The report includes the imported content hash, input byte hashes, diagnostics, and counts. Put its content hash and asset path in `osm.asset`.

The importer projects WGS84 through ECEF into local ENU. It supports simple building extrusion, roads, tree points, and selected land-cover polygons. It reports unsupported buildings, bridges, tunnels, and missing relation members.

Optional HGT elevation needs `--dem-hgt`, tile name, source URL/date, license, and attribution. Supported grids are 1201 or 3601 samples per side, with EGM96 heights.

### Fixed test assets

The flake pins the Hannover asset archive by URL and byte hash. It is not part of the source tree. To prepare it, use a new archive link and an absent extraction directory:

```sh
mkdir -p tmp
nix build --no-update-lock-file .#hannover-500m --out-link tmp/hannover-500m-archive
tar -xzf tmp/hannover-500m-archive -C scenarios/assets
```

Use `scenarios/external/hannover.json` with the extracted asset. The directory is ignored by Git. Root-level Hannover experiment scenarios may reference other local paths; check each before use.

The preview option `--landscape-asset /path/to/landscape-dem.json` is for a fixed Hannover editor preset, not arbitrary maps. It checks a pinned content digest. Use `osm.asset` for other landscapes.

Keep original downloads, import reports, commands, and license records. `scripts/pack_local_asset.py` builds deterministic archives. Publishing an asset or updating a pinned flake input requires separate maintainer approval.

### External Godot scenes

Prepare and pin an external project before use. For the [Truck Town demo](https://github.com/godotengine/godot-demo-projects/tree/master/3d/truck_town):

```sh
CHONGMING_GODOT_EXTERNAL_PROJECT=/path/to/godot-demo-projects/3d/truck_town \
CHONGMING_GODOT_EXTERNAL_SCENE=res://town/model/town_model.gltf \
uv run --frozen chongming-scene --scenario scenarios/truck_town.json \
  --output results/truck-town
```

Both variables are required. The worker copies resources to a temporary project and imports them; it does not run the external project's UI or vehicle logic. Record the external commit and asset hashes. `--check` does not validate resource import.

External geometry does not supply Python occlusion truth. The example uses `unknown` occlusion; comparisons are limited to the same fixed recording and its known conditions.

## Algorithm development

[Source layout](design.md#source-layout) lists module ownership. `ports.development.CenterAlgorithm` defines the center plugin:

| Method                | Contract                                                                      |
| --------------------- | ----------------------------------------------------------------------------- |
| `start(registration)` | Register stations, center session, policy, clock mapping, and start time      |
| `process(step)`       | Consume solutions, received events, completion progress, and finite-end state |
| `close()`             | Release resources; allow repeated cleanup                                     |

Pass a trusted no-argument factory with `--algorithm module:factory`. Return `CenterAlgorithmResult` with `measurements`, `tracks`, and full stage `batches`, not a tuple of two lists. Register each step's solutions before processing events that refer to them, as the reference adapter does. Keep truth and generation settings out of online inputs.

`reference.create_algorithm()` is the reference factory. `FusionEngineAdapter` wraps an existing engine. The same plugin can run in modes 3 and 4. Voxel display is specific to the reference plugin.

`composition/center.py::reference_policy()` owns default thresholds. It uses at least two stations, coarse voxels, least squares, and track-based motion alignment. New targets require exposure midpoints within 1 ms. Window width comes from the shortest configured camera period. Change detailed policies through Python assembly and save the actual policy with the run; authoring `search` only controls the search volume and step.

The `vision/` frame-difference pipeline is separate from the MOG2 device implementation. Its OpenCV tuning windows are unavailable with the default headless OpenCV dependency.

## Evidence and playback

Experiment output directories must not exist. Keep one directory per run.

| File                                                                    | Content                                                    |
| ----------------------------------------------------------------------- | ---------------------------------------------------------- |
| `authoring.json`                                                        | Input authoring document when saved by the entry point     |
| `scenario.json`                                                         | Compiled diagnostic scene in finite scene exports          |
| `manifest.json`                                                         | Mode, actual policy, sessions, renderer, and provenance    |
| `online.jsonl`                                                          | Ordinary frame metadata, events, and skipped-image records |
| `truth.jsonl`, `truth-static.json`                                      | Privileged truth and shared scene fields                   |
| `timing.jsonl`, `summary.json`                                          | Stage timing and final state                               |
| `edge-output.jsonl`                                                     | Modes 2/4: pixel hashes and actual device results          |
| `center-registration.json`, `center-input.jsonl`, `center-output.jsonl` | Modes 3/4: registered configuration, inputs, and results   |
| `video/camera-*.mp4`, `video.json`                                      | Optional encoded video and stream metadata                 |

`complete` means the finite run ended, not that localization succeeded. `algorithms_complete: false` records unfinished general system components. Check `status == "located"` before using a measurement position. Top-level measurements and tracks also appear in stage batches.

Use stage reason codes to diagnose missing output: warmup, absent calibration, unknown ray error, insufficient stations, motion alignment, association ambiguity, search truncation, or geometric rejection. Do not lower gates merely to obtain positions.

The evidence writer stores stable fields once and stores changed fields inline. Use `read_truth_records()` in `scenarios/run.py` to restore snapshots and verify static references.

Export and preview a recording:

```sh
uv run --frozen chongming-scene --scenario scenarios/empty.json --output results/recording-demo
uv run --frozen chongming-preview --recording results/recording-demo --playback-fps 30
```

Keep the authoring file, referenced assets, online/truth/timing files, static truth, video files, and `video.json`. `--recording` excludes `--scenario` and camera-offset editing. Playback does not restore complete center events, still starts Godot, and can switch to live generation at the end. `chongming-center` has no recording input option.

For a run with no video export, add `--no-video` to `chongming-scene`. `--render-every N` samples pixels from each camera's exposure sequence without dropping logical exposures.

## Checks

In the development shell:

```sh
ruff check src tests scripts
ruff format --check src tests scripts
uv run --frozen mypy src tests
uv run --frozen python -m unittest tests.test_architecture tests.test_distribution -v
for scenario in scenarios/*.json; do
  uv run --frozen chongming-center --scenario "$scenario" --check
done
```

The scenario loop reports missing local assets; it does not download or repair them. `scenarios/external/` needs separate asset preparation.

Run relevant component tests first. Pure center tests include `tests.test_fusion_ingress`, `test_fusion_geometry`, `test_fusion_matching`, `test_fusion_motion`, `test_fusion_uncertainty`, and `test_fusion_tracking`. Device tests include `tests.test_detection`, `test_device_pipeline`, and `test_device_rays`.

Full regression needs Godot and graphics:

```sh
nix develop --no-update-lock-file .#software --command xvfb-run -a uv run --frozen python \
  -m unittest discover -s tests -v
```

Godot tests cover actual rendering, update acceptance, integer precision, layer isolation, and diagnostics. Configuration checks and mock tests do not replace them. Report failures and untested paths for the current revision.

### Evaluation and benchmarks

[Algorithm evaluation](algorithm-evaluation.md) documents the native tuning workbench, explicit device policies, saved-input center replay, model-size scoring and operational attribution. Reuse existing recordings and new output directories. The [2026-11-02 benchmark](benchmark-2026-11-02.md) records the selected frame-difference policy and three-station integrated results, including device and center timing. [Visualization monitoring](visualization-monitoring.md) separates rendering and recording costs from algorithm costs.

### Verification of the visualization and evaluation change

The 2026-11-02 delivery passed 211 related device, center, replay, architecture and Godot visualization tests, full Ruff checks, and mypy for all 148 source/test files. All top-level scenario configuration checks passed. These checks do not rerun the recorded quality benchmark.

Full discovery was interrupted while `test_editor_updates_native_view_without_sensor_frames_and_cancel_restores_state` waited on `PreviewRuntime._encode_queue_lock`. Full regression is therefore not reported as passing. Four other failures were reproduced in an isolated copy of upstream revision `855d126`:

- `test_kalman_update_matches_known_scalar_gain_and_joseph_covariance`: existing state expectation differs from the current variance-floor behavior.
- `test_native_generator_and_worker_cache`: the vegetation fixture calls an uninitialized instancing helper.
- `test_read_only_package_project_uses_a_separate_working_directory`: package project path expectation differs from the writable project copy.
- `test_explicit_landscape_and_scenario_paths_survive_a_working_directory_change`: macOS `/tmp` versus resolved `/private/tmp` path mismatch.

These remain unresolved. Assertions and runtime thresholds were not relaxed to hide them.

### Formatting

`nix fmt` uses treefmt with nixfmt, Ruff, and Prettier. Limit formatting to changed files when possible:

```sh
nix fmt -- README.md AGENTS.md docs/introduction.md docs/design.md docs/development.md
```

The CI command is:

```sh
nix develop --no-update-lock-file .#ci --command treefmt --no-cache --fail-on-change
```

This command writes changes and fails if formatting changed a file; it is not read-only. Review the diff. Lock files, generated output, and Godot source files are outside the configured formatters.

### Performance experiments

Use `scripts/benchmark_scene.py` or the `review_*` scripts with separate output paths. Record the scene, assets, resolution, code and dependency versions, hardware, driver, renderer, and cold/warm samples.

`gpu_ms` is worker wall time for scene updates, drawing, synchronization, and readback. It is not a hardware GPU timer. `gpu_submissions` counts pixel requests. Check image correctness before comparing throughput.

## Local release

The release tool builds local files only. It does not publish them.

```sh
uv run --frozen python scripts/distribute.py build --output dist/release
uv run --frozen python scripts/distribute.py check --output dist/release --render
```

Use a dedicated directory containing only this version's expected artifacts:

- Source archive (`sdist`).
- Wheel built from that source archive.
- User archive with the documentation, scenarios, sdist, wheel, and requirements.
- Hashed runtime `requirements.txt`.
- `SHA256SUMS` for the other four files.

Build stages are `sdist`, `wheel`, and `bundle`. Resume only from the same source snapshot. Check stages are `contents`, `install`, and `all`; the last also rebuilds and compares wheel contents.

Checks validate paths, resources, license, entry points, hashes, and installation in an isolated environment and working directory. `--render` adds real rendering probes and needs graphics. Full mode 2/4 behavior remains the job of integration tests.

Do not include local assets, experiment output, caches, or secrets in a release. Run the relevant checks after changing document paths or package resources.

## Third-party assets and licenses

Code licensing is stated in the [README](../README.md#license-and-use). Dependency and asset licenses are separate. Keep source URLs, versions or dates, byte hashes, conversion commands, and license terms with external inputs. Check permission before distribution.

### Plants

The bundled [Shapespark Low-Poly Exterior Plants Kit](https://github.com/shapespark/shapespark-assets/tree/main/shapespark-low-poly-plants-kit) uses CC0 1.0 Universal. The [license text](../src/chongming/rendering/godot/assets/CC0-1.0.txt) remains in the package. Attribution is not required by CC0; the project records the source for provenance.

The unmodified upstream `shapespark_low_poly_plants.gltf` has SHA-256:

```text
ac33eec69ab0ace22817af29a40a8cf3a4bdf5e560ffdd060a5cd9c4b9b7a2b7
```

The renderer loads embedded meshes and textures, converts card transparency to alpha scissor, and separates trunk and crown surfaces for wind deformation. It maps 30 plant nodes to 12 tree, 6 shrub, and 12 grass variants. Placement and variant selection are deterministic.

`assets/impostors/Tree-*.png` are billboards rendered from the same kit. To regenerate them in a graphics session:

```sh
"$CHONGMING_GODOT" --path src/chongming/rendering/godot \
  --script tools/generate_vegetation_impostors.gd \
  --rendering-method gl_compatibility --audio-driver Dummy
```

This operation updates generated plant images.

### Maps and elevation

Bundled `osm_demo.osm`, `osm_landscape_demo.osm`, and their synthetic assets are CC0 fixtures.

Real OSM data generally uses [ODbL 1.0](https://opendatacommons.org/licenses/odbl/1-0/). Preserve [OpenStreetMap contributors attribution](https://www.openstreetmap.org/copyright) and follow the [OSMF attribution guidelines](https://osmfoundation.org/wiki/Licence/Attribution_Guidelines).

For Mapzen/Skadi elevation, retain the [format](https://github.com/tilezen/joerd/blob/master/docs/formats.md) and [source attribution](https://github.com/tilezen/joerd/blob/master/docs/attribution.md) records. The Hannover DEM carries:

> Mapzen terrain tiles; Europe terrain produced using Copernicus data and information funded by the European Union - EU-DEM layers; global GMTED2010 and SRTM courtesy of USGS.

Do not label all mixed-source elevation as public-domain SRTM. The Hannover archive distributes OSM-derived data under ODbL and retains the DEM provider terms.

Godot demo project code uses MIT, but individual models and textures can have their own terms. Check their license files before using or distributing an external scene.
