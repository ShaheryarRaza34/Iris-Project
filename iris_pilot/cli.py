"""iris-pilot command line.

    iris-pilot run      execute the full pipeline once (the one command)
    iris-pilot status   show artifact freshness and the most recent run
    iris-pilot stages   list stages in execution order

Exit codes for `run`: 0 succeeded, 1 failed, 3 succeeded with non-critical warnings.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import db, freshness
from .manifest import read_json, render_freshness, render_summary
from .pipeline import DEFAULT_DATABASE_URL, Config, build_stages, run_pipeline
from .runner import RunStatus

EXIT_CODES = {RunStatus.SUCCEEDED: 0, RunStatus.FAILED: 1, RunStatus.SUCCEEDED_WITH_WARNINGS: 3}


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="iris-pilot", description="Project IRIS one-command pilot orchestration")
    p.add_argument("--database-url", default=os.environ.get("IRIS_DATABASE_URL", DEFAULT_DATABASE_URL),
                   help="PostgreSQL URL (env IRIS_DATABASE_URL)")
    p.add_argument("--runs-dir", type=Path, default=Path("runs"), help="where run manifests are written")
    sub = p.add_subparsers(dest="command", required=True)

    r = sub.add_parser("run", help="run the full pipeline")
    r.add_argument("--export-dir", type=Path, default=Path("exports"))
    r.add_argument("--fixtures-dir", type=Path, default=Path("fixtures"))
    r.add_argument("--no-fixtures", action="store_true",
                   help="skip fixture loading; staging is filled by an external ingestion step")
    r.add_argument("--sample-size", type=int, default=3, help="dossiers exported per vertical")
    r.add_argument("--keep-exports", type=int, default=3, help="export runs retained per vertical")
    r.add_argument("--fail-fast", action="store_true",
                   help="skip all remaining stages after a critical failure (default: independent branches continue)")
    r.add_argument("--fail-stage", action="append", default=[], metavar="STAGE",
                   help="inject a failure into STAGE (failure drills); repeatable")
    r.add_argument("--json", action="store_true", help="print the manifest as JSON instead of the summary")

    sub.add_parser("status", help="show artifact freshness and the most recent run")
    sub.add_parser("stages", help="list stages in execution order")
    return p


def cmd_run(args) -> int:
    if args.sample_size < 1 or args.keep_exports < 1:
        print("--sample-size and --keep-exports must be >= 1", file=sys.stderr)
        return 2
    cfg = Config(
        database_url=args.database_url,
        export_dir=args.export_dir,
        runs_dir=args.runs_dir,
        fixtures_dir=None if args.no_fixtures else args.fixtures_dir,
        sample_size=args.sample_size,
        keep_exports=args.keep_exports,
        fail_stages=frozenset(args.fail_stage),
        fail_fast=args.fail_fast,
    )
    try:
        status, path, manifest = run_pipeline(cfg)
    except ValueError as exc:  # invalid stage configuration, e.g. unknown --fail-stage
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(manifest, indent=2, default=str) if args.json else render_summary(manifest))
    print(f"manifest: {path}")
    return EXIT_CODES[status]


def _latest_run(runs_dir: Path) -> dict | None:
    candidates = sorted(p for p in runs_dir.glob("*.json") if p.name != "latest.json") if runs_dir.exists() else []
    return read_json(candidates[-1]) if candidates else None


def cmd_status(args) -> int:
    latest = _latest_run(args.runs_dir)
    healthy = True
    try:
        conn = db.connect(args.database_url)
    except Exception as exc:  # noqa: BLE001
        print(f"database unreachable: {db.one_line(exc)}")
        return 1
    with conn:
        if latest is None:
            print("no runs recorded yet")
            healthy = False
        else:
            if latest["status"] == "running":
                if db.run_lock_is_held(conn):
                    print(f"run {latest['run_id']} is IN PROGRESS")
                else:
                    print(f"run {latest['run_id']} is INCOMPLETE: manifest says running but no process holds "
                          "the run lock (crashed or killed)")
                    latest["status"] = "incomplete"
            print(render_summary({**latest, "freshness": None}))
            healthy = latest["status"] == "succeeded"
        try:
            rows = freshness.collect(conn)
        except Exception as exc:  # noqa: BLE001
            print(f"artifacts: unavailable ({type(exc).__name__}: {exc})")
            return 1
    print("artifacts (live):")
    print("\n".join(render_freshness(rows)))
    healthy = healthy and all(r["state"] == "fresh" for r in rows)
    return 0 if healthy else 1


def cmd_stages(_args) -> int:
    for i, s in enumerate(build_stages(Config()), 1):
        crit = "critical" if s.critical else "non-critical"
        req = ", ".join(s.requires) or "-"
        print(f"{i:>2}. {s.name:<26} {crit:<13} requires: {req:<45} {s.description}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    return {"run": cmd_run, "status": cmd_status, "stages": cmd_stages}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
