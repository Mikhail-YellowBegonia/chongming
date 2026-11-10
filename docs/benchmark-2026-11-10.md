# Issue #51: center attribution and device comparisons, 2026-11-10

On 30 fixed saved-image-ray windows, voxel candidate filtering preserves the direct baseline's quality while reducing tested support combinations from 8820 to 1599. Mean kernel time across three runs is 103.90 ms/window versus 424.30 ms/window: an observed ratio of **4.08**, restricted to this fixture and implementation. Adding continuous refinement to the same voxel evidence raises recall from **5.41% to 87.57%** under the declared quality gates.

The new device arm, MOG2 with occupancy and erosion, completes but scores only **66.99% recall and 86.95% precision**. Erosion is not a beneficial addition to tuned occupancy-enabled MOG2 on this recording.

## Center scores: B1/B2/B3

The fixture selects every fifteenth actual scored exposure from the existing three-station integrated recording: 30 windows from 449, with 185 responsible target-times. Selection uses the exposure schedule, including empty windows, rather than predictions, truth labels or sealed-window midpoints. This is a deterministic subset of the tuned Hannover recording, not held-out validation or a full-recording score.

| Route                                            | TP  | FP  | FN  | Recall | Precision | F1     | Tested support groups |
| ------------------------------------------------ | --- | --- | --- | ------ | --------- | ------ | --------------------- |
| Voxel candidates + continuous refinement         | 162 | 0   | 23  | 87.57% | 100.00%   | 93.37% | 1599                  |
| Direct combinations + same continuous refinement | 162 | 0   | 23  | 87.57% | 100.00%   | 93.37% | 8820                  |
| Same voxel evidence, coarse representative only  | 10  | 0   | 175 | 5.41%  | 100.00%   | 10.26% | 1599                  |

All three repetitions produce identical quality counts. Direct geometry has 164 accepted support groups before deduplication; the voxel route retains **164/164**, or 100%. Both retain 162 final measurements after deduplication. Voxel screening removes 81.87% of the tested combinations without losing an accepted direct-geometry support group on this fixture.

The inputs are the already-admitted ordinary image rays and registered configuration from `results/integrated-hannover-20261102/center-three-stations-final`. Truth enters only the offline scorer. Every group requires three independent stations, one ray per station; no two-ray fallback is used. Both refined arms use the existing continuous solver, calibration weighting, uncertainty, angle/condition/residual rules and common final search-volume/forward-depth checks, followed by existing 3 m complete-link deduplication. No track prediction is supplied because the selected exposures are synchronous. These timings exclude device processing, admission, tracking, serialization and scoring.

The direct baseline is an explicit offline implementation: one full-volume candidate preserves all admitted station alternatives, which the existing exhaustive grouping and geometry code consume. The production `DirectGeometryCandidateModule` stub is unchanged. This is a bounded unpruned baseline, not a claim about every optimized triangulation implementation.

The coarse baseline uses the current candidate-bound midpoint on the recorded 10 m grid. Repeated support evidence chooses the lexicographically first supporting candidate ID. This representative need not be a single cell center. Its residual is tested at that point with the same 1 m threshold, plus angle, conditioning, volume and forward-depth rules; it has no iterative fit, fitted covariance or solver-convergence test. Of 1599 groups, 1589 are rejected, compared with 1435 for voxel plus refinement. It is an ablation of this pipeline's representative, not a universal voxel-only method.

| Route                 | Mean kernel ms/window across runs | SD of run means | Precise matched errors: N / mean / P95 |
| --------------------- | --------------------------------- | --------------- | -------------------------------------- |
| Voxel + refinement    | 103.90                            | 2.29            | 162 / 1.409 m / 2.829 m                |
| Direct + refinement   | 424.30                            | 5.76            | 162 / 1.409 m / 2.829 m                |
| Coarse representative | 46.44                             | 0.88            | 10 / 0.877 m / 2.772 m                 |

The coarse error statistic has a much smaller conditional denominator. Its lower matched-only mean does not establish better localization: it misses 175 of 185 responsible samples. Precise matching uses 0.7 times the model AABB diagonal; broad diagnostics use three diagonals. No infinite-precision or general real-time claim follows.

