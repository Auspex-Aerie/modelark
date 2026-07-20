"""Seed a fresh catalog from a sanitized JSONL export — the bundled starter catalog.

Onboarding gap: a fresh install has an empty catalog, and the only other way to get anything to
curate is to re-walk the whole Hugging Face org list (`discover --walk`) — minutes of API calls,
subject to rate limits, and impossible offline. Importing the shipped `models.jsonl` gives ~4k
pre-classified models to browse and select immediately, with no HF token and no network.

Scope: this seeds the `models` table only. Per-file rows (the `files` table) are deliberately NOT
shipped in the sanitized export, so size budgeting works at the model level from `total_size_bytes`
while per-file manifests are fetched on demand for the repositories actually selected. Import is
insert-only by default: it fills in models the catalog does not already have and never downgrades a
row a richer local `discover` produced (use `overwrite=True` to refresh from the seed).
"""
from __future__ import annotations

import json
from importlib import resources
from pathlib import Path

from modelark.core import db

_PACKAGED_SEED = "data/catalog_seed.jsonl"        # under modelark/ (see pyproject package-data)
_CHECKOUT_SEED = ("catalog", "export", "models.jsonl")


def seed_source(explicit: str | Path | None = None):
    """Resolve the export to import: explicit path → source checkout → packaged resource.

    Returns a `Path` or an importlib `Traversable`; both support `.read_text()`.
    """
    if explicit is not None:
        p = Path(explicit).expanduser()
        if not p.is_file():
            raise FileNotFoundError(f"catalog export does not exist: {p}")
        return p
    checkout = db.REPO_ROOT.joinpath(*_CHECKOUT_SEED)   # editable/source install convenience
    if checkout.is_file():
        return checkout
    return resources.files("modelark").joinpath(_PACKAGED_SEED)


def import_catalog(con, source: str | Path | None = None, *, overwrite: bool = False) -> dict:
    """Import model rows from a sanitized JSONL export into the (auto-created) catalog.

    Insert-only unless `overwrite`, so re-running is safe and never clobbers locally discovered rows.
    Returns {imported, skipped, source, total_models}.
    """
    src = seed_source(source)
    text = src.read_text(encoding="utf-8")
    columns = {r[1] for r in con.execute("PRAGMA table_info(models)").fetchall()}

    imported = skipped = 0
    # BEGIN IMMEDIATE takes the write lock up front, so the existing-rows snapshot and every insert are
    # one atomic unit: a concurrent writer (e.g. the portal fill worker) cannot slip a row in after the
    # snapshot and have insert-only mode silently overwrite it, preserving the "never downgrade a
    # locally discovered row" guarantee. A plain (deferred) BEGIN would not lock until the first write.
    con.execute("BEGIN IMMEDIATE")
    try:
        existing = {r[0] for r in con.execute("SELECT repo_id FROM models").fetchall()}
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            repo_id = row.get("repo_id")
            if not repo_id:
                continue
            if repo_id in existing and not overwrite:
                skipped += 1
                continue
            # Non-scalar values (notably `tags`, a JSON array in the export) are stored as JSON text,
            # matching discover.persist_info; SQLite cannot bind a list/dict directly.
            clean = {k: (json.dumps(v) if isinstance(v, (list, dict)) else v)
                     for k, v in row.items() if k in columns}
            db.upsert(con, "models", clean, pk=["repo_id"])
            existing.add(repo_id)
            imported += 1
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise

    total = con.execute("SELECT count(*) FROM models").fetchone()[0]
    return {"imported": imported, "skipped": skipped, "source": str(src), "total_models": total}
