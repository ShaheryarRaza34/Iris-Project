"""The pilot pipeline: stage definitions, run context, and the run entrypoint.

Stage graph (each vertical is an independent branch after staging):

    preflight -> migrate -> load_staging_fixtures
        -> promote_bess    -> refresh_bess_view     -> export_bess_dossiers     -+
        -> promote_peatland -> refresh_peatland_view -> export_peatland_dossiers -+-> prune_old_exports
"""

from __future__ import annotations

import os
import secrets
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psycopg

from . import db, export, fixtures, freshness, promote, views
from .manifest import MANIFEST_SCHEMA_VERSION, write_json_atomic
from .runner import (PrerequisiteError, RunStatus, Runner, Stage, StageStatus,
                     compute_run_status, iso, utcnow)
from .verticals import VERTICALS, Vertical

DEFAULT_DATABASE_URL = "postgresql://iris:iris@localhost:5433/iris"
MIN_SERVER_VERSION = 160000
MIN_POSTGIS = (3, 4)


@dataclass(frozen=True)
class Config:
    database_url: str = DEFAULT_DATABASE_URL
    export_dir: Path = Path("exports")
    runs_dir: Path = Path("runs")
    fixtures_dir: Path | None = Path("fixtures")  # None: staging is filled externally
    sample_size: int = 3
    keep_exports: int = 3
    fail_stages: frozenset[str] = frozenset()
    fail_fast: bool = False


@dataclass
class Context:
    config: Config
    run_id: str
    conn: psycopg.Connection | None = None
    info: dict[str, Any] = field(default_factory=dict)

    @property
    def db(self) -> psycopg.Connection:
        if self.conn is None or self.conn.closed:
            raise PrerequisiteError("no database connection (preflight did not succeed)")
        return self.conn


def new_run_id() -> str:
    return utcnow().strftime("%Y%m%dT%H%M%S%fZ") + "-" + secrets.token_hex(2)


# --- stage bodies ----------------------------------------------------------

def stage_preflight(ctx: Context) -> dict:
    cfg = ctx.config
    try:
        ctx.conn = db.connect(cfg.database_url)
    except psycopg.OperationalError as exc:
        raise PrerequisiteError(f"database unreachable at {db.redact(cfg.database_url)}: "
                                f"{db.one_line(exc)}") from exc
    conn = ctx.conn
    problems: list[str] = []

    server = conn.info.server_version
    if server < MIN_SERVER_VERSION:
        problems.append(f"PostgreSQL {server} < 16 required")
    row = conn.execute(
        "SELECT default_version FROM pg_available_extensions WHERE name = 'postgis'"
    ).fetchone()
    postgis = row[0] if row else None
    if postgis is None:
        problems.append("PostGIS extension is not available on the server")
    elif tuple(int(p) for p in postgis.split(".")[:2]) < MIN_POSTGIS:
        problems.append(f"PostGIS {postgis} < 3.4 required")

    if not db.try_run_lock(conn):
        problems.append("another iris-pilot run holds the run lock on this database")

    if cfg.fixtures_dir is not None:
        for v in VERTICALS:
            if not (cfg.fixtures_dir / v.fixture_file).is_file():
                problems.append(f"fixture file missing: {cfg.fixtures_dir / v.fixture_file}")
    try:
        cfg.export_dir.mkdir(parents=True, exist_ok=True)
        probe = cfg.export_dir / f".write-probe-{ctx.run_id}"
        probe.write_text("ok")
        probe.unlink()
    except OSError as exc:
        problems.append(f"export dir not writable: {exc}")

    if problems:
        raise PrerequisiteError("; ".join(problems))
    return {"server_version": server, "postgis_version": postgis, "run_lock": "acquired"}


def stage_migrate(ctx: Context) -> dict:
    return db.migrate(ctx.db)


def stage_load_fixtures(ctx: Context) -> dict:
    return {v.key: fixtures.load(ctx.db, v, ctx.config.fixtures_dir) for v in VERTICALS}


def _promote(v: Vertical):
    return lambda ctx: promote.promote(ctx.db, v, ctx.run_id)


def _refresh(v: Vertical):
    return lambda ctx: views.refresh(ctx.db, v, ctx.run_id)


def _export(v: Vertical):
    return lambda ctx: export.export(ctx.db, v, ctx.run_id, ctx.config.export_dir, ctx.config.sample_size)


def stage_prune(ctx: Context) -> dict:
    return export.prune(ctx.config.export_dir, ctx.config.keep_exports, ctx.run_id)


# Which ops.artifact_state row a stage's failure is recorded against.
STAGE_ARTIFACTS: dict[str, str] = {
    **{f"refresh_{v.key}_view": v.view_artifact for v in VERTICALS},
    **{f"export_{v.key}_dossiers": v.export_artifact for v in VERTICALS},
}


