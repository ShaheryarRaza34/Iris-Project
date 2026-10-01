"""End-to-end pipeline tests against a real PostGIS database."""

import dataclasses
import json
import shutil

import psycopg
import pytest

from iris_pilot import cli, db, export
from iris_pilot.manifest import read_json
from iris_pilot.pipeline import run_pipeline
from iris_pilot.runner import RunStatus

pytestmark = pytest.mark.db


def stage(manifest, name):
    return next(s for s in manifest["stages"] if s["name"] == name)


def artifacts(manifest):
    return {a["artifact"]: a for a in manifest["freshness"]}


def dossier_hashes(cfg, vertical):
    latest = read_json(cfg.export_dir / vertical / "LATEST.json")
    index = read_json(cfg.export_dir / vertical / latest["dir"] / "index.json")
    return {d["file"]: d["sha256"] for d in index["dossiers"]}


# --- acceptance 1: one command produces both vertical outputs ---------------

def test_one_command_produces_both_vertical_outputs(config):
    status, path, m = run_pipeline(config)

    assert status == RunStatus.SUCCEEDED
    assert read_json(path)["status"] == "succeeded"
    assert read_json(config.runs_dir / "latest.json")["run_id"] == m["run_id"]
    assert all(s["status"] == "succeeded" for s in m["stages"])
    assert all(s["started_at"] and s["finished_at"] for s in m["stages"])

    assert dossier_hashes(config, "bess").keys() == {"DE_BESS-001.md", "DE_BESS-008.md", "PL_BESS-001.md"}
    assert dossier_hashes(config, "peatland").keys() == {"DE_PEAT-001.md", "DE_PEAT-002.md", "DE_PEAT-007.md"}
    assert {a["state"] for a in m["freshness"]} == {"fresh"}

    bess = stage(m, "promote_bess")["counts"]
    assert (bess["staged"], bess["inserted"], bess["not_accepted"], bess["rejected"]) == (10, 5, 2, 3)
    assert bess["rejected_by_reason"] == {"invalid_geometry": 1, "missing_crs": 1, "missing_geometry": 1}
    assert bess["reprojected"] == 1
    peat = stage(m, "promote_peatland")["counts"]
    assert peat["rejected_by_reason"] == {
        "invalid_degradation_class": 1, "missing_source_date": 1, "unsupported_geometry_type": 1}


def test_country_scoped_identity_and_geometry_contract(config, sql):
    run_pipeline(config)
    # Same source_id in two countries are two entities, not a collision.
    assert sql("SELECT country_code FROM core.bess_site WHERE source_id = 'BESS-001' ORDER BY 1") == [("DE",), ("PL",)]
    # Everything promoted is valid MultiPolygon in the canonical CRS; delivered CRS is kept.
    assert sql("""SELECT DISTINCT ST_SRID(geom), GeometryType(geom), ST_IsValid(geom)
                  FROM core.bess_site UNION SELECT DISTINCT ST_SRID(geom), GeometryType(geom), ST_IsValid(geom)
                  FROM core.peatland_parcel""") == [(25833, "MULTIPOLYGON", True)]
    assert sql("SELECT source_srid FROM core.bess_site WHERE source_id = 'BESS-008'") == [(4326,)]
    # Missing attributes stay missing, they are not defaulted.
    assert sql("SELECT capacity_mw FROM core.bess_site WHERE source_id = 'BESS-003'") == [(None,)]
    md = (config.export_dir / "peatland").glob("*/DE_PEAT-007.md")
    assert "Peat depth (source) | not available in source - not estimated" in next(md).read_text()


def test_cli_run_exit_code_zero_on_success(config, capsys):
    code = cli.main(["--database-url", config.database_url, "--runs-dir", str(config.runs_dir), "run",
                     "--export-dir", str(config.export_dir), "--fixtures-dir", str(config.fixtures_dir)])
    assert code == 0
    assert "status=SUCCEEDED" in capsys.readouterr().out


# --- acceptance 4: rerun is safe ---------------------------------------------

def test_rerun_is_safe_and_idempotent(config, sql):
    _, _, first = run_pipeline(config)
    before_hashes = {v: dossier_hashes(config, v) for v in ("bess", "peatland")}
    before_core = sql("SELECT country_code, source_id, content_hash, promoted_by_run FROM core.bess_site ORDER BY 1, 2")

    status, _, second = run_pipeline(config)

    assert status == RunStatus.SUCCEEDED
    for v in ("bess", "peatland"):
        c = stage(second, f"promote_{v}")["counts"]
        assert (c["inserted"], c["updated"]) == (0, 0)
        assert c["dataset_version"] == stage(first, f"promote_{v}")["counts"]["dataset_version"]
        assert dossier_hashes(config, v) == before_hashes[v]  # byte-identical dossiers
    # Core rows untouched (not even rewritten), no duplicates.
    assert sql("SELECT country_code, source_id, content_hash, promoted_by_run FROM core.bess_site ORDER BY 1, 2") \
        == before_core
    assert stage(second, "migrate")["counts"]["applied_now"] == 0
    assert stage(second, "refresh_bess_view")["counts"]["mode"] == "concurrent"


def test_rerun_after_failure_recovers_everything(config):
    run_pipeline(dataclasses.replace(config, fail_stages=frozenset({"promote_bess"})))
    status, _, m = run_pipeline(config)
    assert status == RunStatus.SUCCEEDED
    assert {a["state"] for a in m["freshness"]} == {"fresh"}


def test_old_exports_are_pruned_but_latest_is_kept(config):
    cfg = dataclasses.replace(config, keep_exports=2)
    for _ in range(4):
        run_pipeline(cfg)
    dirs = [p for p in (cfg.export_dir / "bess").iterdir() if p.is_dir()]
    assert len(dirs) == 2
    assert read_json(cfg.export_dir / "bess" / "LATEST.json")["dir"] in {d.name for d in dirs}


