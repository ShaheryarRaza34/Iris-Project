"""Stage runner semantics, no database needed."""

import itertools

import pytest

from iris_pilot.runner import (PrerequisiteError, RunStatus, Runner, Stage, StageResult, StageStatus,
                               compute_run_status)


def ok(name, log):
    def fn(_ctx):
        log.append(name)
        return {"n": 1}
    return fn


def boom(name, log):
    def fn(_ctx):
        log.append(name)
        raise RuntimeError(f"{name} exploded")
    return fn


def by_name(runner):
    return {r.name: r for r in runner.results}


def test_stages_run_in_declared_order_and_record_counts():
    log = []
    runner = Runner([Stage("a", ok("a", log)), Stage("b", ok("b", log), requires=("a",)),
                     Stage("c", ok("c", log))])
    assert runner.run(None) == RunStatus.SUCCEEDED
    assert log == ["a", "b", "c"]
    r = by_name(runner)["b"]
    assert r.status == StageStatus.SUCCEEDED and r.counts == {"n": 1}
    assert r.started_at <= r.finished_at and r.duration_ms is not None


def test_failed_critical_stage_fails_run_and_skips_dependents():
    log = []
    runner = Runner([
        Stage("a", ok("a", log)),
        Stage("b", boom("b", log), requires=("a",)),
        Stage("c", ok("c", log), requires=("b",)),
        Stage("d", ok("d", log), requires=("c",)),
    ])
    assert runner.run(None) == RunStatus.FAILED
    res = by_name(runner)
    assert res["b"].status == StageStatus.FAILED
    assert res["b"].error["type"] == "RuntimeError" and "b exploded" in res["b"].error["message"]
    assert res["c"].status == StageStatus.SKIPPED and "b (failed)" in res["c"].skipped_reason
    assert res["d"].status == StageStatus.SKIPPED and "c (skipped)" in res["d"].skipped_reason
    assert log == ["a", "b"]


def test_independent_branch_continues_after_failure():
    log = []
    runner = Runner([
        Stage("root", ok("root", log)),
        Stage("left", boom("left", log), requires=("root",)),
        Stage("right", ok("right", log), requires=("root",)),
        Stage("left_out", ok("left_out", log), requires=("left",)),
        Stage("right_out", ok("right_out", log), requires=("right",)),
    ])
    assert runner.run(None) == RunStatus.FAILED
    assert log == ["root", "left", "right", "right_out"]
    assert by_name(runner)["right_out"].status == StageStatus.SUCCEEDED


def test_fail_fast_halts_everything_after_a_critical_failure():
    log = []
    runner = Runner([Stage("a", boom("a", log)), Stage("b", ok("b", log))], fail_fast=True)
    assert runner.run(None) == RunStatus.FAILED
    assert log == ["a"]
    assert "fail-fast" in by_name(runner)["b"].skipped_reason


def test_non_critical_failure_is_a_warning_not_plain_success():
    runner = Runner([Stage("a", ok("a", [])), Stage("tidy", boom("tidy", []), critical=False)])
    assert runner.run(None) == RunStatus.SUCCEEDED_WITH_WARNINGS


def test_prerequisite_failure_does_not_execute_stage_body():
    log = []
    runner = Runner([Stage("a", ok("a", log), check=lambda _ctx: ["view missing", "lock held"])])
    assert runner.run(None) == RunStatus.FAILED
    err = by_name(runner)["a"].error
    assert log == []
    assert err["kind"] == "prerequisite" and "view missing; lock held" in err["message"]


def test_prerequisite_error_raised_by_stage_is_classified():
    def fn(_ctx):
        raise PrerequisiteError("db down")
    runner = Runner([Stage("a", fn)])
    runner.run(None)
    assert by_name(runner)["a"].error["kind"] == "prerequisite"


def test_injected_failure_marks_stage_failed_without_running_it():
    log = []
    runner = Runner([Stage("a", ok("a", log))], inject_failures=frozenset({"a"}))
    assert runner.run(None) == RunStatus.FAILED
    assert log == [] and by_name(runner)["a"].error["kind"] == "injected"


def test_failure_hook_is_called_and_its_own_errors_are_recorded():
    seen = []

    def hook(stage, exc):
        seen.append((stage.name, type(exc).__name__))
        raise OSError("disk full")

    runner = Runner([Stage("a", boom("a", []))], on_stage_failed=hook)
    runner.run(None)
    assert seen == [("a", "RuntimeError")]
    assert "disk full" in by_name(runner)["a"].error["failure_recording_error"]


def test_on_update_sees_running_state_before_completion():
    snapshots = []
    runner = Runner([Stage("a", ok("a", []))],
                    on_update=lambda rs: snapshots.append([str(r.status) for r in rs]))
    runner.run(None)
    assert ["running"] in snapshots and snapshots[-1] == ["succeeded"]


@pytest.mark.parametrize("bad", [
    dict(stages=[Stage("a", ok("a", [])), Stage("a", ok("a", []))]),
    dict(stages=[Stage("a", ok("a", []), requires=("b",)), Stage("b", ok("b", []))]),
    dict(stages=[Stage("a", ok("a", []))], inject_failures=frozenset({"nope"})),
])
def test_invalid_stage_configuration_is_rejected(bad):
    with pytest.raises(ValueError):
        Runner(**bad)


def test_no_outcome_with_a_non_successful_critical_stage_is_reported_as_success():
    """Exhaustive over every status combination of three stages."""
    statuses = list(StageStatus)
    for combo in itertools.product(statuses, repeat=3):
        for crit in itertools.product([True, False], repeat=3):
            results = [StageResult(f"s{i}", c, (), status=s) for i, (s, c) in enumerate(zip(combo, crit))]
            status = compute_run_status(results)
            critical_bad = any(c and s != StageStatus.SUCCEEDED for s, c in zip(combo, crit))
            if critical_bad:
                assert status == RunStatus.FAILED, (combo, crit)
            if status == RunStatus.SUCCEEDED:
                assert all(s == StageStatus.SUCCEEDED for s in combo), (combo, crit)
