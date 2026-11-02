# Algorithm evaluation

This guide defines offline device, center, and integrated scoring. The current measured results are in the [2026-11-02 benchmark](benchmark-2026-11-02.md). Renderer and recording timing use a separate [performance report](visualization-monitoring.md).

## Evidence and responsibility

Reuse `results/hannover_vegetation_test_30s`. Replays do not start Godot or record new images. Keep each experiment in a new output directory; existing output paths are rejected.

Truth is used only by offline scoring and display. It must not enter the device pipeline, ray generation, or center algorithm. Recording truth includes target projections and field-of-view membership; this experiment does not establish occlusion. Responsibility uses target-center FOV membership, without an occlusion exemption.

Device responsibility requires one main camera's FOV. Center responsibility defaults to at least two distinct main stations. `--minimum-stations 3` changes center scoring responsibility to three stations, independently of actual detections. Missing detections must remain in the recall denominator.

Evaluation uses exact exposure/reference times, including windows with no output. It does not substitute sealed-window midpoints for actual exposures. Center evaluation currently requires one synchronous main exposure time per sealed window.

## Device pipeline

The workbench and `WorkbenchMotionModule` share preprocessing, foreground extraction, morphology, optional occupancy filtering, component extraction, and Kalman tracking. Frame-difference policies use `algorithm_id=frame-difference-occupancy-v1`; MOG2 uses `mog2-workbench-v1`. Select these policies explicitly for modes 2 and 4. The legacy MOG2 detection module remains a separate reference implementation.

The selected device configuration is [Hannover frame difference](../scenarios/policies/hannover_vision_inc26_dilate15.json). It is an explicit experiment policy, not a replacement for global defaults. Slider `Score Inc=26` means `score_increment=2.6`; `Score Decay=1` means `score_decay=0.1`; `Occ Dilate=15` sets occupancy dilation.

The first 15 seconds after each station's first main exposure are adaptive warmup. Frames still train the pipeline but do not contribute to quality metrics. This recording has 900 main frames per station, 451 excluded warmup frames and 449 scored frames.

### Pixel matching

With `--size-k 0.5 --gate-px 10`, a device output matches within:

```text
gate_px = max(10, 0.5 * projected_AABB_diagonal_px)
```

Projection bounds come from recorded model geometry clipped to the image. This uses a length, not pixel area. It allows a signal on a wing tip to match a large nearby aircraft. Missing valid projection dimensions reject dynamic scoring rather than silently using the fixed gate. Omitting `--size-k` selects the fixed pixel gate.

Each frame uses maximum-cardinality one-to-one assignment, then minimum total error. Repeated outputs near the same target count as duplicates. Outputs far from all responsible targets count separately as false alarms and contribute to precision's false-positive count. Metrics are recall and precision; error distributions are not the acceptance criterion.

```sh
nix develop --no-update-lock-file --command uv run --frozen python -m chongming.evaluate_recording \
  results/hannover_vegetation_test_30s \
  --policy scenarios/policies/hannover_vision_inc26_dilate15.json \
  --size-k 0.5 --gate-px 10 --output results/device-example
```

The report includes aggregate and station metrics, per-target recall, policies, source provenance and per-frame evidence. Original-image coordinates are restored after downsampling.

### Native workbench preview

Use `python -m chongming.vision.interactive_tuning VIDEO` inside the Nix/uv environment. Add `--recording`, `--policy`, and `--station` to load recorded scoring evidence and an explicit station policy. `--size-k` and `--gate-px` control the overlay gate. A native OpenCV window shows matched, missed, duplicate and false-alarm evidence alongside current scores; it does not generate HTML.

Controls:

- Space pauses; A/D step backward/forward; Q or Escape exits.
- Mode 0/1/2 selects two-/three-/five-frame difference. Mode 3 selects MOG2.
- `Use Occupancy` enables occupancy filtering independently of foreground extraction.
- `Score k x100` sets the projected-size multiplier; `Score Floor px` sets its lower bound.
- Changing algorithm parameters or seeking backward replays training history. Paused display refreshes do not repeatedly train or count a frame.
- Add `--export-directory DIR`; S writes a new `workbench-policy-NNN.json` snapshot without replacing an existing experiment. Policy export also requires `--policy` and `--station`.

The earlier offline HTML diagnostic remains available through `chongming.evaluation_preview`, but the tuning workflow uses the native workbench.

