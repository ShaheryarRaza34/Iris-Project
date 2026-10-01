# IRIS-CAND-22 — One-Command Pilot Orchestration

A lean, repeatable pipeline that promotes accepted data, refreshes the BESS and peatland
candidate views, and exports sample prospecting dossiers. One command runs the whole thing.
Every stage's outcome, counts, timings and failure details go to a run manifest, and
stale or failed artifacts are reported explicitly.

```
iris-pilot run       # the one command
iris-pilot status    # freshness of every view/export + latest run
iris-pilot stages    # the stage graph
```

## Setup (clean checkout)

Requirements: Python 3.12+, Docker (for PostgreSQL 16 + PostGIS 3.4).

```bash
docker compose up -d --wait          # PostGIS on localhost:5433 (iris/iris)
python -m pip install -e ".[dev]"
iris-pilot run                       # or: python -m iris_pilot run
python -m pytest -q                  # 28 tests; DB tests use throwaway databases
```

There is also a `Makefile`: `make run`, `make status`, `make test`, `make drill`.

To use another server, set `IRIS_DATABASE_URL` (for `run`/`status`) or
`IRIS_TEST_ADMIN_URL` (for tests). The tests need a role that can `CREATE DATABASE`.

## What a run looks like

```
run 20261001T174743239010Z-d8f5  status=FAILED  ...
stages:
  [OK  ] preflight                    49ms  server_version=160004 postgis_version=3.4.3 run_lock=acquired
  [OK  ] migrate                       9ms  applied_now=0 already_applied=5
  [OK  ] promote_bess                 72ms  staged=10 eligible=5 inserted=0 updated=1 unchanged=4 ... dataset_version=2
  [OK  ] promote_peatland             20ms  staged=8 eligible=4 inserted=0 updated=0 unchanged=4 ...
  [FAIL] refresh_bess_view             7ms
         -> InjectedFailure: failure injected into stage 'refresh_bess_view'
  [OK  ] refresh_peatland_view        29ms  row_count=4 built_from_version=1 mode=concurrent ...
  [SKIP] export_bess_dossiers            -
         -> upstream not successful: refresh_bess_view (failed)
  [OK  ] export_peatland_dossiers     74ms  dossiers=3 built_from_version=1 ...
  [SKIP] prune_old_exports               - (non-critical)
artifacts:
  mart.mv_bess_candidates     STALE       built from bess v1, promoted data is at v2
                              last attempt FAILED in run 20261001T...-d8f5: InjectedFailure: ...
  export:bess                 STALE       built from bess v1, promoted data is at v2
  mart.mv_peatland_candidates FRESH       built from v1 by run 20261001T...-d8f5
  export:peatland             FRESH       built from v1 by run 20261001T...-d8f5
```

Exit codes for `run`: `0` succeeded, `1` failed, `3` succeeded with non-critical warnings.
`status` exits non-zero unless the latest run succeeded and every artifact is fresh.

## Stages

| # | Stage | Critical | Requires | Does |
|---|---|---|---|---|
| 1 | `preflight` | yes | – | DB reachable, PG ≥ 16, PostGIS ≥ 3.4, takes the run lock, fixtures present, export dir writable |
| 2 | `migrate` | yes | preflight | applies pending SQL migrations; fails on edited or unknown applied migrations |
| 3 | `load_staging_fixtures` | yes | migrate | upserts committed CSV fixtures into `staging.*` (omitted with `--no-fixtures`) |
| 4–5 | `promote_bess`, `promote_peatland` | yes | staging | accepted, contract-valid rows → `core.*` |
| 6–7 | `refresh_bess_view`, `refresh_peatland_view` | yes | promote_* | `REFRESH MATERIALIZED VIEW`, row-count postcondition |
| 8–9 | `export_bess_dossiers`, `export_peatland_dossiers` | yes | refresh_* | sample two-page dossiers + `index.json` |
| 10 | `prune_old_exports` | no | both exports | keeps the last N export runs per vertical, clears crashed temp dirs |

