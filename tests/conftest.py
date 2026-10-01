"""Integration fixtures: each test gets its own throwaway database.

Point IRIS_TEST_ADMIN_URL at any PostgreSQL 16 + PostGIS 3.4 server where the
user may CREATE DATABASE (default: the docker compose service). DB tests are
skipped, loudly, if the server is unreachable.
"""

import os
import secrets
from pathlib import Path

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from iris_pilot.pipeline import Config

ADMIN_URL = os.environ.get("IRIS_TEST_ADMIN_URL", "postgresql://iris:iris@localhost:5433/postgres")
REPO = Path(__file__).resolve().parents[1]


def _with_db(url: str, dbname: str) -> str:
    return make_conninfo(**{**conninfo_to_dict(url), "dbname": dbname})


@pytest.fixture(scope="session")
def admin_url():
    try:
        psycopg.connect(ADMIN_URL, connect_timeout=3).close()
    except psycopg.OperationalError as exc:
        pytest.skip(f"PostgreSQL not reachable at IRIS_TEST_ADMIN_URL ({exc}); run `docker compose up -d --wait`")
    return ADMIN_URL


@pytest.fixture
def database_url(admin_url):
    name = f"iris_test_{secrets.token_hex(4)}"
    with psycopg.connect(admin_url, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    try:
        yield _with_db(admin_url, name)
    finally:
        with psycopg.connect(admin_url, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture
def config(database_url, tmp_path):
    return Config(
        database_url=database_url,
        export_dir=tmp_path / "exports",
        runs_dir=tmp_path / "runs",
        fixtures_dir=REPO / "fixtures",
    )


@pytest.fixture
def sql(database_url):
    """Run ad-hoc SQL against the test database (simulates operators / upstream)."""
    conn = psycopg.connect(database_url, autocommit=True)

    def run(query, params=None):
        cur = conn.execute(query, params)
        return cur.fetchall() if cur.description else None

    yield run
    conn.close()