# --- acceptance 2 + 3: a failed critical stage is never success; stale is visible

def test_failed_critical_stage_reports_failure_and_stale_state(config, sql, capsys):
    run_pipeline(config)
    # New accepted data arrives upstream (staging filled externally this time) ...
    sql("UPDATE staging.bess_site SET grid_distance_m = 1500 WHERE country_code = 'DE' AND source_id = 'BESS-002'")
    # ... and the BESS view refresh fails.
    failing = dataclasses.replace(config, fixtures_dir=None, fail_stages=frozenset({"refresh_bess_view"}))
    status, path, m = run_pipeline(failing)

    assert status == RunStatus.FAILED
    assert read_json(path)["status"] == "failed"
    assert stage(m, "promote_bess")["counts"]["updated"] == 1
    assert stage(m, "refresh_bess_view")["status"] == "failed"
    assert stage(m, "refresh_bess_view")["error"]["kind"] == "injected"
    assert stage(m, "export_bess_dossiers")["status"] == "skipped"
    assert "refresh_bess_view (failed)" in stage(m, "export_bess_dossiers")["skipped_reason"]
    # The independent vertical still delivered.
    assert stage(m, "export_peatland_dossiers")["status"] == "succeeded"

    a = artifacts(m)
    assert a["mart.mv_bess_candidates"]["state"] == "stale"
    assert a["mart.mv_bess_candidates"]["last_attempt_status"] == "failed"
    assert a["export:bess"]["state"] == "stale"
    assert "v1" in a["export:bess"]["reason"] and "v2" in a["export:bess"]["reason"]
    assert a["export:peatland"]["state"] == "fresh"

    # The CLI agrees: non-zero exit and STALE in the status output.
    capsys.readouterr()
    assert cli.main(["--database-url", config.database_url, "--runs-dir", str(config.runs_dir), "status"]) == 1
    out = capsys.readouterr().out
    assert "status=FAILED" in out and "STALE" in out and "last attempt FAILED" in out


def test_real_failure_dropped_view_fails_prerequisite(config, sql):
    run_pipeline(config)
    sql("DROP MATERIALIZED VIEW mart.mv_peatland_candidates")
    status, _, m = run_pipeline(config)

    assert status == RunStatus.FAILED
    err = stage(m, "refresh_peatland_view")["error"]
    assert err["kind"] == "prerequisite" and "does not exist" in err["message"]
    assert stage(m, "export_peatland_dossiers")["status"] == "skipped"
    assert stage(m, "export_bess_dossiers")["status"] == "succeeded"


def test_concurrent_run_is_refused_by_run_lock(config, database_url):
    with psycopg.connect(database_url, autocommit=True) as holder:
        holder.execute("SELECT pg_advisory_lock(%s)", [db.RUN_LOCK_KEY])
        status, _, m = run_pipeline(config)
    assert status == RunStatus.FAILED
    assert "another iris-pilot run holds the run lock" in stage(m, "preflight")["error"]["message"]
    assert all(s["status"] == "skipped" for s in m["stages"][1:])


def test_unreachable_database_fails_cleanly_with_manifest(config):
    bad = dataclasses.replace(config, database_url="postgresql://iris:iris@localhost:1/iris")
    status, path, m = run_pipeline(bad)
    assert status == RunStatus.FAILED
    assert stage(m, "preflight")["error"]["kind"] == "prerequisite"
    assert "unreachable" in stage(m, "preflight")["error"]["message"]
    assert "error" in read_json(path)["freshness"]


def test_tampered_export_is_visible(config, capsys):
    run_pipeline(config)
    latest = read_json(config.export_dir / "bess" / "LATEST.json")
    (config.export_dir / "bess" / latest["dir"] / "DE_BESS-001.md").write_text("edited by hand")
    capsys.readouterr()
    assert cli.main(["--database-url", config.database_url, "--runs-dir", str(config.runs_dir), "status"]) == 1
    assert "modified after export" in capsys.readouterr().out


def test_migration_drift_is_detected(config, tmp_path, monkeypatch):
    mig = tmp_path / "migrations"
    shutil.copytree(db.MIGRATIONS_DIR, mig)
    monkeypatch.setattr(db, "MIGRATIONS_DIR", mig)
    run_pipeline(config)
    (mig / "002_staging.sql").write_text((mig / "002_staging.sql").read_text() + "\n-- edited\n")
    status, _, m = run_pipeline(config)
    assert status == RunStatus.FAILED
    assert "edited after being applied" in stage(m, "migrate")["error"]["message"]


def test_interrupted_run_leaves_truthful_manifest(config, monkeypatch):
    def interrupted(*_a, **_k):
        raise KeyboardInterrupt
    monkeypatch.setattr(export, "export", interrupted)
    with pytest.raises(KeyboardInterrupt):
        run_pipeline(config)
    m = read_json(config.runs_dir / "latest.json")
    assert m["status"] == "failed"
    assert stage(m, "export_bess_dossiers")["error"]["kind"] == "aborted"


def test_empty_vertical_cannot_succeed(config, tmp_path):
    fx = tmp_path / "fixtures"
    shutil.copytree(config.fixtures_dir, fx)
    lines = (fx / "peatland_parcel.csv").read_text().splitlines()
    (fx / "peatland_parcel.csv").write_text(lines[0] + "\n")  # header only
    status, _, m = run_pipeline(dataclasses.replace(config, fixtures_dir=fx))
    assert status == RunStatus.FAILED
    assert "no rows" in stage(m, "export_peatland_dossiers")["error"]["message"]
    assert json.dumps(m)  # manifest stays serialisable
