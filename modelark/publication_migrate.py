"""Read-only annex-payload conversion inspect. Does not apply or cut over live catalogs."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from modelark.publication_policy import PublicationRefused
from modelark.publication_store import library


_PAYLOAD_PREFIX = "__modelark_payload_v1__"


def _state(row):
    annex, digest, size = row["annex_key"], row["orig_sha256"], row["orig_bytes"]
    if annex:
        return "already-converted-with-proof"
    if not digest or size is None:
        return "needs-evidence"
    return "convertible"


def _mapping(rfilename):
    digest = hashlib.sha256(str(rfilename).encode("utf-8")).hexdigest()
    return f"{_PAYLOAD_PREFIX}/p-{digest}.blob"


def inspect_conversion(con, *, drive=None, repos=None):
    """Read-only census of archived copies without annex keys.

    Opens no Git, writes no SQLite, starts no generation. Live apply/cutover
    remains a separate authorized step.
    """
    identity = library(con)
    clauses = ["(annex_key IS NULL OR annex_key='')"]
    params = []
    if drive is not None:
        clauses.append("drive_label=?")
        params.append(drive)
    if repos:
        clauses.append(f"repo_id IN ({','.join('?' * len(repos))})")
        params.extend(repos)
    columns = ("drive_label", "repo_id", "rfilename", "orig_sha256", "orig_bytes",
               "stored_relpath", "stored_name", "annex_key")
    rows = con.execute(
        f"SELECT {','.join(columns)} FROM archived WHERE {' AND '.join(clauses)} "
        "ORDER BY drive_label, repo_id, rfilename",
        params,
    ).fetchall()
    candidates = []
    counts = {}
    for raw in rows:
        item = dict(zip(columns, raw))
        item["state"] = _state(item)
        item["proposed_stored_relpath"] = _mapping(item["rfilename"])
        candidates.append(item)
        counts[item["state"]] = counts.get(item["state"], 0) + 1
    return {
        "kind": "annex-migrate-inspect",
        "inspected_at": datetime.now(timezone.utc).isoformat(),
        "library_id": None if identity is None else identity[0],
        "map_uuid": None if identity is None else identity[1],
        "drive": drive,
        "repos": None if repos is None else list(repos),
        "counts": counts,
        "candidates": candidates,
        "apply": "disabled-until-explicit-cutover",
    }


def apply_conversion(*_a, **_k):
    raise PublicationRefused("PUBLICATION_CONVERSION_DISABLED")


def resume_conversion(*_a, **_k):
    raise PublicationRefused("PUBLICATION_CONVERSION_DISABLED")


def plan_json(plan):
    return json.dumps(plan, indent=2, sort_keys=True) + "\n"
