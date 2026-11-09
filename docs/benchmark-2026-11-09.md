# Occupancy benchmark: erosion follow-up, 2026-11-09

A single 3-by-3 erosion allows MOG2 without occupancy to finish this recording within the declared budgets. It does not make the frame-difference configuration feasible: that arm still exceeds the 100-candidate limit. The completed MOG2 arm scores **71.92% recall and 86.05% precision**, with substantial misses on the smallest distant target.

This follows the [2026-11-08 evaluation](benchmark-2026-11-08.md). It reuses the same Hannover images and scoring gates. The benchmark did not render new sensor input or run a center localization comparison. Verification tests can render temporary fixtures separately.

## Fixed configuration

Both occupancy-disabled arms use one independent erosion with a 3-by-3 elliptical kernel after foreground extraction and before occupancy filtering and blob extraction. Opening and closing remain disabled. Blob dilation is 3. The area filter, downsampling, tracker, scoring and resource budgets remain unchanged. The kernel is applied at downsampling 2, in processed-image pixels.

The optional `detection.apply_erosion` policy field defaults to false. The native workbench and device adapter apply the same operation and preserve the field during export. Its implementation and evidence semantics are defined in [Algorithm evaluation](algorithm-evaluation.md#bounded-occupancy-benchmark).

## Feasibility and candidate counts

| Arm                                                         | Probe                        | Full-input outcome               |
| ----------------------------------------------------------- | ---------------------------- | -------------------------------- |
| Frame difference, occupancy off, erosion 3, blob dilation 3 | Candidate limit at 10.03 s   | Not attempted after failed probe |
| MOG2, occupancy off, erosion 3, blob dilation 3             | Complete, 360 frames/station | Complete, 900 frames/station     |

The failed frame-difference attempt stops at east station frame 301. It completed 904 station-frames before the failing frame; none reached the 15-second scoring boundary. Its full-recording quality is unavailable (`aggregate: null`). Its failure is a candidate-budget outcome, not an algorithm exception or zero precision.

The saved candidate records allow a same-frame comparison with the preceding no-erosion probe, without rerunning either method:

| Frame difference, east station frame 301              | No erosion | One erosion |
| ----------------------------------------------------- | ---------- | ----------- |
| Foreground pixels before morphology                   | 19650      | 19650       |
| Foreground pixels after morphology                    | 19650      | 2774        |
| Components after blob dilation, before area filtering | 46         | 432         |
| Accepted candidates                                   | 29         | 198         |

Both rows use occupancy off and blob dilation 3. Erosion removes foreground pixels but fragments this frame into more components. A lower foreground pixel count does not guarantee fewer accepted candidates. The no-erosion arm later fails on the west station at the same exposure group; its east-station record remains available for this comparison.

The MOG2 erosion arm has a full-run candidate peak of 35, with no warmup overload suppression. Across the 1347 scored station-frames, mean foreground pixels decrease from 548.16 before erosion to 140.04 after it. Mean accepted candidates are 5.94, P95 is 9, and maximum is 23. These counts do not distinguish true target pixels from background noise.

## Completed MOG2 quality

The full run has 1347 scored station-frames and 9134 responsible target-times: TP=6569, FN=2565, FP=1065. False positives comprise 968 false alarms and 97 duplicates. Recall is 71.92%, precision 86.05%, and F1 78.35%.

| Target                 | Responsible target-times | MOG2 occupancy on, no erosion, blob dilation 3 | MOG2 occupancy off, erosion 3, blob dilation 3 |
| ---------------------- | ------------------------ | ---------------------------------------------- | ---------------------------------------------- |
| crossing-fast-aircraft | 603                      | 25.54%                                         | 85.90%                                         |
| extreme-far-small      | 1347                     | 97.25%                                         | 40.24%                                         |
| far-fast-sphere        | 1347                     | 98.00%                                         | 96.66%                                         |
| medium-orbit-1         | 1347                     | 96.81%                                         | 74.39%                                         |
| medium-orbit-2         | 1347                     | 95.10%                                         | 65.55%                                         |
| near-fast-crossing     | 449                      | 0.00%                                          | 0.00%                                          |
| near-vertical-box      | 1347                     | 28.14%                                         | 100.00%                                        |
| ultra-far-aircraft     | 1347                     | 96.81%                                         | 72.38%                                         |

The columns report per-target recall for two practical configurations. Both occupancy and erosion differ; these deltas do not isolate a causal erosion effect. In particular, the no-occupancy, no-erosion control terminated before scoring, so it has no full-recording recall for a direct erosion ablation. The results show the tradeoff: some targets improve, while the smallest distant target and several other targets lose recall compared with tuned occupancy filtering. An analytical mask test independently verifies that the erosion removes a 2-by-2 signal along with isolated noise.

Supervised wall time is 19.27 seconds and worker peak RSS is 693.3 MiB. Mean scored detection-plus-tracking time is 2.362 ms per station-frame, P95 2.871 ms. Wall time includes input validation, decoding, scoring, checkpoint writes and process startup. This is one macOS arm64 run with Python 3.12.13, OpenCV 4.14.0 and 10 OpenCV threads. Earlier runs used an earlier diagnostic implementation; no controlled speedup, energy or target-hardware claim follows.

## Interpretation and research focus

MOG2 with suitable noise suppression remains a useful baseline. The occupancy approach's clearest scale-specific advantage here is retaining the distant small signal that the erosion configuration loses. For `extreme-far-small`, recall is 99.18% for selected frame difference with occupancy, 97.25% for tuned MOG2 with occupancy, and 40.24% for MOG2 with erosion and no occupancy. This supports preserving small signals through occupancy filtering instead of relying on erosion for noise suppression. It does not show a large frame-difference foreground-operator advantage over MOG2 when both use occupancy.

An advantage over every other target is not established. MOG2 with erosion scores 96.66% on `far-fast-sphere` and 100.00% on `near-vertical-box`, compared with 94.73% and 95.03% for selected frame difference with occupancy. Conversely, `near-fast-crossing` scores 0.00% with both tested MOG2 configurations and 76.39% with selected frame difference. Differences therefore also depend on motion and background behavior. The recording does not supply a held-out, predefined target-size stratification from which to claim a general advantage limited to distant small targets.

The maintainer's research focus is voxel voting for candidate selection and continuous geometric optimization for multistation localization. Device evidence and center-method attribution must stay separate. The historical center benchmark establishes the measured behavior of the existing pipeline on this recording; this follow-up does not measure the independent benefit of voxel voting or optimization. A bounded comparison with direct geometry on the same saved ray input remains the proposed P1 attribution experiment.

## Evidence and verification

The tables and conclusions in this report are versioned summaries suitable for citation. The linked recordings and result directories are local ignored evidence; they are not included in Git or release archives. Run manifests retain the source hashes from execution; citing the report's commit does not imply that the benchmark ran from a clean checkout of that commit.

The [new combined manifest](../results/occupancy-benchmark-20261109-01/manifest.json) indexes all 14 attempts and references the previous combined manifest without replacing it. Recording input hashes match across all five run directories. The current follow-up has separate [probe](../results/occupancy-probe-erode3-20261109-01/manifest.json) and [full-input](../results/occupancy-full-erode3-20261109-01/manifest.json) manifests. [Scored-frame diagnostics](../results/occupancy-benchmark-20261109-01/erosion-diagnostics.json) summarize the saved pixel, component, candidate, observation and pipeline records.

The source inventory identifies the default-disabled erosion policy, shared stage implementation and pre-morphology counters added for this follow-up. Earlier input, policies, logs, source hashes and measured results remain unchanged. ViBe remains a historical attempt, not reproduced here. P1 direct-geometry and regional analyses remain unimplemented.

Global Ruff checks, Python formatting, mypy for 151 source/test files and 74 related behavior tests passed. The full regression command used `unittest discover -s tests -v -f` and stopped at its first failure after 256 tests. The failure is `test_kalman_update_matches_known_scalar_gain_and_joseph_covariance`: the position is 3.480769230769231, while the assertion expects 3.42. The same failure was reproduced in a clean temporary export of branch base `72ca1efca295188b31e5077b9ac40d82704db0dd` using the same environment. No Kalman code or assertion was changed. Remaining discovery tests were not run, so full regression is not reported as passing.

The current [regression log](../results/occupancy-verification-20261109-01/full-regression.log) and [clean-base reproduction](../results/occupancy-verification-20261109-01/baseline-kalman.log) retain the failure evidence. The maintainer accepted this existing center-tracking discrepancy as a known limitation for the benchmark delivery. Its algorithm and test remain unchanged; any repair is separate work.
