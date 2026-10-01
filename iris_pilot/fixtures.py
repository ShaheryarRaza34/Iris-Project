"""Loads the committed CSV fixtures into staging.

Stands in for the ingestion adapters that fill staging in production. The
upsert only touches rows whose content differs, so reloading is a no-op.
"""

from __future__ import annotations

import csv
from pathlib import Path

import psycopg

from .runner import StageError
from .verticals import Vertical

COMMON = (("country_code", "text"), ("source_id", "text"), ("name", "text"),
          ("source_date", "date"), ("review_status", "text"))


def load(conn: psycopg.Connection, v: Vertical, fixtures_dir: Path) -> dict:
    path = fixtures_dir / v.fixture_file
    cols = COMMON + v.attributes
    names = [c for c, _ in cols]
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        expected = set(names) | {"geom_ewkt"}
        if set(reader.fieldnames or ()) != expected:
            raise StageError(f"{path.name}: columns {reader.fieldnames} do not match contract {sorted(expected)}")
        # Empty CSV cells mean "absent in source" -> NULL, never a default.
        records = [[(row[c] or None) for c in names] + [row["geom_ewkt"] or None] for row in reader]

    placeholders = ", ".join(f"%s::{t}" for _, t in cols)
    sql = f"""
        INSERT INTO {v.staging_table} AS t ({", ".join(names)}, geom)
        VALUES ({placeholders}, ST_GeomFromEWKT(%s))
        ON CONFLICT (country_code, source_id) DO UPDATE
           SET {", ".join(f"{c} = EXCLUDED.{c}" for c in names[2:])}, geom = EXCLUDED.geom, loaded_at = now()
         WHERE ROW({", ".join(f"t.{c}" for c in names[2:])}, ST_AsEWKB(t.geom))::text
               IS DISTINCT FROM ROW({", ".join(f"EXCLUDED.{c}" for c in names[2:])}, ST_AsEWKB(EXCLUDED.geom))::text
        RETURNING (xmax = 0)
    """
    inserted = updated = 0
    with conn.transaction(), conn.cursor() as cur:
        for rec in records:
            cur.execute(sql, rec)
            res = cur.fetchone()
            if res is not None:
                inserted += res[0]
                updated += not res[0]
    return {"rows_in_file": len(records), "inserted": inserted, "updated": updated,
            "unchanged": len(records) - inserted - updated}
