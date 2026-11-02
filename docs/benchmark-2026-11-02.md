# Benchmark: 2026-11-02

The current integrated configuration scores **89.38% recall, 98.52% precision and 93.73% F1** on the Hannover vegetation synthetic recording, using known calibration and three-station FOV responsibility. This is the stage baseline for image detection through 3D localization. Center performance is recorded and accepted for this stage; no further optimization is planned now.

## Conditions and scope

- Data: `hannover_vegetation_test_30s`, three stations and 900 main frames per station. Existing recordings were reused. The scene includes vegetation and targets at different sizes, ranges and speeds.
- Device: two-frame difference with occupancy filtering, using the [selected policy](../scenarios/policies/hannover_vision_inc26_dilate15.json). Score Inc=26 means increment 2.6; Score Decay=1 means decay 0.1; occupancy dilation is 15. MOG2 remains a control group.
- Center: independent-station voxel voting, exhaustive supported combinations, continuous geometry and 3 m complete-link deduplication. Voxel votes, association groups and geometry support all require at least three independent stations, without two-station fallback.
- Uncertainty: known recorded calibration, explicitly assumed isotropic original-image pixel standard deviation 5 px, not an empirically calibrated covariance. Auxiliary camera calibration recognition is not evaluated.
- Warmup: exclude 15 s from each station's first main exposure midpoint, epoch 1000000 ns. Device training still consumes warmup frames; their observations do not reach the center. Scoring uses 449 synchronous windows and 449 frames per station.
- Environment recorded by the device reports: macOS 26.6 arm64, Python 3.12.13, OpenCV 4.14.0, OpenCV thread count 10, Nix develop and uv frozen. Hardware inspected while preparing the report: Apple M4, 10 cores and 24 GiB RAM. Timings are single runs, not controlled-load or repeated measurements.

The quality path covers image detection, observation-to-ray conversion, center admission, voxel candidates, intersection and deduplication. It scores localization measurements, not track identity or continuity. Shared-evidence tracking exclusions do not hide fusion outputs from scoring. Field network behavior, measured calibration errors, real camera domain differences and live scheduling are outside this test.

## Scoring

A sample is one target at one synchronous time, not whether a trajectory was ever detected. A target is responsible when its true center lies in at least three main-camera fields of view. Actual detection counts do not remove targets from the recall denominator. Occlusion is not an exemption in this experiment.

Let D be the physical model's body-space AABB diagonal. Error <=0.7D is a precise hit; 0.7D<error<3D is intermediate; error >=3D is completely off target. One-to-one maximum matching is applied per time. Extra outputs near the same target count as duplicates. Outputs near nonresponsible targets are excluded from precise scoring only within the precise gate; none were excluded in the final three-station result.

Recall is TP/(TP+FN); precision is TP/(TP+FP). Open-space detection has no directly enumerable true-negative set, so classification accuracy is not reported.

## Tests completed

| Stage                                                              |     Recall |  Precision | Interpretation                                                             |
| ------------------------------------------------------------------ | ---------: | ---------: | -------------------------------------------------------------------------- |
| Tuned frame-difference device                                      |     95.33% |     96.60% | Three-station aggregate; pixel gate max(10, 0.5 × projected AABB diagonal) |
| Tuned MOG2 device with occupancy                                   |     77.21% |     91.17% | Same recording and device scoring; not selected                            |
| Original center association, ideal input                           |     41.40% |       100% | Two-station responsibility; association search truncation caused misses    |
| Post-intersection deduplication, ideal input                       |       100% |       100% | 900 windows and 6137 two-station target-times, zero observation error      |
| Integrated, two-station intersection and responsibility            |     99.10% |     51.01% | Exposed cross-target ghost intersections; original result retained         |
| Integrated, two-station intersection, three-station responsibility |     99.09% |     50.88% | Rescored original outputs with the final responsibility denominator        |
| **Integrated, three-station intersection and responsibility**      | **89.38%** | **98.52%** | **Current stage conclusion**                                               |

