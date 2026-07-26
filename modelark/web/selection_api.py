"""Selection API — the build cart (`selection` table) and the two-stage Finish.

Ticking a row stages it in the cart (sequential building). "Finish" stamps
`finalized_at` on the whole current cart → that's the committed wishlist the
fetch pipeline reads (rows with finalized_at NOT NULL).
"""
from __future__ import annotations

from modelark import wishlist
from modelark.core import telemetry
from modelark.web import data, fill_worker

log = telemetry.get_logger("modelark.selection")

# RFC-002/DEC-049 (#39 slice 1): a live process-local Fill controller owns the finalized selection,
# so portal finalize + every removal path are refused until it stops; additions and reads stay
# allowed. The guard is portal-scoped — external CLI writers remain a documented residual until #39.
_FILL_ACTIVE_REFUSAL = {
    "ok": False,
    "refused": True,
    "code": "FILL_SESSION_ACTIVE",
    "error": "Selection finalization and removal are blocked while Fill is running.",
    "actions": ["wait_for_fill", "stop_fill"],
}


def _guarded(mutate) -> dict:
    """Run a selection graph-mutation unless a Fill controller is live, in which case return the
    typed refusal without changing any selection row. The worker primitive is dependency-free and
    signals refusal with ``None``; the typed refusal contract lives here."""
    result = fill_worker.WORKER.guarded_mutation(mutate)
    return _FILL_ACTIVE_REFUSAL if result is None else result


def _summary_on(con) -> dict:
    """Build selection summary using ``con`` only (no nested data._lock acquisition).

    Finding 43: write + revision bump hold data._lock; summary must not re-enter data.q().
    """
    try:
        by = con.execute(
            "SELECT v.category, count(*), sum(v.bytes) FROM selection s "
            "JOIN ui_cache v USING(repo_id) GROUP BY 1 ORDER BY 3 DESC").fetchall()
        recent = dict(con.execute(
            "SELECT category, repo_id FROM (SELECT v.category, s.repo_id, "
            "row_number() OVER (PARTITION BY v.category ORDER BY s.added_at DESC, s.repo_id) rn "
            "FROM selection s JOIN ui_cache v USING(repo_id)) WHERE rn=1").fetchall())
        tot = con.execute(
            "SELECT count(*), coalesce(sum(v.bytes),0) "
            "FROM selection s JOIN ui_cache v USING(repo_id)").fetchone()
    except Exception:
        by, recent = [], {}
        tot = con.execute("SELECT count(*), 0 FROM selection").fetchone()
    fin = con.execute(
        "SELECT count(*) FROM selection WHERE finalized_at IS NOT NULL").fetchone()[0]
    return {
        "n": tot[0], "bytes": tot[1], "finalized": fin, "budget": data.DEFAULT_BUDGET_TB,
        "cap_24h_gb": wishlist.download()["max_24h_gb"],
        "by_cat": [{"cat": c, "n": n, "bytes": b, "recent": recent.get(c)} for c, n, b in by],
    }


def _with_revision(body) -> dict:
    """Run selection mutation + planner_revision bump under the shared portal connection lock.

    Finding 43: selection write and revision bump must not interleave with other portal writers,
    and summary must not re-acquire the lock mid-transaction (deadlock risk with data.q).
    """
    from modelark.proposal import GraphResult, graph_write

    with data._lock:
        def op(c):
            return GraphResult(proven_noop=False, value=body(c))
        return graph_write(data.conn(), op).value


def summary() -> dict:
    """Read-only summary (takes the shared lock once via data.q / unlocked path under write)."""
    try:
        by = data.q("SELECT v.category, count(*), sum(v.bytes) FROM selection s "
                    "JOIN ui_cache v USING(repo_id) GROUP BY 1 ORDER BY 3 DESC")
        recent = dict(data.q(
            "SELECT category, repo_id FROM (SELECT v.category, s.repo_id, "
            "row_number() OVER (PARTITION BY v.category ORDER BY s.added_at DESC, s.repo_id) rn "
            "FROM selection s JOIN ui_cache v USING(repo_id)) WHERE rn=1"))
        tot = data.q("SELECT count(*), coalesce(sum(v.bytes),0) "
                     "FROM selection s JOIN ui_cache v USING(repo_id)")[0]
    except Exception:
        by, recent = [], {}
        tot = data.q("SELECT count(*), 0 FROM selection")[0]
    fin = data.q("SELECT count(*) FROM selection WHERE finalized_at IS NOT NULL")[0][0]
    return {
        "n": tot[0], "bytes": tot[1], "finalized": fin, "budget": data.DEFAULT_BUDGET_TB,
        "cap_24h_gb": wishlist.download()["max_24h_gb"],
        "by_cat": [{"cat": c, "n": n, "bytes": b, "recent": recent.get(c)} for c, n, b in by],
    }


def toggle(repo_id: str, on: bool) -> dict:
    if on:                                          # addition: never Fill-guarded; still bumps revision
        def body(c):
            c.execute(
                "INSERT INTO selection(repo_id) VALUES (?) ON CONFLICT DO NOTHING", [repo_id])
            return _summary_on(c)
        return _with_revision(body)

    def mutate():                                   # deselect: guarded while Fill is live
        def body(c):
            c.execute("DELETE FROM selection WHERE repo_id=?", [repo_id])
            return _summary_on(c)
        return _with_revision(body)
    return _guarded(mutate)


def bulk(ids: list[str], on: bool) -> dict:
    if on:                                          # bulk addition: never Fill-guarded; still bumps
        def body(c):
            c.executemany(
                "INSERT INTO selection(repo_id) VALUES (?) ON CONFLICT DO NOTHING",
                [[i] for i in ids])
            return _summary_on(c)
        return _with_revision(body)

    def mutate():                                   # bulk removal: guarded while Fill is live
        def body(c):
            c.executemany("DELETE FROM selection WHERE repo_id=?", [[i] for i in ids])
            return _summary_on(c)
        return _with_revision(body)
    return _guarded(mutate)


def clear() -> dict:
    def mutate():
        def body(c):
            c.execute("DELETE FROM selection")
            return _summary_on(c)
        return _with_revision(body)
    return _guarded(mutate)


def finalize() -> dict:
    """Commit the current cart: stamp the whole un-finalized set as the wishlist."""
    def mutate():
        def body(c):
            c.execute(
                "UPDATE selection SET finalized_at = CURRENT_TIMESTAMP WHERE finalized_at IS NULL")
            return _summary_on(c)
        return _with_revision(body)
    return _guarded(mutate)


def oversize(body: dict) -> dict:
    """Log that a build set exceeds the 24h download cap — a considerate-use nudge, not a block. The
    Catalog page POSTs this once when the selection first crosses the cap (it also shows a dismissable
    banner), leaving a durable record that the tool discouraged over-grabbing."""
    log.warning("build set exceeds 24h download cap",
                selected_gb=round(float(body["selected_gb"])), cap_gb=round(float(body["cap_gb"])))
    return {"ok": True}


def export_ids() -> list[str]:
    return [r[0] for r in data.q("SELECT repo_id FROM selection ORDER BY repo_id")]
