"""Generic ordered stage runner.

Knows nothing about databases or verticals. It runs stages in declaration
order, skips stages whose dependencies did not succeed, records timings,
counts and failures, and derives one run status. The rule that matters most
lives in ``compute_run_status``: a run is only ``succeeded`` if every
critical stage succeeded.
"""

from __future__ import annotations

import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Callable


class StageStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


class RunStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    # Only non-critical stages failed; every critical output was produced.
    SUCCEEDED_WITH_WARNINGS = "succeeded_with_warnings"
    FAILED = "failed"


class StageError(Exception):
    """A stage failed for a known, explainable reason."""


class PrerequisiteError(StageError):
    """A stage's precondition was not met; the stage body was not executed."""


class InjectedFailure(StageError):
    """Deliberate failure requested via --fail-stage (drills and tests)."""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(ts: datetime | None) -> str | None:
    return ts.isoformat(timespec="milliseconds") if ts else None


@dataclass(frozen=True)
class Stage:
    name: str
    fn: Callable[[Any], dict[str, Any] | None]
    critical: bool = True
    requires: tuple[str, ...] = ()
    # Optional precondition check; returns a list of unmet prerequisites.
    check: Callable[[Any], list[str]] | None = None
    description: str = ""


@dataclass
class StageResult:
    name: str
    critical: bool
    requires: tuple[str, ...]
    status: StageStatus = StageStatus.PENDING
    started_at: datetime | None = None
    finished_at: datetime | None = None
    counts: dict[str, Any] = field(default_factory=dict)
    error: dict[str, Any] | None = None
    skipped_reason: str | None = None

    @property
    def duration_ms(self) -> int | None:
        if self.started_at and self.finished_at:
            return int((self.finished_at - self.started_at).total_seconds() * 1000)
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "critical": self.critical,
            "requires": list(self.requires),
            "status": str(self.status),
            "started_at": iso(self.started_at),
            "finished_at": iso(self.finished_at),
            "duration_ms": self.duration_ms,
            "counts": self.counts,
            "error": self.error,
            "skipped_reason": self.skipped_reason,
        }


def compute_run_status(results: list[StageResult]) -> RunStatus:
    """Derive the run status. Anything other than an explicit SUCCEEDED on a
    critical stage (failed, skipped, still pending/running) fails the run."""
    if any(r.critical and r.status != StageStatus.SUCCEEDED for r in results):
        return RunStatus.FAILED
    if any(r.status != StageStatus.SUCCEEDED for r in results):
        return RunStatus.SUCCEEDED_WITH_WARNINGS
    return RunStatus.SUCCEEDED


def _error_dict(exc: BaseException) -> dict[str, Any]:
    kind = (
        "prerequisite" if isinstance(exc, PrerequisiteError)
        else "injected" if isinstance(exc, InjectedFailure)
        else "stage_error" if isinstance(exc, StageError)
        else "unexpected"
    )
    tb = traceback.format_exception(type(exc), exc, exc.__traceback__)
    return {
        "kind": kind,
        "type": type(exc).__name__,
        "message": str(exc).strip(),
        # The tail is enough to locate the failure without bloating the manifest.
        "traceback_tail": "".join(tb[-3:]).strip(),
    }


class Runner:
    """Executes stages in order.

    ``on_update`` is called after every state change so the manifest on disk
    always reflects progress; a hard crash leaves a manifest stuck in
    ``running``, which ``status`` reports as an incomplete run.
    ``on_stage_failed`` lets the pipeline record failures against the
    artifacts a stage owns.
    """

    def __init__(
        self,
        stages: list[Stage],
        *,
        fail_fast: bool = False,
        inject_failures: frozenset[str] = frozenset(),
        on_update: Callable[[list[StageResult]], None] | None = None,
        on_stage_failed: Callable[[Stage, BaseException], None] | None = None,
    ) -> None:
        names = [s.name for s in stages]
        if len(set(names)) != len(names):
            raise ValueError(f"duplicate stage names: {names}")
        for i, s in enumerate(stages):
            unknown = [r for r in s.requires if r not in names[:i]]
            if unknown:
                raise ValueError(f"stage {s.name!r} requires {unknown}, which are not declared before it")
        bad = inject_failures - set(names)
        if bad:
            raise ValueError(f"--fail-stage names unknown stages: {sorted(bad)}")
        self.stages = stages
        self.fail_fast = fail_fast
        self.inject_failures = inject_failures
        self.on_update = on_update or (lambda _results: None)
        self.on_stage_failed = on_stage_failed
        self.results = [StageResult(s.name, s.critical, s.requires) for s in stages]

    def run(self, ctx: Any) -> RunStatus:
        by_name = {r.name: r for r in self.results}
        halted_by: str | None = None
        self.on_update(self.results)

        for stage, result in zip(self.stages, self.results):
            if halted_by:
                self._skip(result, f"run halted after critical stage {halted_by!r} failed (--fail-fast)")
                continue
            blocked = [r for r in stage.requires if by_name[r].status != StageStatus.SUCCEEDED]
            if blocked:
                self._skip(result, "upstream not successful: " + ", ".join(
                    f"{b} ({by_name[b].status})" for b in blocked))
                continue

            result.status = StageStatus.RUNNING
            result.started_at = utcnow()
            self.on_update(self.results)
            try:
                if stage.name in self.inject_failures:
                    raise InjectedFailure(f"failure injected into stage {stage.name!r}")
                if stage.check:
                    unmet = stage.check(ctx)
                    if unmet:
                        raise PrerequisiteError("unmet prerequisites: " + "; ".join(unmet))
                result.counts = stage.fn(ctx) or {}
                result.status = StageStatus.SUCCEEDED
            except Exception as exc:  # noqa: BLE001 - every failure must be recorded
                result.status = StageStatus.FAILED
                result.error = _error_dict(exc)
                if self.on_stage_failed:
                    try:
                        self.on_stage_failed(stage, exc)
                    except Exception as hook_exc:  # noqa: BLE001
                        result.error["failure_recording_error"] = repr(hook_exc)
                if stage.critical and self.fail_fast:
                    halted_by = stage.name
            finally:
                result.finished_at = utcnow()
                self.on_update(self.results)

        return compute_run_status(self.results)

    @staticmethod
    def _skip(result: StageResult, reason: str) -> None:
        result.status = StageStatus.SKIPPED
        result.skipped_reason = reason
