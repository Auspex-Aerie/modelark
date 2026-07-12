"""Shared catalog access for the portal: one connection, a lock, and a flat
`ui_cache` table materialized once so requests don't re-aggregate `files`."""
from __future__ import annotations

import threading

from modelark.core import db

DEFAULT_BUDGET_TB = 27.0
_lock = threading.Lock()
_con = None
total = 0

_BUCKET = ("CASE WHEN category!='generative-llm' THEN 'non-LLM' "
           "WHEN params_b IS NULL THEN '?' "
           "WHEN params_b<=8 THEN '≤8B' WHEN params_b<=32 THEN '8–32B' "
           "WHEN params_b<=70 THEN '32–70B' WHEN params_b<=200 THEN '70–200B' "
           "ELSE '>200B' END")
BUCKETS = ["≤8B", "8–32B", "32–70B", "70–200B", ">200B", "non-LLM"]


def conn():
    global _con
    if _con is None:
        _con = db.connect()  # read-write: the portal writes selection + wishlist status
    return _con


def q(sql, params=()):
    with _lock:
        return conn().execute(sql, list(params)).fetchall()


def build_cache() -> int:
    """Snapshot v_ui (the files aggregation) into a flat temp table once."""
    global total
    with _lock:
        c = conn()
        c.execute("DROP TABLE IF EXISTS ui_cache")        # SQLite has no CREATE OR REPLACE TABLE
        c.execute(
            f"CREATE TEMP TABLE ui_cache AS "
            f"SELECT repo_id, author, params_b, category, variant, license, downloads_30d, "
            f"gated, bytes, {_BUCKET} AS bucket FROM v_ui")
        total = c.execute("SELECT count(*) FROM ui_cache").fetchone()[0]
    return total