### MOG2 control group

MOG2 learns a mixture of Gaussian background components for each pixel. It produces a foreground mask, not object classes. Persistent or slow targets can become background. See the [OpenCV API](https://docs.opencv.org/4.1.0/d7/d7b/classcv_1_1BackgroundSubtractorMOG2.html).

The separate MOG2 window controls history, variance threshold, learning rate, and shadow detection. `MOG LR x10000=0` means automatic learning; n>=1 means `(n-1)/10000`. Only mask value 255 is foreground. The frame-difference `Threshold` slider does not control MOG2.

The [manually tuned MOG2 policy](../scenarios/policies/hannover_mog2_occupancy_tuned_control.json) uses history 500, variance threshold 16, automatic learning, no shadow classification, occupancy enabled, and blob dilation 3. The selected frame-difference policy uses blob dilation 5. Therefore these are practical candidate configurations, not a single-factor ablation.

The final device scores were 95.33% recall / 96.60% precision for frame difference and 77.21% / 91.17% for tuned MOG2. MOG2 remains a workbench/offline control and is not selected for the current integrated configuration.

### Device timing

Per-frame records separate detection, tracking, their sum `pipeline_ms`, process CPU time, video decoding, conversion, and individual detection stages. Stage timings are nested in detection and must not be added again to the pipeline total. Reports include mean, median, nearest-rank P95, maximum and sample count, both including and excluding warmup.

Algorithm throughput excludes decode, conversion, scoring, UI and disk. CPU time does not measure energy. `--opencv-threads N` requests a thread count; check the saved report's actual `environment.opencv_threads`. The experiments reported here recorded 10 OpenCV threads, correcting an earlier description as single-threaded.

See [device performance and embedded interpretation](benchmark-2026-11-02.md#device-timing-and-embedded-interpretation) for phase costs and the difference between offline decoding and direct camera input.

## Center scoring

Center tests replay ordinary ideal virtual rays and known calibration. This isolates fusion from actual image detection; it is not an integrated quality score.

Let D be the physical model AABB diagonal in body coordinates, after asset scaling:

- Precise hit: error <=0.7D.
- Intermediate: 0.7D<error<3D.
- Completely off target: error >=3D.

There is no 10 m floor. The voxel step and continuous-position accuracy are separate quantities. Precise and broad gates have independent one-to-one assignments. An extra output near a matched target remains a duplicate false positive. Outputs near nonresponsible targets are excluded from precise scoring only within the precise gate; broad diagnostics use their broad gate.

```sh
nix develop --no-update-lock-file --command uv run --frozen python -m chongming.replay_center \
  results/hannover_vegetation_test_30s --asset-root scenarios \
  --postintersection --output results/center-ideal-example
nix develop --no-update-lock-file --command uv run --frozen python -m chongming.evaluate_center \
  results/center-ideal-example --asset-root scenarios --size-k 0.7 --miss-k 3 \
  --output results/center-ideal-score-example
```

Ideal center testing scores from the first frame by default, without device warmup exclusion.

### Exhaustive intersection diagnostics

`chongming.diagnose_center` enumerates all independent-station subsets and ray combinations within saved voxel candidates, one ray per station, deduplicating identical event-ID groups. It bypasses exclusive assignment but does not invent support outside the saved candidates.

Each group retains the raw unweighted fit, solver rank and conditioning, residuals, forward depths, production geometry result, and rejection classifications. A nonunique fit has no position. Raw rejected intersections are not production measurements.

```sh
nix develop --no-update-lock-file --command uv run --frozen python -m chongming.diagnose_center \
  results/center-ideal-hannover-20261102 --output results/center-diagnostic-example
nix develop --no-update-lock-file --command uv run --frozen python -m chongming.evaluate_center \
  results/center-diagnostic-example --asset-root scenarios --size-k 0.7 --miss-k 3 \
  --diagnostic-raw --output results/center-raw-score-example
```

`--diagnostic-raw` explicitly scores raw solutions. Without it, only located geometry results count.

### Post-intersection pipeline

Select `--algorithm chongming.reference:create_postintersection_algorithm` for live center modes, or `--postintersection` in ideal replay. The legacy factory stays available for comparison.

All candidate station subsets enter geometry without exclusive search truncation. Located outputs are sorted by independent station count, maximum residual, condition number and stable ID. Complete-link spatial clusters have pairwise distances <=3 m. Retain an original representative, without averaging positions or refitting rays. The threshold is conservative relative to the 10 m voxel step; it can still merge nearby real targets.

`FusionBatchResult.suppressed_measurements` preserves removed measurements, representative IDs, distances and `duplicate_location` reasons. Spatially distinct outputs can still reuse a ray. Only tracking input is filtered for exclusive evidence; `tracking_exclusions` retains `shared_ray_conflict` records. All fusion outputs remain visible to scoring.

The original ideal-input run scored 41.40% recall / 100% precision, with association search truncation in 507/900 windows. Exhaustive production geometry recovered all 6137 responsible target-times but produced 15534 duplicate outputs. Post-intersection deduplication scored 100% / 100% on the same ideal data. These results do not imply perfect quality with real image detections.

## Integrated replay and attribution

`chongming.replay_integrated` exports actual image `Observation2D` results and uses the regular ray generator and post-intersection center. The device export interface receives images and observations, not truth labels. Calibration references are rebound to image sessions; auxiliary calibration recognition is not evaluated.

The experiment explicitly assumes isotropic original-image pixel standard deviation 5 px. It is not a measured covariance. Residual jitter/flex is zero only under this recording's unperturbed camera assumptions. Warmup observations are withheld from the center, while calibration and progress continue.

```sh
nix develop --no-update-lock-file --command uv run --frozen python -m chongming.replay_integrated \
  results/hannover_vegetation_test_30s --asset-root scenarios \
  --policy scenarios/policies/hannover_vision_inc26_dilate15.json --pixel-sigma-px 5 \
  --output results/integrated-example
nix develop --no-update-lock-file --command uv run --frozen python -m chongming.evaluate_center \
  results/integrated-example/center --asset-root scenarios --size-k 0.7 --miss-k 3 \
  --warmup-s 15 --warmup-origin-ns 1000000 --output results/integrated-example/score
nix develop --no-update-lock-file --command uv run --frozen python -m chongming.integrated_attribution \
  results/integrated-example/device results/integrated-example/center results/integrated-example/score \
  --output results/integrated-example/attribution
```

Use the same warmup origin for device and center. Mixing scene epoch zero with first exposure 1000000 ns changes the boundary by one frame. Performance attribution selects actual scored windows, not the incoming progress step's warmup flag.

Offline attribution labels actual emitted/admitted rays using device scoring. Duplicate detections may retain the same target label but never add an independent station. Misses are classified by insufficient correct emitting stations, ingress rejection, inability of correct raw ray groups to meet the gate, geometry rejection, candidate omission, or spatial suppression. Extra outputs are classified as duplicates, false-input propagation, cross-target groups, or same-target localization error. This is operational primary ownership, not proof that only one physical cause contributed. Detailed evidence retains rays, groups and suppressed solutions.

Per-window center timing separates candidate generation, association, geometry, deduplication and tracking. `center_process_ms` also includes ingress/progress overhead. The sum of separately measured device and center computation is not physical end-to-end latency.

### Three-station comparison

Two stations and two targets in or near one plane can produce valid cross-target intersections. Each ghost must also pass forward-depth, search and residual checks. A third station adds a constraint, but does not guarantee removal of every ambiguity.

Reuse saved image rays and calibration without rerunning device detection:

```sh
nix develop --no-update-lock-file --command uv run --frozen python -m chongming.replay_center_inputs \
  results/integrated-hannover-20261102/center-final --minimum-stations 3 \
  --output results/three-station-example
nix develop --no-update-lock-file --command uv run --frozen python -m chongming.evaluate_center \
  results/three-station-example --asset-root scenarios --size-k 0.7 --miss-k 3 \
  --minimum-stations 3 --warmup-s 15 --warmup-origin-ns 1000000 \
  --output results/three-station-score-example
```

The replay sets minimum voxel station count, candidate score, association group size and geometry support to three. It does not fall back to two-ray groups. Evaluation responsibility is independently set to three-station FOV coverage, not observed ray count.

On 449 windows and 2760 three-station target-times, two-station intersection scored TP=2735, FN=25, FP=2640; three-station intersection scored TP=2467, FN=293, FP=37. Recall changed from 99.09% to 89.38%, precision from 50.88% to 98.52%. The [benchmark](benchmark-2026-11-02.md) retains per-target results, attribution, measured costs, local evidence paths and limitations. Do not infer generalization or real-time capability from this single tuned recording.