def build_stages(cfg: Config) -> list[Stage]:
    stages = [
        Stage("preflight", stage_preflight,
              description="DB reachable, versions, run lock, fixtures, export dir"),
        Stage("migrate", stage_migrate, requires=("preflight",),
              description="apply pending SQL migrations, detect drift"),
    ]
    upstream = "migrate"
    if cfg.fixtures_dir is not None:
        stages.append(Stage("load_staging_fixtures", stage_load_fixtures, requires=("migrate",),
                            description="upsert committed fixtures into staging"))
        upstream = "load_staging_fixtures"
    for v in VERTICALS:
        stages.append(Stage(f"promote_{v.key}", _promote(v), requires=(upstream,),
                            description=f"promote accepted {v.title} rows to {v.core_table}"))
    for v in VERTICALS:
        stages.append(Stage(f"refresh_{v.key}_view", _refresh(v), requires=(f"promote_{v.key}",),
                            check=lambda ctx, v=v: views.check_view(ctx.db, v),
                            description=f"refresh {v.view}"))
    for v in VERTICALS:
        stages.append(Stage(f"export_{v.key}_dossiers", _export(v), requires=(f"refresh_{v.key}_view",),
                            check=lambda ctx, v=v: export.check_export(ctx.db, v),
                            description=f"export sample {v.title} dossiers"))
    stages.append(Stage("prune_old_exports", stage_prune, critical=False,
                        requires=tuple(f"export_{v.key}_dossiers" for v in VERTICALS),
                        description="remove old export runs and crashed temp dirs"))
    return stages


# --- run entrypoint --------------------------------------------------------

def _outputs(cfg: Config) -> dict[str, Any]:
    out = {}
    for v in VERTICALS:
        latest = cfg.export_dir / v.key / "LATEST.json"
        out[v.key] = str(latest) if latest.exists() else None
    return out


def run_pipeline(cfg: Config) -> tuple[RunStatus, Path, dict[str, Any]]:
    run_id = new_run_id()
    ctx = Context(config=cfg, run_id=run_id)
    manifest_path = cfg.runs_dir / f"{run_id}.json"
    manifest: dict[str, Any] = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "run_id": run_id,
        "status": str(RunStatus.RUNNING),
        "started_at": iso(utcnow()),
        "finished_at": None,
        "command": " ".join(sys.argv),
        "pid": os.getpid(),
        "config": {
            "database_url": db.redact(cfg.database_url),
            "export_dir": str(cfg.export_dir),
            "fixtures_dir": str(cfg.fixtures_dir) if cfg.fixtures_dir else None,
            "sample_size": cfg.sample_size,
            "fail_stages": sorted(cfg.fail_stages),
            "fail_fast": cfg.fail_fast,
        },
        "stages": [],
        "freshness": None,
        "outputs": {},
    }

    def on_update(results):
        manifest["stages"] = [r.to_dict() for r in results]
        write_json_atomic(manifest_path, manifest)

    def on_stage_failed(stage: Stage, exc: BaseException):
        artifact = STAGE_ARTIFACTS.get(stage.name)
        if artifact and ctx.conn is not None and not ctx.conn.closed:
            views.record_failed(ctx.conn, artifact, run_id=run_id, error=f"{type(exc).__name__}: {exc}")

    runner = Runner(build_stages(cfg), fail_fast=cfg.fail_fast, inject_failures=cfg.fail_stages,
                    on_update=on_update, on_stage_failed=on_stage_failed)
    try:
        runner.run(ctx)
    except BaseException as exc:  # KeyboardInterrupt, SystemExit: still leave a truthful manifest
        for r in runner.results:
            if r.status == StageStatus.RUNNING:
                r.status = StageStatus.FAILED
                r.finished_at = utcnow()
                r.error = {"kind": "aborted", "type": type(exc).__name__, "message": "run aborted",
                           "traceback_tail": None}
        manifest["aborted"] = repr(exc)
        raise
    finally:
        # Status is always re-derived from stage results: the single rule that
        # prevents a failed critical stage from being reported as success.
        status = compute_run_status(runner.results)
        manifest["stages"] = [r.to_dict() for r in runner.results]
        manifest["freshness"] = _safe_freshness(ctx)
        manifest["outputs"] = _outputs(cfg)
        manifest["status"] = str(status)
        manifest["finished_at"] = iso(utcnow())
        write_json_atomic(manifest_path, manifest)
        shutil.copyfile(manifest_path, cfg.runs_dir / "latest.json")
        if ctx.conn is not None and not ctx.conn.closed:
            ctx.conn.close()  # releases the session-level run lock
    return status, manifest_path, manifest


def _safe_freshness(ctx: Context) -> Any:
    try:
        conn = ctx.conn if ctx.conn is not None and not ctx.conn.closed else db.connect(ctx.config.database_url)
        try:
            return freshness.collect(conn)
        finally:
            if conn is not ctx.conn:
                conn.close()
    except Exception as exc:  # noqa: BLE001 - freshness must not mask the run result
        return {"error": f"{type(exc).__name__}: {db.one_line(exc)}"}