## Device scores: C1

Every completed device run processes 900 frames per station, including 451 adaptive warmup frames, and scores 449 per station. The denominator is 9134 responsible target-times over 1347 scored station-frames. Gates remain `max(10 px, 0.5 * projected target diagonal)`. Opening/closing are off; MOG2 blob dilation is 3, occupancy-mask dilation is 15 when enabled, and erosion is one 3-by-3 elliptical operation when enabled.

| Method                                     | Occupancy | Erosion | TP          | FP          | FN          | Recall      | Precision   | F1          |
| ------------------------------------------ | --------- | ------- | ----------- | ----------- | ----------- | ----------- | ----------- | ----------- |
| Selected frame difference, blob dilation 5 | on        | off     | 8707        | 306         | 427         | 95.33%      | 96.60%      | 95.96%      |
| Tuned MOG2                                 | on        | off     | 7052        | 683         | 2082        | 77.21%      | 91.17%      | 83.61%      |
| MOG2                                       | off       | on      | 6569        | 1065        | 2565        | 71.92%      | 86.05%      | 78.35%      |
| MOG2: new C1 arm                           | on        | on      | 6119        | 918         | 3015        | 66.99%      | 86.95%      | 75.68%      |
| MOG2, archived matched control             | off       | off     | unavailable | unavailable | unavailable | unavailable | unavailable | unavailable |

The last arm previously exceeded 100 candidates at 13.30 seconds, before quality scoring. Its outcome is reused from the preserved dilation-3 full-input attempt; it is not assigned zero precision. The other four configurations were each rerun three times under the current implementation, with identical quality counts in every repeat.

At fixed occupancy on, adding erosion loses 933 true positives and lowers both recall and precision. At fixed erosion on, adding occupancy lowers recall by 4.93 percentage points and raises precision by 0.91 points. The data does not support a universal benefit from either operation. These fixed-factor comparisons replace the earlier two-factor contrast for these particular questions.

## Fixed-region and motion scores: C2

One reference occupancy mask per station is learned from the selected frame-difference policy and frozen at frame 450, the last excluded warmup exposure. The same fixed region is used for every method and every scored frame. Region areas are 8.17% of the center image, 12.23% east and 14.23% west. This is a reference-policy-conditioned region, not independently labeled vegetation truth. Membership uses the recorded target center in processed-image coordinates. Full-frame one-to-one matching occurs before stratification.

| Method                       | Inside reference region: hits / 221 | Recall | Outside: hits / 8913 | Recall |
| ---------------------------- | ----------------------------------- | ------ | -------------------- | ------ |
| Frame difference + occupancy | 177                                 | 80.09% | 8530                 | 95.70% |
| MOG2 + occupancy, no erosion | 152                                 | 68.78% | 6900                 | 77.42% |
| MOG2 + erosion, no occupancy | 106                                 | 47.96% | 6463                 | 72.51% |
| MOG2 + occupancy + erosion   | 100                                 | 45.25% | 6019                 | 67.53% |

Motion labels use recorded truth projections only during offline analysis. Slow motion is declared as apparent displacement over 30 frames divided by elapsed time at most 5 original-image px/s, with contiguous FOV history. There are **zero slow scored samples** under this definition, so slow-motion quality is unavailable.

A return-to-region sample requires a previous center within 10 px at least two seconds earlier, with an intervening excursion beyond 20 px from the current center, using contiguous visible history. There are 36 such responsible target-times: frame difference plus occupancy hits 36/36 (100%); MOG2 plus occupancy hits 33/36 (91.67%); the two erosion configurations each hit 22/36 (61.11%). The remaining 9098 samples have no qualifying return. These are correlated trajectory samples from one recording, not 36 independent targets or broad repeated-motion validation. No labels were constructed from predictions.

## Repeated timing and load: C3

Each configuration is run three times, sequentially, with rotated method order. All arms within a comparison use the same implementation and inputs. OpenCV threads are 10; `OPENBLAS_NUM_THREADS=1` and `VECLIB_MAXIMUM_THREADS=1` are set for the batch. Device pipeline timing includes detection and actual tracking, excluding decode, scoring and evidence writes. Foreground-stage timing is nested within detection and must not be added to the pipeline total.

