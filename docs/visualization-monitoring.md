# Visualization performance monitoring

## Online monitoring

Godot's error-diagnostics page shows median, P95, maximum and sample count for up to 512 recent samples.

| Metric          | Scope                                                                                  |
| --------------- | -------------------------------------------------------------------------------------- |
| `update_ms`     | Accepted algorithm update: validation, evaluation, cache changes and draw-node rebuild |
| `evaluation_ms` | Matching and error statistics; included in `update_ms`                                 |
| `redraw_ms`     | Draw-node rebuild, including selection and station-origin changes                      |

Times use Godot's monotonic clock in milliseconds. Rejected/repeated updates and ordinary UI heartbeats do not add update/evaluation samples. No redraw sample is added without a drawing root. Windows roll independently; sample indices do not pair them. Cache counts also show rays, measurements, tracks and raw nonzero voxels.

The diagnostic cache is bounded: at most 600 rays, 200 measurements and 300 tracks. When an update would exceed a bound, the oldest cached entries are dropped first. Counts in the diagnostics page are cached counts, not total algorithm output. The bound is display-only; it does not change algorithm results or evidence.

These are CPU wall times, without waiting for GPU completion. They exclude Python encoding, socket transfer, image upload and window composition. Their inverse is not free-view FPS, and nested intervals must not be added.

## Existing recording evidence

No Godot, video decoding or new recording is needed:

```sh
nix develop --no-update-lock-file --command uv run --frozen python -m chongming.performance \
  results/hannover_vegetation_test_30s \
  --output results/hannover-performance.json
```

The output path must not exist; omit `--output` for stdout. Input requires `manifest.json`, `summary.json` and `timing.jsonl`. If present, `center-output.jsonl` contributes `center_ms` samples.

The report retains recording code, configuration, lock and platform provenance. Each phase has count, mean, median, nearest-rank P95 and maximum, including cold-start samples. It is not the recording summary's final 512-step rolling window. Empty samples are null; invalid or negative durations reject the report.

`total_ms` measures generation. `evidence_ms` measures subsequent evidence writing and encoder submission. Their sum is `step_wall_ms`; steps divided by its accumulated seconds estimates measured-step throughput. It is not whole-process runtime: startup, encoder shutdown and timing-row writes are outside this sum. `render_ms` already includes `gpu_ms`, which is Godot update, synchronization, drawing and readback wall time, not a hardware GPU timer.

`unavailable` lists metrics this evidence cannot establish. Without center output, center cost is not inferred from video. Quality scoring is separate: see [Algorithm evaluation](algorithm-evaluation.md) and the [current benchmark](benchmark-2026-11-02.md).

## Historical Hannover recording cost

Source: `results/hannover_vegetation_test_30s`, revision `a33ea76fff7bfdde3027a514fbf24257967a0c46`, macOS arm64. This historical recording is not current source performance. It completed 900 logical steps and 5400 frames without skipped exposures.

| Milliseconds per logical step   |    Mean |  Median |     P95 | Maximum |
| ------------------------------- | ------: | ------: | ------: | ------: |
| Generation `total_ms`           |  784.61 |  774.13 |  848.31 | 2201.46 |
| Imaging `render_ms`             |  628.13 |  619.67 |  674.99 | 2033.65 |
| Godot wall time `gpu_ms`        |  305.06 |  301.90 |  326.40 | 1574.94 |
| Geometry truth `frame_truth_ms` |  153.94 |  150.86 |  171.35 |  271.25 |
| Evidence `evidence_ms`          |  405.94 |  399.84 |  450.24 |  802.22 |
| Measured step `step_wall_ms`    | 1190.55 | 1173.98 | 1288.83 | 3003.68 |

Accumulated measured-step time is 1071.49 s, about 0.84 steps/s. This directory has no center output. It cannot validate earlier work-note estimates of center cost or attribute slow recording to association. Imaging, geometry truth and evidence writing all contribute.

Comparisons need the same scene digest, resolution, device policy, hardware, driver and sampling scope, with code and dependency versions recorded. No cross-hardware performance failure threshold is defined.
