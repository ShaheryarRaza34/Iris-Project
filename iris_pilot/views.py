"""Materialized view refresh and artifact-state bookkeeping."""

from __future__ import annotations

import psycopg

from .runner import StageError
from .verticals import Vertical


def view_populated(conn: psycopg.Connection, view: str) -> bool | None:
    """True/False for populated/unpopulated, None if the view does not exist."""
    schema, name = view.split(".")
    row = conn.execute(
        "SELECT ispopulated FROM pg_matviews WHERE schemaname = %s AND matviewname = %s",
        [schema, name],
    ).fetchone()
    return None if row is None else row[0]


def dataset_version(conn: psycopg.Connection, vertical: str) -> int:
    return conn.execute(
        "SELECT version FROM ops.dataset_version WHERE vertical = %s", [vertical]
    ).fetchone()[0]


def record_built(conn: psycopg.Connection, artifact: str, *, version: int, run_id: str,
                 row_count: int, location: str | None = None) -> None:
    conn.execute(
        """UPDATE ops.artifact_state
           SET built_from_version = %s, built_at = now(), built_by_run = %s, row_count = %s,
               location = coalesce(%s, location),
               last_attempt_at = now(), last_attempt_run = %s,
               last_attempt_status = 'succeeded', last_error = NULL
           WHERE artifact = %s""",
        [version, run_id, row_count, location, run_id, artifact],
    )


def record_failed(conn: psycopg.Connection, artifact: str, *, run_id: str, error: str) -> None:
    # Only the attempt columns change: built_* still describes the last good build.
    conn.execute(
        """UPDATE ops.artifact_state
           SET last_attempt_at = now(), last_attempt_run = %s,
               last_attempt_status = 'failed', last_error = %s
           WHERE artifact = %s""",
        [run_id, error[:500], artifact],
    )


def check_view(conn: psycopg.Connection, v: Vertical) -> list[str]:
    if view_populated(conn, v.view) is None:
        return [f"materialized view {v.view} does not exist (migrations out of sync?)"]
    return []


def refresh(conn: psycopg.Connection, v: Vertical, run_id: str) -> dict:
    with conn.transaction():
        populated = view_populated(conn, v.view)
        if populated is None:
            raise StageError(f"materialized view {v.view} does not exist")
        version = dataset_version(conn, v.key)
        # CONCURRENTLY keeps the view readable during refresh, but needs an
        # already-populated view; the first build is a plain refresh.
        mode = "concurrent" if populated else "full"
        conn.execute(f"REFRESH MATERIALIZED VIEW {'CONCURRENTLY ' if populated else ''}{v.view}")

        rows = conn.execute(f"SELECT count(*) FROM {v.view}").fetchone()[0]
        expected = conn.execute(f"SELECT count(*) FROM {v.core_table}").fetchone()[0]
        if rows != expected:
            raise StageError(f"{v.view} has {rows} rows after refresh, expected {expected} from {v.core_table}")
        bands = dict(conn.execute(
            f"SELECT screening_band, count(*) FROM {v.view} GROUP BY 1 ORDER BY 1"
        ).fetchall())
        record_built(conn, v.view_artifact, version=version, run_id=run_id, row_count=rows)
    return {"row_count": rows, "built_from_version": version, "mode": mode, "by_band": bands}
