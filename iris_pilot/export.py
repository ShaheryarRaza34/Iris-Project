"""Two-page Markdown prospecting dossiers, exported from the candidate views.

Dossier bodies are a pure function of the view row, so reruns on unchanged
data produce byte-identical files. Run-specific metadata lives in index.json.

Layout per vertical:
    <export_dir>/<vertical>/<run_id>/<CC>_<source_id>.md
    <export_dir>/<vertical>/<run_id>/index.json
    <export_dir>/<vertical>/LATEST.json   -> points at the current run dir

A run builds into .tmp-<run_id>/ and renames it into place, then swaps
LATEST.json atomically, so readers never see a half-written export.
"""

from __future__ import annotations

import hashlib
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

from .manifest import read_json, write_json_atomic
from .runner import StageError
from .verticals import CANONICAL_CRS_LABEL, CANONICAL_SRID, UNCERTAINTY_WORDING, Vertical
from .views import dataset_version, record_built

PAGE_BREAK = '<div style="page-break-after: always"></div>'
NOT_IN_SOURCE = "not available in source - not estimated"

BAND_ORDER = "CASE screening_band WHEN 'shortlist' THEN 1 WHEN 'review' THEN 2 ELSE 3 END"


def check_export(conn: psycopg.Connection, v: Vertical) -> list[str]:
    """Never export from a stale or empty view."""
    row = conn.execute(
        "SELECT built_from_version, row_count FROM ops.artifact_state WHERE artifact = %s",
        [v.view_artifact],
    ).fetchone()
    current = dataset_version(conn, v.key)
    problems = []
    if row is None or row[0] is None:
        problems.append(f"{v.view} has never been built")
    else:
        if row[0] < current:
            problems.append(f"{v.view} is stale (built from v{row[0]}, data at v{current})")
        if not row[1]:
            problems.append(f"{v.view} has no rows - nothing to export")
    return problems


