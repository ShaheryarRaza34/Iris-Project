"""Controlled promotion: accepted, contract-valid staging rows -> core.

Idempotent by construction: rows are upserted on (country_code, source_id)
and only written when their content hash changed. The dataset version is
bumped only if something was inserted or updated, so a rerun on unchanged
input leaves every derived artifact fresh.
"""

from __future__ import annotations

import psycopg

from .verticals import CANONICAL_SRID, Vertical

# Order matters: the first failing rule is the reported reason.
COMMON_REJECTIONS: tuple[tuple[str, str], ...] = (
    ("not_accepted", "review_status <> 'accepted'"),
    ("missing_geometry", "geom IS NULL"),
    ("missing_crs", "ST_SRID(geom) = 0"),
    ("unsupported_geometry_type", "GeometryType(geom) NOT IN ('POLYGON', 'MULTIPOLYGON')"),
    ("invalid_geometry", "NOT ST_IsValid(geom)"),
    ("empty_geometry", "ST_IsEmpty(geom)"),
    ("missing_source_date", "source_date IS NULL"),
)


def _reason_case(v: Vertical) -> str:
    whens = "\n".join(
        f"            WHEN {pred} THEN '{reason}'" for reason, pred in COMMON_REJECTIONS + v.rejections
    )
    return f"CASE\n{whens}\n            ELSE NULL\n        END"


def promote(conn: psycopg.Connection, v: Vertical, run_id: str) -> dict:
    attrs = [c for c, _ in v.attributes]
    biz_cols = ["name", *attrs, "source_date"]
    insert_cols = ["country_code", "source_id", *biz_cols, "geom", "source_srid",
                   "content_hash", "promoted_at", "promoted_by_run"]
    update_cols = [c for c in insert_cols if c not in ("country_code", "source_id")]
    hash_expr = "md5(ROW(" + ", ".join(biz_cols) + ", ST_AsEWKB(geom))::text)"

    with conn.transaction():
        conn.execute(
            f"""
            CREATE TEMP TABLE _classified ON COMMIT DROP AS
            SELECT s.*, {_reason_case(v)} AS reject_reason
            FROM {v.staging_table} s
            """
        )
        by_reason = dict(conn.execute(
            "SELECT coalesce(reject_reason, 'eligible'), count(*) FROM _classified GROUP BY 1"
        ).fetchall())
        rejections = [
            {"country_code": r[0], "source_id": r[1], "reason": r[2]}
            for r in conn.execute(
                """SELECT country_code, source_id, reject_reason FROM _classified
                   WHERE reject_reason IS NOT NULL AND reject_reason <> 'not_accepted'
                   ORDER BY country_code, source_id"""
            ).fetchall()
        ]
        reprojected = conn.execute(
            "SELECT count(*) FROM _classified WHERE reject_reason IS NULL AND ST_SRID(geom) <> %s",
            [CANONICAL_SRID],
        ).fetchone()[0]

        written = conn.execute(
            f"""
            INSERT INTO {v.core_table} AS t ({", ".join(insert_cols)})
            SELECT country_code, source_id, {", ".join(biz_cols)},
                   ST_Multi(ST_Transform(geom, {CANONICAL_SRID})),
                   ST_SRID(geom),
                   {hash_expr},
                   now(),
                   %(run_id)s
            FROM _classified
            WHERE reject_reason IS NULL
            ON CONFLICT (country_code, source_id) DO UPDATE
               SET {", ".join(f"{c} = EXCLUDED.{c}" for c in update_cols)}
             WHERE t.content_hash IS DISTINCT FROM EXCLUDED.content_hash
            RETURNING (xmax = 0) AS inserted
            """,
            {"run_id": run_id},
        ).fetchall()
        inserted = sum(1 for (ins,) in written if ins)
        updated = len(written) - inserted

        if written:
            conn.execute(
                """UPDATE ops.dataset_version
                   SET version = version + 1, changed_at = now(), changed_by_run = %s
                   WHERE vertical = %s""",
                [run_id, v.key],
            )
        version = conn.execute(
            "SELECT version FROM ops.dataset_version WHERE vertical = %s", [v.key]
        ).fetchone()[0]
        core_rows = conn.execute(f"SELECT count(*) FROM {v.core_table}").fetchone()[0]

    eligible = by_reason.pop("eligible", 0)
    not_accepted = by_reason.pop("not_accepted", 0)
    return {
        "staged": eligible + not_accepted + sum(by_reason.values()),
        "eligible": eligible,
        "inserted": inserted,
        "updated": updated,
        "unchanged": eligible - inserted - updated,
        "not_accepted": not_accepted,
        "rejected": sum(by_reason.values()),
        "reprojected": reprojected,
        "core_rows": core_rows,
        "dataset_version": version,
        "rejected_by_reason": dict(sorted(by_reason.items())),
        "rejections": rejections,
    }
