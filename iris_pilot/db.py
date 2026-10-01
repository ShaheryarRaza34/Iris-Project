"""Connection, run lock, and forward-only SQL migrations."""

from __future__ import annotations

import hashlib
from pathlib import Path

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from .runner import StageError

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

# Session-level advisory lock serialising pipeline runs against one database.
RUN_LOCK_KEY = 7_212_026


def connect(url: str) -> psycopg.Connection:
    # autocommit: each stage opens explicit transactions; the session-level
    # advisory lock then lives for the whole run.
    return psycopg.connect(url, autocommit=True, connect_timeout=5, application_name="iris-pilot")


def redact(url: str) -> str:
    try:
        params = conninfo_to_dict(url)
    except psycopg.ProgrammingError:
        return "<unparseable>"
    if "password" in params:
        params["password"] = "***"
    return make_conninfo(**params)


def one_line(exc: BaseException) -> str:
    """libpq errors span several lines; collapse them for manifests and consoles."""
    return " ".join(str(exc).split())


def try_run_lock(conn: psycopg.Connection) -> bool:
    return conn.execute("SELECT pg_try_advisory_lock(%s)", [RUN_LOCK_KEY]).fetchone()[0]


def run_lock_is_held(conn: psycopg.Connection) -> bool:
    row = conn.execute(
        "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND objid = %s AND granted",
        [RUN_LOCK_KEY],
    ).fetchone()
    return row[0] > 0


def _checksum(path: Path) -> str:
    # Normalise line endings so a CRLF checkout does not look like drift.
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def migrate(conn: psycopg.Connection) -> dict:
    conn.execute(
        """
        CREATE SCHEMA IF NOT EXISTS ops;
        CREATE TABLE IF NOT EXISTS ops.schema_migration (
            filename   text PRIMARY KEY,
            checksum   text NOT NULL,
            applied_at timestamptz NOT NULL DEFAULT now()
        );
        """
    )
    applied = dict(conn.execute("SELECT filename, checksum FROM ops.schema_migration").fetchall())
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))

    drifted = [f.name for f in files if f.name in applied and applied[f.name] != _checksum(f)]
    if drifted:
        raise StageError(f"applied migrations were edited after being applied: {drifted}")
    missing = sorted(set(applied) - {f.name for f in files})
    if missing:
        raise StageError(f"database has migrations not present in this checkout: {missing}")

    newly = []
    for f in files:
        if f.name in applied:
            continue
        with conn.transaction():
            conn.execute(f.read_text(encoding="utf-8"))
            conn.execute(
                "INSERT INTO ops.schema_migration (filename, checksum) VALUES (%s, %s)",
                [f.name, _checksum(f)],
            )
        newly.append(f.name)
    return {"applied_now": len(newly), "already_applied": len(applied), "files": newly}
