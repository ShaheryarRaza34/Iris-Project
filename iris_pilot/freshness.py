"""Freshness of every derived artifact, compared with current promoted data."""

from __future__ import annotations

from typing import Any

import psycopg
from psycopg.rows import dict_row

from .export import verify_export
from .runner import iso
from .views import view_populated


def collect(conn: psycopg.Connection) -> list[dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cur:
        rows = cur.execute(
            """SELECT a.*, d.version AS data_version
               FROM ops.artifact_state a JOIN ops.dataset_version d USING (vertical)
               ORDER BY a.vertical, a.kind DESC"""
        ).fetchall()

    out = []
    for r in rows:
        built, data = r["built_from_version"], r["data_version"]
        if r["built_at"] is None:
            state, reason = "never_built", "never built"
        elif built < data:
            state, reason = "stale", f"built from {r['vertical']} v{built}, promoted data is at v{data}"
        else:
            state, reason = "fresh", f"built from v{built} by run {r['built_by_run']}"

        if state == "fresh" and r["kind"] == "materialized_view" and not view_populated(conn, r["artifact"]):
            state, reason = "stale", "view exists but is not populated"
        if state == "fresh" and r["kind"] == "export":
            problem = verify_export(r["location"])
            if problem:
                state, reason = "missing", problem

        out.append({
            "artifact": r["artifact"],
            "vertical": r["vertical"],
            "kind": r["kind"],
            "state": state,
            "reason": reason,
            "built_from_version": built,
            "data_version": data,
            "built_at": iso(r["built_at"]),
            "row_count": r["row_count"],
            "location": r["location"],
            "last_attempt_status": r["last_attempt_status"],
            "last_attempt_run": r["last_attempt_run"],
            "last_error": r["last_error"],
        })
    return out