Device scoring covers 1347 station-frames and 9134 target-times. Frame difference has TP=8707, FN=427, FP=306; tuned MOG2 has TP=7052, FN=2082, FP=683. Device and center use different responsibilities and gates; their recalls cannot be multiplied to derive integrated recall.

Godot inspection checked raw voxels, supporting rays and full intersection outputs. It identified association losses, multiple locations near aircraft wings and distant cross-target ghost locations. Scores 0 and 1 remain hidden; scores 2–15 use an opacity gradient of 6%–18%, clamped above 15. Native workbench scoring supports parameter tuning without HTML.

## Final integrated result

There are 2760 three-station responsible target-times: **TP=2467, FN=293, FP=37; F1=93.73%**.

| Case                         | Count | Operational primary attribution                              |
| ---------------------------- | ----: | ------------------------------------------------------------ |
| Miss                         |   167 | Device emitted fewer than three correct station observations |
| Miss                         |   126 | Center geometry rejection or estimation                      |
| Duplicate output             |    35 | Multiple locations survived center deduplication             |
| Cross-target output          |     1 | Center association ambiguity                                 |
| Same-target displaced output |     1 | Propagated device localization error                         |

Attribution uses offline labels, never supplied to the algorithms. It does not prove a single physical cause. The two nonduplicate outputs outside the precise gate are within 3D; completely off-target outputs are zero. This does not mean false positives have disappeared.

| Target                 | TP / responsible samples | Recall |
| ---------------------- | -----------------------: | -----: |
| crossing-fast-aircraft |                  21 / 66 | 31.82% |
| extreme-far-small      |                438 / 449 | 97.55% |
| far-fast-sphere        |                393 / 449 | 87.53% |
| medium-orbit-1         |                379 / 449 | 84.41% |
| medium-orbit-2         |                410 / 449 | 91.31% |
| near-vertical-box      |                380 / 449 | 84.63% |
| ultra-far-aircraft     |                446 / 449 | 99.33% |

`near-fast-crossing` has no three-station responsible samples, so no recall is assigned. The aggregate weights target-times; it must not hide the weak result for the fast crossing aircraft with few samples.

Two stations and two targets in or near one plane can produce geometrically valid cross-target intersections. The locations must also satisfy forward-depth, search and residual constraints. Three-station support suppresses this ambiguity but needs more correct device observations and reduces recall. It does not guarantee that all ghost intersections disappear.

## Center performance

| Measurement scope                                                            |      Mean |       P95 |
| ---------------------------------------------------------------------------- | --------: | --------: |
| Two-station integrated center call                                           | 158.83 ms | 253.27 ms |
| Two-station integrated geometry                                              | 114.96 ms | 203.05 ms |
| Three-station integrated center call                                         | 101.83 ms | 160.94 ms |
| Three-station integrated geometry                                            |  61.88 ms | 120.34 ms |
| Three device pipelines plus three-station center, sequential computation sum | 114.15 ms | 173.29 ms |

The last row sums separately measured device and center computation; it is not observed end-to-end latency. Mean center throughput is about 9.82 synchronous exposure groups/s, below 30 Hz. Geometry groups average 53.64/window versus 139.58 in the two-station experiment. Candidate generation still averages 35.79 ms. These single runs occurred at different times; do not claim a stable speedup ratio. Center inefficiency is accepted for this stage.

These computation measurements exclude video decoding, conversion, ray generation, serialization, disk, scoring, rendering and network.

## Device timing and embedded interpretation

Device timings are **per frame at one station**, not the sum of three stations. In the final integrated frame-difference experiment, the 1347 scored frames average **4.107 ms for detection plus tracking**, P95 **4.411 ms**. Including warmup, 2700 frames average 4.278 ms, P95 5.293 ms.

| Device stage, scored frames     |         Mean |          P95 |
| ------------------------------- | -----------: | -----------: |
| Preprocessing                   |     0.587 ms |     0.665 ms |
| Two-frame foreground extraction |     0.098 ms |     0.111 ms |
| Morphology                      |     0.001 ms |     0.002 ms |
| Occupancy filtering             |     2.223 ms |     2.430 ms |
| Components / blob extraction    |     0.767 ms |     0.802 ms |
| Detection total                 |     3.868 ms |     4.146 ms |
| Tracking                        |     0.239 ms |     0.297 ms |
| **Detection plus tracking**     | **4.107 ms** | **4.411 ms** |