def _fetch(conn: psycopg.Connection, v: Vertical, limit: int) -> list[dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(
            f"""
            SELECT *,
                   ST_X(pt) AS pt_x, ST_Y(pt) AS pt_y,
                   ST_X(ST_Transform(pt, 4326)) AS lon, ST_Y(ST_Transform(pt, 4326)) AS lat,
                   ST_XMin(geom) AS xmin, ST_YMin(geom) AS ymin,
                   ST_XMax(geom) AS xmax, ST_YMax(geom) AS ymax
            FROM (SELECT *, ST_PointOnSurface(geom) AS pt FROM {v.view}) c
            ORDER BY {BAND_ORDER}, area_m2 DESC, country_code, source_id
            LIMIT %s
            """,
            [limit],
        ).fetchall()


def _num(value: Any, fmt: str, unit: str) -> str:
    return NOT_IN_SOURCE if value is None else f"{format(value, fmt)} {unit}"


def _table(rows: list[tuple[str, str]]) -> str:
    return "\n".join(["| Attribute | Value |", "|---|---|", *(f"| {k} | {val} |" for k, val in rows)])


def render_dossier(v: Vertical, r: dict[str, Any], version: int) -> str:
    common_head = [
        ("Country", r["country_code"]),
        ("Source ID", r["source_id"]),
        ("Area", f"{r['area_m2']:,} m2 ({r['area_m2'] / 10_000:.2f} ha)"),
    ]
    if v.key == "bess":
        specific = [
            ("Capacity (source)", _num(r["capacity_mw"], ",.2f", "MW")),
            ("Distance to grid (source)", _num(r["grid_distance_m"], ",.0f", "m")),
        ]
    else:
        specific = [
            ("Degradation class", r["degradation_class"].replace("_", "-")),
            ("Peat depth (source)", _num(r["peat_depth_m"], ".2f", "m")),
            ("Eco-points (indicative)",
             f"{r['eco_points_indicative']:,} ({r['eco_point_factor_per_m2']} eco-points/m2 commercial baseline)"),
        ]
    location = [
        ("Screening band", f"**{r['screening_band']}**"),
        ("Screening rule", r["screening_reason"]),
        ("Representative point", f"E {r['pt_x']:.1f} / N {r['pt_y']:.1f} ({CANONICAL_CRS_LABEL}); "
                                 f"{r['lon']:.5f} E, {r['lat']:.5f} N (WGS84)"),
        ("Bounding box", f"E {r['xmin']:.1f}-{r['xmax']:.1f} / N {r['ymin']:.1f}-{r['ymax']:.1f}"),
    ]
    source_crs = (CANONICAL_CRS_LABEL if r["source_srid"] == CANONICAL_SRID
                  else f"EPSG:{r['source_srid']}, reprojected to {CANONICAL_CRS_LABEL}")
    provenance = [
        ("Source date", r["source_date"].isoformat()),
        ("Delivered CRS", source_crs),
        ("Units", "area m2 (planar, canonical CRS); " +
                  ("grid distance m; capacity MW" if v.key == "bess" else "peat depth m; eco-points per m2")),
        ("Dataset version", f"{v.key} v{version}"),
        ("Candidate view", v.view),
    ]
    missing = [label for label, val in specific if val == NOT_IN_SOURCE]

    return "\n".join([
        f"# {v.title} prospecting dossier - {r['name']}",
        "",
        f"`{r['country_code']}/{r['source_id']}`",
        "",
        f"> {UNCERTAINTY_WORDING}",
        "",
        "## Page 1 - Site summary",
        "",
        _table(common_head + specific + location),
        "",
        PAGE_BREAK,
        "",
        "## Page 2 - Provenance and caveats",
        "",
        _table(provenance),
        "",
        "### Data gaps",
        "",
        *([f"- {m}: {NOT_IN_SOURCE}" for m in missing] or ["- none in the screened attributes"]),
        "",
        "### Uncertainty",
        "",
        UNCERTAINTY_WORDING,
        "",
    ])


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", name)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def export(conn: psycopg.Connection, v: Vertical, run_id: str, export_dir: Path, sample_size: int) -> dict:
    with conn.transaction():
        version = dataset_version(conn, v.key)
        rows = _fetch(conn, v, sample_size)
    if not rows:
        raise StageError(f"{v.view} returned no rows to export")

    vertical_dir = export_dir / v.key
    tmp = vertical_dir / f".tmp-{run_id}"
    final = vertical_dir / run_id
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True)

    dossiers = []
    for r in rows:
        fname = f"{_safe(r['country_code'])}_{_safe(r['source_id'])}.md"
        path = tmp / fname
        path.write_text(render_dossier(v, r, version), encoding="utf-8", newline="\n")
        dossiers.append({"file": fname, "country_code": r["country_code"], "source_id": r["source_id"],
                         "screening_band": r["screening_band"], "sha256": _sha256(path)})

    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    index = {"vertical": v.key, "run_id": run_id, "generated_at": generated_at, "view": v.view,
             "built_from_version": version, "sample_size": sample_size, "dossiers": dossiers}
    write_json_atomic(tmp / "index.json", index)
    tmp.rename(final)
    write_json_atomic(vertical_dir / "LATEST.json", {
        "run_id": run_id, "dir": run_id, "generated_at": generated_at,
        "built_from_version": version, "dossier_count": len(dossiers),
    })

    with conn.transaction():
        record_built(conn, v.export_artifact, version=version, run_id=run_id,
                     row_count=len(dossiers), location=str(final.resolve()))
    return {"dossiers": len(dossiers), "built_from_version": version, "path": str(final)}


def verify_export(location: str | None) -> str | None:
    """Return a problem description, or None if the export on disk is intact."""
    if not location:
        return "no export location recorded"
    d = Path(location)
    index_path = d / "index.json"
    if not index_path.exists():
        return f"export directory missing or incomplete: {d}"
    index = read_json(index_path)
    for item in index["dossiers"]:
        p = d / item["file"]
        if not p.exists():
            return f"dossier missing: {p.name}"
        if _sha256(p) != item["sha256"]:
            return f"dossier modified after export: {p.name}"
    latest = d.parent / "LATEST.json"
    if not latest.exists() or read_json(latest).get("dir") != d.name:
        return f"LATEST.json does not point at {d.name}"
    return None


def prune(export_dir: Path, keep: int, current_run: str) -> dict:
    """Keep the newest `keep` run directories per vertical, never the LATEST one,
    and clear leftover temp dirs from crashed runs (the run lock guarantees no
    other run is writing)."""
    removed, tmp_removed = 0, 0
    for vdir in sorted(p for p in export_dir.iterdir() if p.is_dir()) if export_dir.exists() else []:
        latest = vdir / "LATEST.json"
        protected = {read_json(latest)["dir"]} if latest.exists() else set()
        for t in vdir.glob(".tmp-*"):
            if t.name != f".tmp-{current_run}":
                shutil.rmtree(t)
                tmp_removed += 1
        runs = sorted((p for p in vdir.iterdir() if p.is_dir() and not p.name.startswith(".")),
                      key=lambda p: p.name, reverse=True)
        for old in runs[keep:]:
            if old.name not in protected:
                shutil.rmtree(old)
                removed += 1
    return {"removed_run_dirs": removed, "removed_tmp_dirs": tmp_removed, "kept_per_vertical": keep}
