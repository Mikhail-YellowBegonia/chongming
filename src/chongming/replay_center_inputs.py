"""Replay saved integrated inputs with an explicit independent-station threshold."""

from __future__ import annotations

import argparse
import json
import shutil
import time
from dataclasses import replace
from pathlib import Path

from chongming.contracts import development as d
from chongming.reference import create_postintersection_algorithm
from chongming.scenarios.run import _json
from chongming.scenarios.serialization import _decode


def replay(source: Path, output: Path, minimum_stations: int) -> None:
    if minimum_stations < 2:
        raise ValueError("minimum_stations must be at least two")
    registration = _decode(
        json.loads((source / "center-registration.json").read_text()), d.CenterRegistration
    )
    policy = registration.policy
    registration = replace(
        registration,
        policy=replace(
            policy,
            candidates=replace(
                policy.candidates,
                minimum_independent_stations=minimum_stations,
                minimum_candidate_score=float(minimum_stations),
            ),
            geometry=replace(policy.geometry, minimum_independent_stations=minimum_stations),
        ),
    )
    output.mkdir(parents=True, exist_ok=False)
    for name in ("scenario.json", "center-input.jsonl", "edge-rays.jsonl"):
        shutil.copyfile(source / name, output / name)
    (output / "center-registration.json").write_text(_json(registration) + "\n")
    algorithm = create_postintersection_algorithm()
    try:
        algorithm.start(registration)
        with (
            (source / "center-input.jsonl").open() as inputs,
            (output / "center-output.jsonl").open("x") as outputs,
            (output / "timings.jsonl").open("x") as timings,
        ):
            for count, line in enumerate(inputs):
                step = _decode(json.loads(line), d.CenterInputStep)
                began = time.perf_counter()
                result = algorithm.process(step)
                elapsed = (time.perf_counter() - began) * 1000
                assert algorithm.center is not None
                timings.write(
                    json.dumps(
                        {
                            "step_no": step.step_no,
                            "center_process_ms": elapsed,
                            "windows": algorithm.center.fusion.stage_timings,
                        }
                    )
                    + "\n"
                )
                outputs.write(_json(result) + "\n")
                if count % 100 == 0:
                    print(f"Processed {count} saved input steps", flush=True)
    finally:
        algorithm.close()
    (output / "replay-summary.json").write_text(
        json.dumps(
            {
                "source": str(source.resolve()),
                "minimum_stations": minimum_stations,
                "input": "identical saved device rays and calibration; no truth supplied to algorithm",
                "status": "complete",
            },
            indent=2,
        )
        + "\n"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--minimum-stations", type=int, required=True)
    args = parser.parse_args()
    replay(args.source, args.output, args.minimum_stations)


if __name__ == "__main__":
    main()
