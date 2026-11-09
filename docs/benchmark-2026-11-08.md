# Occupancy benchmark: 2026-11-08

The new bounded evaluation reproduces the historical frame-difference and tuned MOG2 device scores on the existing Hannover recording. Occupancy-disabled configurations exceed the agreed 100-candidate limit, including both supplemental blob-dilation-3 configurations. MOG2 without occupancy passes the 12-second probe but fails at 13.30 seconds in the full-input attempt. Probe completion does not establish full-run feasibility.

This is device detection/tracking evidence for issue #51. It does not rerun center localization or integrated scoring. Independent erosion is proposed but has not been implemented or measured in this report.

## Input and policy

- Recording: `results/hannover_vegetation_test_30s`; no new images were generated.
- Three main stations, 900 frames each. Successful full runs score 449 frames per station and 9134 responsible target-times in total.
- All images train the detector from the first frame. The first 15 seconds from each station's first main exposure are excluded from quality scoring. The recorded origin is 1000000 ns.
- Pixel gate: `max(10, 0.5 * projected target AABB diagonal)`. Responsibility remains target-center main-camera FOV membership, without an occlusion exemption.
- Opening and closing are disabled. Downsampling is 2; minimum component area is 10 processed pixels, without a maximum area. Occupancy-mask dilation is 15 when enabled. Blob dilation is separately recorded per arm.
- The four starting arms use blob dilation 5. Tuned MOG2 uses 3. The two supplemental occupancy-disabled arms change only blob dilation from 5 to 3. Method-specific blob dilation is permitted by the maintainer and is not required to remain equal across the comparison.

All arms use 10 OpenCV threads, a 4 GiB worker RSS budget and a 2-second detection/tracking phase budget. Probe wall time is limited to 300 seconds; full-input wall time to 1800 seconds. During the first 10 seconds, frames with more than 100 accepted candidates continue detector learning but skip tracking and produce no observations. At or after 10 seconds, the same condition stops the arm before tracking. A count of exactly 100 is allowed. These evaluation guards are separate from the live device's ray admission policy.

The environment reports macOS 26.6 arm64, Python 3.12.13 and OpenCV 4.14.0. Runs are sequential single measurements. The supervisor's RSS samples and worker high-water RSS are retained. These are not target-hardware memory, power or latency measurements.

## Completed full-input quality

| Method                      | Occupancy | Blob dilation | TP   | FN   | FP   | Recall | Precision |
| --------------------------- | --------- | ------------- | ---- | ---- | ---- | ------ | --------- |
| Frame difference            | on        | 5             | 8707 | 427  | 306  | 95.33% | 96.60%    |
| MOG2 starting configuration | on        | 5             | 7127 | 2007 | 8056 | 78.03% | 46.94%    |
| Tuned MOG2                  | on        | 3             | 7052 | 2082 | 683  | 77.21% | 91.17%    |

The first and third rows reproduce the historical quality counts. Their timing is newly measured and includes the current diagnostics. Matching quality counts do not establish repeatable timing or held-out performance. The MOG2 dilation change strongly affects this implementation's area-filtered candidates and precision; it should not be treated as an irrelevant setting.

| Completed arm                           | Total supervised wall time | Worker peak RSS | Mean detection + tracking per scored station-frame |
| --------------------------------------- | -------------------------- | --------------- | -------------------------------------------------- |
| Frame difference, occupancy, dilation 5 | 29.35 s                    | 581.3 MiB       | 4.559 ms                                           |
| MOG2, occupancy, dilation 5             | 33.95 s                    | 673.7 MiB       | 6.047 ms                                           |
| Tuned MOG2, occupancy, dilation 3       | 33.56 s                    | 674.4 MiB       | 5.594 ms                                           |

The means weight each station equally because all stations have 449 scored frames. Supervised wall time also includes input validation, decoding, scoring, evidence writes and worker startup. It is not equivalent to the algorithm pipeline time. No rendering is included.

## Resource-limit evidence

| Occupancy-disabled arm       | Attempt                           | Stop time from first exposure | Station | Frame index | Accepted candidates | Completed station-frames |
| ---------------------------- | --------------------------------- | ----------------------------- | ------- | ----------- | ------------------- | ------------------------ |
| Frame difference, dilation 5 | probe                             | 10.87 s                       | west    | 326         | 113                 | 980                      |
| MOG2, dilation 5             | probe                             | 10.03 s                       | east    | 301         | 163                 | 904                      |
| Frame difference, dilation 3 | supplemental probe                | 10.03 s                       | west    | 301         | 104                 | 905                      |
| MOG2, dilation 3             | full input after successful probe | 13.30 s                       | east    | 399         | 105                 | 1198                     |

Frame indices start at zero and belong to the failing station. Completed station-frames sum all three stations and exclude the failing frame; candidate evidence includes that frame. Every stop above is `candidate_limit`, not a timeout, out-of-memory event or algorithm exception. The guards stopped the arm before expensive tracking of the failing frame.

All incomplete arms have `aggregate: null`. They stopped before the 15-second scoring boundary and have no scored frames. Their quality is unavailable, not zero precision or zero recall. The two probe attempts with dilation 3 preserve their own failure/success records independently of the later full-input attempt. Earlier dilation-5 attempts were not overwritten.

The full-input successful runs suppressed 36 station-frames for frame difference, 209 for MOG2 dilation 5, and 65 for tuned MOG2 during the initial 10-second hold. Those frames still trained the detector. No scored frame was removed by this hold. These are tracking-overload suppressions, not measured counts from the integrated ray admission path.

## Evidence and remaining work

The tables and conclusions in this report are versioned summaries suitable for citation. The linked recordings and result directories are local ignored evidence; they are not included in Git or release archives. Run manifests retain the source hashes from execution; citing the report's commit does not imply that the benchmark ran from a clean checkout of that commit.

The local [combined manifest](../results/occupancy-benchmark-20261108-01/manifest.json) indexes all 11 attempted arms across the starting probes, supplemental probes and full-input runs. Each row retains its outcome, policy/source provenance through its run manifest, counters, resource peaks and the hash of its supervisor summary. The run directories are:

- [Starting probes](../results/occupancy-probe-20261108-01/manifest.json).
- [Dilation-3 probes](../results/occupancy-probe-dilate3-20261108-01/manifest.json).
- [Full-input attempts](../results/occupancy-full-20261108-01/manifest.json).

Each run has a frozen input/source hash inventory, exported policies, historical evidence index, worker logs, atomic progress checkpoints, candidate records and completed-frame evidence. The recording input inventories match across the three runs. Source hashes are retained per run; adding supplemental arm selection changed the benchmark orchestration between runs, without changing the detector, tracker, scorer or budget implementation.

The historical index includes an archived `mog2_only` report marked complete in `results/mog2-comparison-20261102-121913-300095`. Its original files and provenance remain unchanged. The maintainer's earlier reports of excessive unfiltered MOG2/ViBe candidates and erosion damage remain development history; these new bounded failures do not prove that every historical configuration failed. ViBe remains a historical attempt, not reproduced in this evaluation, because its original implementation has not been recovered.

One fixed erosion configuration may be evaluated next, with its kernel, iteration count and stage position declared before running. Candidate suppression must be considered together with losses on small true targets. No erosion result, integrated comparison, direct-geometry baseline, held-out scene or target-hardware claim is made here. Commands and evidence semantics are defined in [Algorithm evaluation](algorithm-evaluation.md#bounded-occupancy-benchmark).

Current implementation verification passed global Ruff checks, Python formatting, mypy for 151 source/test files and 71 related behavior tests. Full regression, including graphics and packaging, has not been run for this branch.
