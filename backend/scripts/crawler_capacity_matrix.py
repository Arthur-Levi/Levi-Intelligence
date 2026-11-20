"""Aggregate individually bounded crawler capacity artifacts without hiding failed runs."""

import argparse
import json
from pathlib import Path

from scripts.crawler_capacity import aggregate_reports


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("reports", nargs="+", type=Path)
    parser.add_argument("--output", type=Path, default=Path("capacity-matrix.json"))
    args = parser.parse_args()
    reports = [json.loads(path.read_text()) for path in args.reports]
    if (
        len(reports) != 12
        or sorted(r["workers"] for r in reports) != [1] * 3 + [2] * 3 + [4] * 3 + [8] * 3
    ):
        parser.error("exactly three reports for each worker count 1, 2, 4, 8 are required")
    if any(r.get("workload") != 1000 for r in reports):
        parser.error("all reports must use 1000 jobs")
    if len({r.get("commit") for r in reports}) != 1:
        parser.error("all reports must use the same commit")
    result = {
        "commit": reports[0]["commit"],
        "runs": reports,
        "summary": aggregate_reports(reports),
    }
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result["summary"], indent=2))


if __name__ == "__main__":
    main()
