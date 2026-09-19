"""Summarize a recorded mass-distribution benchmark and select a gated strategy."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from pyfog.distribution import (
    DistributionMeasurement,
    DistributionTopology,
    benchmark_summary,
    choose_distribution,
)


def load_document(path: Path) -> tuple[list[DistributionMeasurement], DistributionTopology]:
    try:
        value: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"No se pudo leer el benchmark: {error}") from None
    records: object
    if isinstance(value, list):
        records = value
        topology_value: object = {}
    elif isinstance(value, dict):
        records = value.get("measurements")
        topology_value = value.get("topology", {})
    else:
        records = None
        topology_value = {}
    if not isinstance(records, list):
        raise ValueError("El benchmark debe contener una lista measurements.")
    return (
        [DistributionMeasurement.model_validate(record) for record in records],
        DistributionTopology.model_validate(topology_value),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("benchmark", type=Path, help="JSON de mediciones reproducibles")
    args = parser.parse_args()
    try:
        measurements, topology = load_document(args.benchmark)
        report = {
            "summary": benchmark_summary(measurements),
            "decision": choose_distribution(measurements, topology).model_dump(mode="json"),
        }
    except ValueError as error:
        sys.stderr.write(f"benchmark_distribution: {error}\n")
        return 2
    sys.stdout.write(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
