"""Unit tests for bounded capacity report calculations."""

from scripts.crawler_capacity import (
    aggregate_reports,
    count_wait_snapshot,
    fairness,
    percentile,
    percentiles,
    sampling_budget,
)


def test_percentiles_empty_single_and_ordering():
    assert percentiles([]) == {"p50": None, "p95": None, "p99": None}
    assert percentiles([3]) == {"p50": 3, "p95": 3, "p99": 3}
    assert percentile([3, 1, 2], 0.5) == 2


def test_wait_snapshot_is_session_samples_not_distinct_events():
    rows = [
        ("Lock", "advisory", True, False),
        ("Lock", "transactionid", False, True),
        ("IO", "DataFileRead", False, False),
    ]
    first, peak = count_wait_snapshot(rows)
    second, _ = count_wait_snapshot(rows)
    first.update(second)
    assert peak == 2
    assert first["lock_wait_samples"] == 4
    assert first["advisory_lock_wait_samples"] == 2
    assert first["other_lock_wait_samples"] == 2
    assert first["wait_event_type:IO"] == 2


def test_telemetry_sample_budget_is_bounded():
    assert sampling_budget(30) == 300
    assert sampling_budget(600) == 6000
    assert sampling_budget(10000) == 6000


def test_worker_fairness_includes_idle_workers():
    assert fairness([5, 5])["jain_index"] == 1
    assert fairness([10, 0])["jain_index"] == 0.5
    assert fairness([0, 0])["jain_index"] is None


def report(workers=2, throughput=100, aborted=False, integrity=None):
    return {
        "workers": workers,
        "workload": 1000,
        "jobs_completed": 1000,
        "aborted": aborted,
        "integrity": integrity or {"all_jobs_completed": "PASS"},
        "throughput_jobs_s": throughput,
        "claim_latency_s": {"p95": 0.1, "p99": 0.2},
        "completion_latency_s": {"p95": 0.3, "p99": 0.4},
    }


def test_aggregate_preserves_failed_and_partial_runs():
    partial = report()
    partial["jobs_completed"] = 500
    failed = report(integrity={"all_jobs_completed": "FAIL"})
    group = aggregate_reports(
        [report(throughput=90), report(throughput=110), report(aborted=True), partial, failed]
    )["2"]
    assert group["runs"] == 5
    assert group["successful"] == 2
    assert group["failed_or_aborted"] == 3
    assert group["metrics"]["throughput_jobs_s"] == {
        "median": 100,
        "min": 90,
        "max": 110,
        "range": 20,
    }
    assert aggregate_reports([report(aborted=True)])["2"]["metrics"]["claim_p99_s"] is None


def test_legacy_report_without_integrity_is_readable():
    legacy = report()
    del legacy["integrity"]
    assert aggregate_reports([legacy])["2"]["successful"] == 1
