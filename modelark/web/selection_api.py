"""Selection API — the build cart (`selection` table) and the two-stage Finish.

Ticking a row stages it in the cart (sequential building). "Finish" stamps
`finalized_at` on the whole current cart → that's the committed wishlist the
fetch pipeline reads (rows with finalized_at NOT NULL).
"""
from __future__ import annotations

from modelark.web import data


def summary() -> dict:
    by = data.q("SELECT v.category, count(*), sum(v.bytes) FROM selection s "
                "JOIN ui_cache v USING(repo_id) GROUP BY 1 ORDER BY 3 DESC")
    recent = dict(data.q(
        "SELECT category, repo_id FROM (SELECT v.category, s.repo_id, "
        "row_number() OVER (PARTITION BY v.category ORDER BY s.added_at DESC, s.repo_id) rn "
        "FROM selection s JOIN ui_cache v USING(repo_id)) WHERE rn=1"))
    tot = data.q("SELECT count(*), coalesce(sum(v.bytes),0) "
                 "FROM selection s JOIN ui_cache v USING(repo_id)")[0]
    fin = data.q("SELECT count(*) FROM selection WHERE finalized_at IS NOT NULL")[0][0]
    return {
        "n": tot[0], "bytes": tot[1], "finalized": fin, "budget": data.DEFAULT_BUDGET_TB,
        "by_cat": [{"cat": c, "n": n, "bytes": b, "recent": recent.get(c)} for c, n, b in by],
    }


def toggle(repo_id: str, on: bool) -> dict:
    if on:
        data.q("INSERT INTO selection(repo_id) VALUES (?) ON CONFLICT DO NOTHING", [repo_id])
    else:
        data.q("DELETE FROM selection WHERE repo_id=?", [repo_id])
    return summary()


def bulk(ids: list[str], on: bool) -> dict:
    with data._lock:
        c = data.conn()
        if on:
            c.executemany("INSERT INTO selection(repo_id) VALUES (?) ON CONFLICT DO NOTHING",
                          [[i] for i in ids])
        else:
            c.executemany("DELETE FROM selection WHERE repo_id=?", [[i] for i in ids])
    return summary()


def clear() -> dict:
    data.q("DELETE FROM selection")
    return summary()


def finalize() -> dict:
    """Commit the current cart: stamp the whole un-finalized set as the wishlist."""
    data.q("UPDATE selection SET finalized_at = CURRENT_TIMESTAMP WHERE finalized_at IS NULL")
    return summary()


def export_ids() -> list[str]:
    return [r[0] for r in data.q("SELECT repo_id FROM selection ORDER BY repo_id")]