Detection stages are included in the detection total and must not be added again. Percentiles cannot be added. Each station has 449 scored frames; mean detection plus tracking is 4.002 ms at center, 4.165 ms at east, and 4.154 ms at west. Occupancy filtering costs more than foreground differencing in this implementation, which helps identify embedded porting work without changing the algorithm now.

The separate practical device comparison, also excluding warmup, gives:

| Device configuration               | Detection + tracking mean / P95 | Foreground mean / P95 | Recall / precision |
| ---------------------------------- | ------------------------------: | --------------------: | -----------------: |
| Tuned frame difference + occupancy |                4.209 / 4.679 ms |      0.102 / 0.127 ms |    95.33% / 96.60% |
| Tuned MOG2 + occupancy             |                6.937 / 7.530 ms |      3.173 / 3.404 ms |    77.21% / 91.17% |

Both have 1347 scored frames and report 10 OpenCV threads. They are single runs at different times, with different blob dilation settings. This compares actual candidate configurations, not just one foreground operator. Including warmup, the corresponding full-pipeline means are 4.52 and 7.26 ms. The original description as single-threaded has been corrected to the actual report metadata. The data still supports keeping frame difference for this scene, with MOG2 as a control.

Python adds dispatch, object and stage-transfer overhead compared with an equivalent native implementation. Much pixel and matrix work already runs in OpenCV/NumPy native code, partly offsetting interpreter costs. The entire measured duration is not Python bytecode execution. No native port was benchmarked, so no fixed embedded speedup is promised. These are Apple M4 Python-plus-native-library measurements, not target embedded cycle, memory or energy budgets.

Offline video decoding averages 1.890 ms, P95 2.251 ms in final scored frames. Conversion/frame packaging averages 0.321 ms, P95 0.442 ms. Neither is included in detection plus tracking. The planned direct camera-to-hardware image-buffer path has no recording-file decoding step, so that cost is excluded from the embedded algorithm budget. ISP format adaptation, buffer transfer and any necessary preprocessing still need hardware measurement; no file decoding does not imply zero input cost. Algorithm preprocessing is already included in detection timing.

[Device timing details](../results/integrated-hannover-20261102/edge-timing-benchmark-20261102.json) aggregate saved per-frame records, including warmup, scored samples, station breakdowns and stage timings. No algorithm or recording was rerun to produce that summary.

## Evidence and reproduction

The [machine-readable benchmark](../results/integrated-hannover-20261102/benchmark-20261102.json) retains metrics, per-target results, timing, source report paths and SHA-256 digests, plus lock, device policy and center-registration digests. Detailed local evidence:

- [Three-station score](../results/integrated-hannover-20261102/score-three-stations/summary.json) and [per-window scores](../results/integrated-hannover-20261102/score-three-stations/frames.jsonl).
- [Attribution and performance](../results/integrated-hannover-20261102/attribution-three-stations/summary.json) and [individual cases](../results/integrated-hannover-20261102/attribution-three-stations/cases.jsonl).
- [Two-station comparison with the same responsibility](../results/integrated-hannover-20261102/score-two-vote-three-responsibility/summary.json) and [three-station configuration](../results/integrated-hannover-20261102/center-three-stations-final/center-registration.json).
- Commands and implementation details are in [Algorithm evaluation](algorithm-evaluation.md).

The evidence links target local ignored outputs; the reports and recordings are not shipped in Git or release archives. The benchmark was tuned and evaluated on one synthetic recording. No held-out generalization, real field performance, repeatability, or real-time claim follows. Reports retain source digests from the experimental dirty worktree; the experiment's HEAD alone is insufficient to reproduce it. Integration with later upstream typing and documentation changes does not constitute a fresh quality run. Verification outcomes for the delivered revision must be reported separately from these experiment results.
