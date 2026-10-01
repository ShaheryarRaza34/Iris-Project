"""Run manifest: atomic JSON on disk plus a concise console rendering."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

MANIFEST_SCHEMA_VERSION = 1


def write_json_atomic(path: Path, payload: Any) -> None:
    """Write via temp file + os.replace so readers never see a torn file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(payload, fh, indent=2, sort_keys=False, default=str)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _fmt_counts(counts: dict[str, Any], prefix: str = "") -> str:
    """Scalars inline; one level of nested dicts as key.sub=value; lists omitted
    (full detail such as per-row rejections stays in the JSON manifest)."""
    parts = []
    for k, v in counts.items():
        if isinstance(v, dict) and not prefix:
            parts.append(_fmt_counts(v, prefix=f"{k}."))
        elif isinstance(v, (int, float, str)) and not isinstance(v, bool):
            parts.append(f"{prefix}{k}={v}")
    return " ".join(p for p in parts if p)


_STAGE_TAG = {"succeeded": "OK  ", "failed": "FAIL", "skipped": "SKIP", "running": "RUN?", "pending": "...."}


def render_summary(manifest: dict[str, Any]) -> str:
    lines = [
        f"run {manifest['run_id']}  status={manifest['status'].upper()}  "
        f"started={manifest['started_at']}  finished={manifest.get('finished_at') or '-'}",
        "stages:",
    ]
    width = max((len(s["name"]) for s in manifest["stages"]), default=10)
    for s in manifest["stages"]:
        tag = _STAGE_TAG.get(s["status"], s["status"])
        crit = "" if s["critical"] else " (non-critical)"
        dur = f"{s['duration_ms']}ms" if s["duration_ms"] is not None else "-"
        line = f"  [{tag}] {s['name']:<{width}} {dur:>8}{crit}"
        if s["status"] == "succeeded" and s["counts"]:
            line += "  " + _fmt_counts(s["counts"])
        if s["error"]:
            line += f"\n         -> {s['error']['type']}: {s['error']['message']}"
        if s["skipped_reason"]:
            line += f"\n         -> {s['skipped_reason']}"
        lines.append(line)
    fresh = manifest.get("freshness")
    if isinstance(fresh, list):
        lines.append("artifacts:")
        lines.extend(render_freshness(fresh))
    elif fresh:
        lines.append(f"artifacts: unavailable ({fresh.get('error')})")
    return "\n".join(lines)


def render_freshness(rows: list[dict[str, Any]]) -> list[str]:
    width = max((len(r["artifact"]) for r in rows), default=10)
    out = []
    for r in rows:
        line = f"  {r['artifact']:<{width}} {r['state'].upper():<11} {r['reason']}"
        if r.get("last_attempt_status") == "failed":
            line += f"\n  {'':<{width}} last attempt FAILED in run {r['last_attempt_run']}: {r['last_error']}"
        out.append(line)
    return out