| Device configuration         | Mean pipeline ms/frame | SD of run means | Mean foreground-stage ms | Mean accepted candidates / scored frame |
| ---------------------------- | ---------------------- | --------------- | ------------------------ | --------------------------------------- |
| Frame difference + occupancy | 3.797                  | 0.116           | 0.116                    | 6.88                                    |
| MOG2 + occupancy             | 4.724                  | 0.076           | 1.287                    | 6.19                                    |
| MOG2 + erosion               | 2.592                  | 0.144           | 1.286                    | 5.94                                    |
| MOG2 + occupancy + erosion   | 4.599                  | 0.228           | 1.256                    | 5.49                                    |

These are practical-policy comparisons; frame difference and MOG2 use different blob dilation. Frame difference is faster than the occupancy-enabled MOG2 configurations here, while eroded MOG2 without occupancy is faster but has lower quality. CPU timings do not measure power.

The center comparison measures actual per-station ray counts, distinct nonempty visited voxels including uncertainty expansion, support alternatives, tested groups and solver calls. It does not infer load from target count. Each refined group causes one solver invocation; coarse representatives cause none. The machine-readable summary retains ray/voxel ranges, phase times, candidate-limit reasons and peak RSS. No final arm exceeded its budget. All measurements use macOS 26.6 arm64, Python 3.12.13 and OpenCV 4.14.0. The observed center ratio is not a fixed 3.5–7x model prediction or a target-hardware result.

## Evidence, scope and verification

The [combined summary](../results/issue51-followup-20261110-01/summary.json) references nine final center runs and twelve full device runs, including source, fixture/policy hashes, quality counts, phase timings, memory and common-region/motion scores. The [center fixture manifest](../results/center-attribution-20261110-02/fixture/manifest.json) fixes the ordinary-ray input and window selection. The [device manifest](../results/device-extensions-20261110-01/manifest.json), reference masks and motion-label definitions fix its comparison populations.

An initial nine-run center fixture used a sealed-window midpoint to classify an empty boundary window and could include a warmup exposure. It was invalidated, preserved in `results/center-attribution-20261110-01`, and excluded from all final numbers. The combined summary marks those attempts `invalid_fixture` with no citable quality score. The corrected fixture uses the same actual-exposure rule as the normal scorer; a focused test covers this boundary. There are 21 valid completed comparisons and nine invalidated initial attempts in this delivery.

Production detection, candidate search, geometry, tracking and admission implementations were not repaired or optimized. Only offline comparison, stratification and reporting tools were added. A tasks and existing Kalman regression work remain deferred by maintainer instruction. The direct route remains an offline baseline; it does not complete the production direct-candidate module.

The subsequent [ViBe and physical-difficulty report](benchmark-vibe-2026-11-10.md) records the later A1/A2/A3 work, authorized preview-lock repair and new baseline. The verification statements here describe the earlier attribution batch.

Global Ruff checks and Python formatting passed; mypy passed for 154 source/test files. All 34 related tests passed, including six new analytical tests covering direct three-station support, coarse-point residual behavior, the new C1 policy, common-region scoring, motion labels and actual-exposure selection for empty windows. These tests are separate from measured quality and do not replace full regression. No held-out scene, real-camera, network, hardware or learned-baseline validation is claimed.

Report tables are versioned summaries suitable for citation. Linked recordings, masks and detailed outputs remain local ignored evidence and are not included in Git or release archives. Reproduction commands are:

```sh
OPENBLAS_NUM_THREADS=1 VECLIB_MAXIMUM_THREADS=1 nix develop --no-update-lock-file --command \
  uv run --frozen python -m chongming.benchmark_center_attribution \
  --source results/integrated-hannover-20261102/center-three-stations-final \
  --output results/center-attribution-NEW --repeats 3
OPENBLAS_NUM_THREADS=1 VECLIB_MAXIMUM_THREADS=1 nix develop --no-update-lock-file --command \
  uv run --frozen python -m chongming.benchmark_device_extensions \
  results/hannover_vegetation_test_30s --output results/device-extensions-NEW --repeats 3
```