The two verticals are independent branches. If BESS fails, peatland still delivers its
output (so one problem doesn't hide the rest), but the run is still `failed`. Use `--fail-fast`
to stop everything after the first critical failure.

## How the acceptance criteria are met

**1. One command produces both vertical outputs.** `iris-pilot run` runs the whole graph and
writes `exports/bess/<run_id>/` and `exports/peatland/<run_id>/`, plus a `LATEST.json`
pointer for each. An empty vertical counts as a failure, not a quiet success:
export refuses to run when its view has no rows.

**2. A failed critical stage cannot be reported as success.** The run status has one source of
truth, `runner.compute_run_status`. Any critical stage that is not explicitly `succeeded`
(failed, skipped, or still running after a crash) makes the run `failed`. The status is always
re-derived from the stage results in a `finally` block, including on Ctrl-C. A test checks
every status/criticality combination, and I confirmed it fails when that rule is weakened.

**3. Stale view/export state is visible.** Promotion bumps a per-vertical `ops.dataset_version`
**only when promoted content actually changed**. Each view and export records in
`ops.artifact_state` the version it was built from, plus its last attempt and error. That gives
four states:

- `fresh`
- `stale`: built from an older version, or the view isn't populated
- `never_built`
- `missing`: the export directory, a dossier file, or `LATEST.json` is gone, or a checksum no
  longer matches `index.json`

These appear in every manifest and in `iris-pilot status`. Export also refuses to build from a
stale view.

**4. Rerun is safe.**

- A session-level advisory lock rejects concurrent runs.
- Migrations are tracked and checksummed.
- Fixture load and promotion are upserts keyed on `(country_code, source_id)` that only write
  when the content hash changes. On unchanged input a rerun inserts and updates nothing, and
  views and exports stay at the same data version.
- Dossier bodies are a pure function of the data, so reruns produce byte-identical files.
- Exports are built in `.tmp-<run_id>/`, renamed into place, and only then is `LATEST.json`
  swapped atomically.
- Manifests are written via temp file + `os.replace`.
- After any failure, a plain rerun is the recovery path.

## Run manifest

Each run writes `runs/<run_id>.json`, and `runs/latest.json` is a copy of the newest. The manifest
is rewritten after every stage transition. A process killed mid-run therefore leaves a manifest
stuck in `running`, which `status` reports as **INCOMPLETE** (it checks whether anyone still
holds the run lock). The manifest contains:

- run id, status, start/finish timestamps, the command line, and config (DB password redacted)
- per stage: status, critical flag, dependencies, timestamps, duration, counts, and either an
  error (`kind`: `prerequisite` / `stage_error` / `injected` / `unexpected` / `aborted`, type,
  message, traceback tail) or a skip reason
- promotion details: counts by rejection reason and the per-row list of rejected
  `(country_code, source_id, reason)`
- a freshness snapshot of every artifact, and the output pointers

## Data contracts

- **Layers:** `staging` holds rows as delivered, with an untyped `geom` and the review decision.
  `core` holds the promoted rows. `mart` holds the screening views. `ops` holds orchestration
  metadata.
- **Identity:** every business table has a non-null `country_code`, and primary keys and joins
  use `(country_code, source_id)`. The fixtures include `BESS-001` in both DE and PL, and they
  are promoted as two separate entities.
- **Geometry:** the column is always `geom`. In `core` it is
  `geometry(MultiPolygon, 25833)` with `CHECK (ST_IsValid(geom))`. EPSG:25833 is metric, so areas
  come out in m².
- **Promotion rejects, never repairs.** The reasons are:
  - `not_accepted`
  - `missing_geometry`
  - `missing_crs` (SRID 0; a CRS is never guessed)
  - `unsupported_geometry_type`
  - `invalid_geometry` (no silent `ST_MakeValid`)
  - `empty_geometry`
  - `missing_source_date`
  - checks specific to each vertical: capacity, distance, degradation class, depth

  A geometry delivered in a *declared* other CRS is reprojected explicitly. The original SRID is
  kept in `source_srid`, counted as `reprojected`, and named in the dossier.
- **Missing optional attributes stay NULL.** Dossiers print "not available in source - not
  estimated", and screening sends those rows to `review` instead of assuming a value.
- **Units are in column names** (`_m`, `_mw`, `area_m2`). Every dossier carries its source date,
  CRS, dataset version and the project's uncertainty wording, word for word.

The fixtures (`fixtures/*.csv`) are small and deterministic, and each one exercises one of these
rules. The expected counts are asserted in the tests.

## Code layout

```
iris_pilot/
  runner.py      generic stage runner + run-status rule (no DB knowledge)
  pipeline.py    stage graph, preflight, run entrypoint, manifest lifecycle
  manifest.py    atomic JSON writes, console rendering
  db.py          connection, run lock, checksummed migrations
  fixtures.py    CSV -> staging upsert (stand-in for ingestion adapters)
  promote.py     classify + content-hash upsert + dataset version bump
  views.py       MV refresh (concurrent once populated) + artifact state
  export.py      dossier rendering, atomic export, verification, pruning
  freshness.py   fresh/stale/never_built/missing per artifact
  verticals.py   BESS and peatland specs, CRS, uncertainty wording
  migrations/    001..005 SQL
tests/
  test_runner.py       runner semantics (no DB)
  test_pipeline_db.py  end-to-end against PostGIS
```

## Assumptions and deliberate simplifications

| Simplification | Production evolution |
|---|---|
| Fixtures stand in for ingestion: `load_staging_fixtures` fills staging from CSV. | Ingestion adapters write staging; run with `--no-fixtures` (already supported) or replace the stage with an adapter handoff check. |
| Screening uses source attributes (`grid_distance_m`, degradation class) and simple, explainable bands. | Spatial screening against reference layers (grid infrastructure, protected areas) computed in PostGIS, with rule versions recorded. |
| Promotion only adds and updates rows; a row that is later rejected in staging is not retracted from `core`. | Add retraction (`retired_at`) handling that also bumps the dataset version. |
| Dossiers are Markdown with a CSS page break as the "two-page" boundary. | Render to PDF from the same data, with a map extract. |
| Freshness and run history live in `ops.*` and JSON files. | Ship manifests and artifact state to the team's monitoring and alerting; keep run rows in the DB for history. |
| One canonical CRS (EPSG:25833) for a one-region pilot. | Canonical SRID chosen per country/region in the adapter config. |
| Ops metadata (`ops.*`) is not country-scoped, because it describes pipeline artifacts, not business entities. | Scope versions/artifacts per `(country_code, vertical)` once more than one region runs independently. |
| A sequential in-process runner, no scheduler. | The stage/manifest contract maps directly onto a scheduler task graph (e.g. Dagster/Airflow) if one is ever needed. |

`--fail-stage STAGE` (repeatable) injects a failure for drills. `make drill` shows a BESS
refresh failure while peatland still delivers.
