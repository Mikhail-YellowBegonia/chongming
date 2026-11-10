# Issue #51: physical difficulty, official ViBe and regression, 2026-11-10

This report extends the [center and device attribution results](benchmark-2026-11-10.md). It uses the same three-station Hannover 30-second recording. No new images are rendered for the quality comparison. Published paper-score comparisons remain deferred.

Official ViBe with occupancy and no erosion averages **73.90% recall, 93.07% precision and 82.39% F1** over three seeds. Selected frame difference scores **95.33% / 96.60% / 95.96%**, and tuned MOG2 scores **77.21% / 91.17% / 83.61%**. Unfiltered ViBe exceeds the candidate budget in all three full-input attempts. These results describe the declared configurations on this tuned recording.

## Comparison context

Every completed device trial processes 900 main frames per station. Training consumes all frames. The first 15 seconds are excluded from quality: 451 warmup and 449 scored frames per station, with 9134 responsible target-times. Responsibility and matching retain the existing target-center FOV rule and `max(10 px, 0.5 * projected AABB diagonal)` gate. Occlusion is unknown. Failed or short trials have no full-input quality score.

The new ViBe baseline uses unmodified official C source at `da0c197d36272125589180e20fdd32b6fe3ba548`. Its current defaults are 30 samples, threshold 10, two matches and update factor 8. The [official source](https://github.com/vandroogenbroeckmarc/vibe/tree/da0c197d36272125589180e20fdd32b6fe3ba548) and its current Apache-2.0 license are archived locally with source hashes. This is a new verified baseline; the project's historical ViBe implementation remains unrecovered. It is not assigned scores from these new trials.

All methods share downsampling, area filtering, tracking and scoring. Opening and closing are disabled. ViBe and MOG2 use blob dilation 3; the selected frame-difference policy uses 5. Independent erosion, where enabled, is one 3-by-3 elliptical operation on the downsampled image. Occupancy-mask dilation is 15. These are practical configurations, with separately named fixed-factor occupancy and erosion comparisons. They are not equally optimized foreground operators.

The official C kernel is sequential. OpenCV is fixed to 10 threads, with OpenBLAS and Accelerate limited to one thread. Seeds 42, 43 and 44 are declared before running. Each process seeds libc once and consumes station frames in recorded order. Three repeats rotate method order and run serially. Controls are deterministic; ViBe repeats measure random variation as well as timing variation. Timings cover detection plus actual tracking, including warmup, and exclude decoding, scoring and writes. Foreground-stage times are subsets of detection. These CPU measurements do not establish energy use or target-hardware performance.

The environment is macOS 26.6 arm64, Python 3.12.13 and OpenCV 4.14.0 under Nix/uv frozen. Hardware inspected after the batch is Apple M4, 10 physical cores and 24 GiB RAM. No separate frequency, thermal or background-load control is claimed.

The same predeclared limits apply: 100 candidates before tracking after the initial 10-second learning hold, 2 seconds per detection/tracking phase, 4 GiB worker RSS, and 300/1800 seconds of probe/full wall time. Every ViBe configuration first receives 360 frames per station, then full input if feasible. A short probe does not guarantee later feasibility.

## Completed quality and feasibility

Values are arithmetic means of three complete trials. The ± value is sample standard deviation in percentage points across those trials, not a confidence interval. Control quality is identical in all repeats. Each complete trial has the same 9134-target-time denominator.

| Method                    | Occupancy | Erosion | Complete full trials | Recall        | Precision     | F1            |
| ------------------------- | --------- | ------- | -------------------- | ------------- | ------------- | ------------- |
| Selected frame difference | on        | off     | 3/3                  | 95.33%        | 96.60%        | 95.96%        |
| Tuned MOG2                | on        | off     | 3/3                  | 77.21%        | 91.17%        | 83.61%        |
| Official ViBe             | off       | off     | 0/3                  | unavailable   | unavailable   | unavailable   |
| Official ViBe             | on        | off     | 3/3                  | 73.90 ± 0.15% | 93.07 ± 0.25% | 82.39 ± 0.08% |
| Official ViBe             | off       | on      | 3/3                  | 53.24 ± 0.43% | 65.54 ± 0.22% | 58.75 ± 0.21% |
| Official ViBe             | on        | on      | 3/3                  | 51.88 ± 0.33% | 92.48 ± 0.40% | 66.47 ± 0.25% |

Frame difference has TP/FP/FN = 8707/306/427; MOG2 has 7052/683/2082 in every repeat. ViBe with occupancy and no erosion has TP 6741–6766, FP 481–520 and FN 2368–2393. All per-seed counts and scores are retained in the machine-readable summary.

The batch contains 30 attempts: 12 completed short probes, 15 complete full-input trials and three full-input candidate-budget stops. The unfiltered stops are all at station east: seed 42, frame 384, 12.801 s and 103 candidates; seed 43, frame 362, 12.068 s and 101 candidates; seed 44, frame 376, 12.534 s and 101 candidates. Frame indices are zero-based. The failed summaries retain null quality, candidate records, processed counts and the last valid frame. All probes passed, demonstrating why their feasibility must not be extrapolated to the full recording.

At fixed erosion on, occupancy increases ViBe precision from 65.54% to 92.48%, while recall falls from 53.24% to 51.88%. At fixed occupancy on, erosion lowers recall from 73.90% to 51.88% and does not improve precision. Occupancy permits full-input operation without erosion here; erosion permits unfiltered operation but removes true signals. No configuration is declared universally optimal. In particular, the new default ViBe baseline has not received the scene-specific tuning used by the selected controls.

## Repeated runtime

Each value is the mean ± sample SD of three run means, in ms per station-frame including warmup. RSS is the maximum observed worker peak across those runs, including decoder and native allocations.

| Method                       | Pipeline ms/frame | Foreground-stage ms/frame | Maximum worker RSS, MiB |
| ---------------------------- | ----------------- | ------------------------- | ----------------------- |
| Frame difference + occupancy | 3.727 ± 0.039     | 0.111 ± 0.002             | 614.4                   |
| MOG2 + occupancy             | 4.658 ± 0.074     | 1.208 ± 0.024             | 702.8                   |
| ViBe + occupancy             | 4.430 ± 0.037     | 1.137 ± 0.008             | 658.7                   |
| ViBe + erosion               | 2.426 ± 0.044     | 1.132 ± 0.010             | 640.6                   |
| ViBe + occupancy + erosion   | 4.308 ± 0.021     | 1.128 ± 0.004             | 655.9                   |

ViBe's foreground stage is slightly faster than MOG2 here. The eroded configuration without occupancy has the lowest pipeline time and much lower quality. Speed alone does not establish a preferable detector. Earlier report timings remain historical measurements; these controls were repeated with this batch rather than substituted from another source revision.

## Physical difficulty

The confirmed descriptor is camera-to-target Euclidean distance divided by the physical model body-space AABB diagonal after asset scaling. Its [Issue #51 definition](https://github.com/Mikhail-YellowBegonia/chongming/issues/51#issuecomment-6760259305) records the change from projected pixel size. One ratio is retained per main station, target and exposure. It never enters online processing and does not change matching gates.

Bins are fixed powers of two, lower inclusive and upper exclusive. Counts pool station-exposure samples after computing their individual ratios; distances are not averaged across stations. Per-exposure evidence retains station identity, range and model size. Unmatched predictions have no known target size, so precision remains global. These correlated samples describe the observed recording; they are not independent trials of an ultimate detection range. Motion, target appearance, orientation and background vary across bins, so empirical recall need not decrease monotonically.

The original four completed device configurations were also stratified in [difficulty-20261110-01](../results/difficulty-20261110-01/summary.json). Their stratified counts exactly recover the original aggregate scores.

The observed scored ratios span **12.421–213.945**. Physical model diagonals and source hashes are frozen in [definition.json](../results/issue51-completion-summary-20261110-01/definition.json). Each of the 15 completed new trials reproduces its original TP, FP and responsible-sample totals after stratification.

| Distance / model diagonal | Responsible target-times per trial | Frame difference recall | MOG2 recall | ViBe + occupancy mean recall |
| ------------------------- | ---------------------------------- | ----------------------- | ----------- | ---------------------------- |
| [8, 16)                   | 357                                | 100.00%                 | 100.00%     | 96.17%                       |
| [16, 32)                  | 2059                               | 98.83%                  | 89.66%      | 79.20%                       |
| [32, 64)                  | 2315                               | 93.35%                  | 79.48%      | 75.00%                       |
| [64, 128)                 | 1982                               | 93.24%                  | 66.45%      | 66.83%                       |
| [128, 256)                | 2421                               | 95.25%                  | 69.89%      | 70.85%                       |

For the specific `extreme-far-small` target, with 1347 responsible station-exposures per trial, frame difference recall is 99.18%, MOG2 97.25%, and ViBe with occupancy 93.49% on average. ViBe recall falls to 17.42% with erosion alone and 17.67% with occupancy plus erosion. This is evidence of erosion damage to that recorded small signal. The ratio strata show an advantage for the selected frame-difference configuration in the largest observed bin, while the non-monotonic results and mixed target populations prevent interpreting the bins as a controlled distance sweep.

## Software confidence: A1/A2/A3

Normal discovery ran without failfast and completed **582 tests in 112.620 seconds: 570 passed, 12 failed, no errors or skips**. Optional native ViBe tests used the pinned compiled library. The [complete log](../results/issue51-completion-20261110-02/full-regression.log) retains all results, including tests beyond the earlier 256-test stopping point. This is not a passing full regression.

The preview hang was a nested acquisition of the same non-reentrant encoding-queue lock. Removing the inner acquisition repairs that path. A bounded subprocess regression checks initial and cached snapshots without sensor images; the original native preview-edit test and full discovery now finish. No timeout, assertion or algorithm threshold was relaxed.
The earlier interrupted discovery and lock trace remain in [the first completion-attempt log](../results/issue51-completion-20261110-01/full-regression.log).

The remaining failures are:

| Test module                       | Failed test                                                                                  |
| --------------------------------- | -------------------------------------------------------------------------------------------- |
| `test_fusion_tracking`            | `test_kalman_update_matches_known_scalar_gain_and_joseph_covariance`                         |
| `test_godot_vegetation`           | `test_native_generator_and_worker_cache`                                                     |
| `test_godot_visualization_layers` | `test_fixed_geometry_styles_visibility_and_sensor_pixels`                                    |
| `test_godot_voxels`               | `test_solid_cells_use_raw_corners_score_alpha_and_clipped_edge_cells`                        |
| `test_package_runtime`            | `test_read_only_package_project_uses_a_separate_working_directory`                           |
| `test_package_runtime`            | `test_explicit_landscape_and_scenario_paths_survive_a_working_directory_change`              |
| `test_reference_fusion`           | `test_reference_plugin_cli_emits_clean_measurements_tracks_and_stage_batches`                |
| `test_visualization`              | `test_real_center_outputs_are_not_repeated_from_stage_batches`                               |
| `test_visualization_integration`  | `test_async_real_pixels_and_center_inputs_are_invariant_under_ui_replays_and_layers`         |
| `test_visualization_integration`  | `test_offset_stations_without_a_motion_prior_keep_the_alignment_failure`                     |
| `test_visualization_integration`  | `test_real_disconnect_after_located_output_keeps_evidence_and_stops_before_next_source_step` |
| `test_visualization_integration`  | `test_switching_from_image_results_to_empty_modes_and_preview_clears_every_cache`            |

The known Kalman discrepancy remains 3.480769230769231 versus expected 3.42. Its [previous clean-base reproduction](../results/occupancy-verification-20261109-01/baseline-kalman.log) is preserved; neither Kalman code nor its assertion changed. The other eleven failures are recorded without a clean-base attribution or repair. Benchmark completion does not imply these subsystems passed.

A2 injects executable-not-found and permission errors at `Popen`. The supervisor now retains startup-failure summaries even when no worker starts, with exception type, errno, command and null quality. A3 tests successful exit without a final summary and returned-phase duration exactly at and above its threshold. Existing forced-termination and overload evidence tests remain intact.

Global Ruff checks and Python formatting passed for 175 Python files. Mypy passed for 160 source/test files. The 48 focused native-interface, difficulty, workbench, device-scoring, pipeline and supervisor tests passed before full discovery. Documentation formatting also passed. These checks do not supersede the twelve full-regression failures above.

## Reproduction and evidence

Provision the optional library using [Development](development.md#optional-official-vibe-library), then run the bounded comparison described in [Algorithm evaluation](algorithm-evaluation.md#official-vibe-baseline). Use new output directories for every batch. The build manifest freezes source/compiler/library identity, and the trial manifest preserves each attempt, policy, completion state, resource peak and stop reason. Source hashes identify the uncommitted working tree used for the runs; citing this report does not imply a clean checkout.

The [trial manifest](../results/vibe-comparison-20261110-01/manifest.json) lists every attempt. The [completion summary](../results/issue51-completion-summary-20261110-01/summary.json) records per-seed quality, timing distributions, difficulty strata and regression failures. Its generator verifies that frozen source files remain unchanged and that stratified counts reproduce each original full-input summary.

```sh
nix develop --no-update-lock-file --command uv run --frozen python scripts/summarize_issue51_completion.py \
  results/hannover_vegetation_test_30s --trials results/vibe-comparison-20261110-01 \
  --regression-log results/issue51-completion-20261110-02/full-regression.log \
  --output results/issue51-completion-summary-NEW
```

Local recordings, external sources, binaries and generated output remain ignored and outside release archives. No held-out scene, real-camera, hardware or published-paper score comparison is claimed.
