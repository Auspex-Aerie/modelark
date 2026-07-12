"""Re-plan-per-drive-batch + the drive write-probe (the INC drive-01 hit: a fill marching off a
half-empty NAS, and a mounted-but-dead drive getting its whole assignment silently skipped).

Covers:
  • _writable          — probes real writability (mounted != healthy); OSError → False.
  • _await_drive       — a mounted-but-UNWRITABLE drive is awaited, never accepted (no silent skip).
  • execute() re-plan  — the primary tier re-plans each pass, fills the priority drive first, then
                         advances, then does replica copy #2, and TERMINATES (GATE-C ok).
  • loop-guard         — a repo that never places is blocked after _MAX_REPO_ATTEMPTS, so one bad
                         repo can't spin the loop forever.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest import mock

from modelark.core import db
from modelark import fetch, fill, plan

# A fake active plan (#33) so fill.execute resolves without a plans table in the mock harness.
_FAKE_PLAN = {"plan_id": "test", "provisioning": "uncompressed", "drives": ["drive-00", "drive-01"]}


# ---- the write-probe --------------------------------------------------------------------------

def test_writable_probe(tmp_path):
    ctx = fetch.RunCtx(con=None)
    with mock.patch.object(fill.register, "archive_path", side_effect=lambda con, l: tmp_path):
        assert fill._writable(ctx, "drive-00") is True                 # real writable dir
        assert not (tmp_path / fill._PROBE_NAME).exists(), "probe file must be cleaned up"
    missing = tmp_path / "gone" / "deeper"                             # parent absent → write raises → False
    with mock.patch.object(fill.register, "archive_path", side_effect=lambda con, l: missing):
        assert fill._writable(ctx, "drive-00") is False
    with mock.patch.object(fill.register, "archive_path", side_effect=lambda con, l: None):
        assert fill._writable(ctx, "drive-00") is False               # unmounted → False


# ---- _await_drive: never accept a mounted-but-dead drive ------------------------------------

def test_await_drive_accepts_writable(tmp_path):
    ctx = fetch.RunCtx(con=None)
    with mock.patch.object(fill, "_mounted", side_effect=lambda c, l: (True, True)), \
         mock.patch.object(fill, "_writable", side_effect=lambda c, l: True):
        assert fill._await_drive(ctx, "drive-01", 0.01) is True


def test_await_drive_awaits_unwritable(tmp_path):
    stop = {"v": False}
    def writable(c, l):
        stop["v"] = True                                              # after the first probe, ask to stop so the wait ends
        return False
    ev = []
    ctx = fetch.RunCtx(con=None, should_stop=lambda: stop["v"], on_progress=ev.append)
    with mock.patch.object(fill, "_mounted", side_effect=lambda c, l: (True, True)), \
         mock.patch.object(fill, "_writable", side_effect=writable), \
         mock.patch.object(fill.time, "sleep", side_effect=lambda s: None):
        res = fill._await_drive(ctx, "drive-01", 0.01)
    assert res is False, "a mounted-but-unwritable drive must NOT be accepted"
    assert any(e.get("awaiting_drive") == "drive-01" for e in ev), "should prompt to re-seat, not silently skip"


# ---- execute() re-plan loop -----------------------------------------------------------------

def _harness(world, await_fn=None):
    """Fakes for plan_placements / placed_copies / fetch.run / run_replica driven by `world`
    ({placed: repo->copies, nc: repo->numcopies, drive: repo->copy#1 drive, bad: set-never-places})."""
    def fake_plan(con, scope=None, plan_id=None, provisioning=None):
        pool = [r for r, n in world["nc"].items() if world["placed"].get(r, 0) < n]
        prim = {"drive-00": [], "drive-01": []}
        for r in pool:
            if world["placed"].get(r, 0) == 0:                        # needs copy #1
                prim[world["drive"][r]].append({"repo": r, "size": 1})
        c2 = [{"repo": r, "size": 1} for r in pool
              if world["nc"][r] >= 2 and world["placed"].get(r, 0) >= 1]
        return {"primary": {"assign": prim, "unplaceable": []},
                "replica": {"assign": {"drive-04": c2}, "unplaceable": [], "source": "drive-00"}}

    calls = []
    def fake_run(drive_label=None, repos=None, max_24h_gb=0, ctx=None, fits=None):
        calls.append((drive_label, list(repos)))
        for r in repos:
            if r not in world.get("bad", set()):
                world["placed"][r] = 1                                # copy #1 down

    def fake_replica(assign, source, ctx=None):
        for items in assign.values():
            for i in items:
                world["placed"][i["repo"]] = 2                        # copy #2 down

    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE models(repo_id TEXT, numcopies INT)")
    con.execute("CREATE TABLE selection(repo_id TEXT, finalized_at TEXT)")
    for r, n in world["nc"].items():
        con.execute("INSERT INTO models VALUES(?,?)", (r, n))
        con.execute("INSERT INTO selection VALUES(?, '2026-01-01')", (r,))

    patches = [
        mock.patch.object(fill.plan, "active", side_effect=lambda con: dict(_FAKE_PLAN)),
        mock.patch.object(fill.librarian, "plan_placements", side_effect=fake_plan),
        mock.patch.object(fill.librarian, "placed_copies", side_effect=lambda con: dict(world["placed"])),
        mock.patch.object(fill.librarian, "raw_sizes",   # giants-first order (default 1 byte → no giants)
                          side_effect=lambda con, repos: {r: world.get("raw", {}).get(r, 1) for r in repos}),
        mock.patch.object(fill, "_fits", side_effect=lambda ctx, pid, prov, rid, label: True),  # capacity tested separately
        mock.patch.object(fill.fetch, "run", side_effect=fake_run),
        mock.patch.object(fill.fetch, "run_replica", side_effect=fake_replica),
        mock.patch.object(fill.fetch, "_bytes_last_24h", side_effect=lambda con: 0),
        mock.patch.object(fill, "_await_drive", side_effect=(await_fn or (lambda ctx, l, p: True))),
    ]
    return con, calls, patches


def test_replan_loop_fills_priority_then_advances(tmp_path):
    # a,c → copy#1 on drive-00 (c also needs copy#2); b → copy#1 on drive-01.
    world = {"placed": {}, "nc": {"a": 1, "b": 1, "c": 2},
             "drive": {"a": "drive-00", "b": "drive-00", "c": "drive-00"}, "bad": set()}
    world["drive"]["b"] = "drive-01"
    con, calls, patches = _harness(world)
    for p in patches:
        p.start()
    try:
        res = fill.execute(fetch.RunCtx(con=con), guided=True, max_24h_gb=0)
    finally:
        for p in patches:
            p.stop()
    assert res["ok"], res
    assert world["placed"] == {"a": 1, "b": 1, "c": 2}, world["placed"]       # all copies down (c got #2)
    assert calls[0][0] == "drive-00", f"NAS/priority drive first, got {calls}"  # priority order
    assert any(c[0] == "drive-01" for c in calls), "advanced to drive-01 after drive-00"


def test_giants_first_order(tmp_path):
    # operator's rule: GIANTS (>250 GB raw) first, then MUST-HAVES, then the rest — regardless of which
    # drive each lands on. giant→drive-01, must+rest→drive-00; fetch order must be giant, must, rest.
    world = {"placed": {}, "nc": {"giant": 1, "must": 2, "rest": 1},
             "drive": {"giant": "drive-01", "must": "drive-00", "rest": "drive-00"},
             "raw": {"giant": 300 * 10**9, "must": 10 * 10**9, "rest": 5 * 10**9}, "bad": set()}
    con, calls, patches = _harness(world)
    for p in patches:
        p.start()
    try:
        res = fill.execute(fetch.RunCtx(con=con), guided=True, max_24h_gb=0)
    finally:
        for p in patches:
            p.stop()
    assert res["ok"], res
    copy1_order = [r for _, repos in calls for r in repos]         # flatten the per-drive batches, in order
    assert copy1_order[:3] == ["giant", "must", "rest"], copy1_order


def test_replan_loop_blocks_a_bad_repo_and_terminates(tmp_path):
    # 'b' never places — the loop must block it (not spin) and still finish ('a' placed).
    world = {"placed": {}, "nc": {"a": 1, "b": 1},
             "drive": {"a": "drive-00", "b": "drive-00"}, "bad": {"b"}}
    con, calls, patches = _harness(world)
    for p in patches:
        p.start()
    try:
        res = fill.execute(fetch.RunCtx(con=con), guided=True, max_24h_gb=0)
    finally:
        for p in patches:
            p.stop()
    assert res["ok"], res                                             # terminated (no must-haves → GATE-C ok)
    assert world["placed"].get("a") == 1 and world["placed"].get("b", 0) == 0
    b_fetches = sum(1 for d, repos in calls if "b" in repos)
    assert b_fetches <= fill._MAX_REPO_ATTEMPTS + 1, f"bad repo retried unbounded: {b_fetches}"


def test_dead_drive_parks_not_blocks(tmp_path):
    # DEF-024: a drive whose await can't be satisfied must PARK the loop (await/re-seat), never get
    # its (or other drives') repos blocked and advance to replica/GATE-C with copy#1 undone (INC-009).
    world = {"placed": {}, "nc": {"a": 1, "b": 1},
             "drive": {"a": "drive-00", "b": "drive-01"}, "bad": set()}
    await_calls = []
    def dead_00(ctx, label, poll):
        await_calls.append(label)
        return label != "drive-00"                                    # drive-00 never becomes ready → stopped
    con, calls, patches = _harness(world, await_fn=dead_00)
    for p in patches:
        p.start()
    try:
        res = fill.execute(fetch.RunCtx(con=con), guided=True, max_24h_gb=0)
    finally:
        for p in patches:
            p.stop()
    assert res["stopped"] is True and not res["ok"], res              # parked on the dead drive, not 'done'
    assert world["placed"] == {}, "parked — must not churn/fetch anything"
    assert calls == [], "no fetch on a drive that never became writable"
    assert await_calls and await_calls[-1] == "drive-00", await_calls  # it awaited the dead drive, didn't skip it


def test_worker_terminal_labels(tmp_path):
    # a resumable cap/throttle must read 'paused', GATE-B 'blocked', a clean finish 'done' — never
    # mislabel a throttle as "fill complete" (the bug the 24h cap surfaced).
    import time as _t
    from modelark.web import fill_worker
    for outcome, expect in [({"status": "paused", "message": "24h cap"}, "paused"),
                            ({"status": "blocked", "message": "add a drive"}, "blocked"),
                            (None, "done")]:
        w = fill_worker.FillWorker()
        w.start(lambda ss, em, o=outcome: o)
        for _ in range(100):
            if not w.running():
                break
            _t.sleep(0.02)
        assert w.status()["status"] == expect, (outcome, w.status())


def test_dest_writable(tmp_path):
    from modelark import fetch
    assert fetch._dest_writable(tmp_path) is True
    assert not (tmp_path / ".modelark-write-probe").exists(), "probe must be cleaned up"
    assert fetch._dest_writable(tmp_path / "missing" / "deeper") is False


def test_fetch_run_bails_on_dead_drive_midbatch(tmp_path):
    # a drive that dies mid-batch must bail after the FIRST failure (re-plan loop re-awaits it),
    # not churn one error per repo through the whole assignment.
    from modelark import fetch
    events, calls = [], {"n": 0}
    def boom(ctx, rid, dest, label, annex, cfg):
        calls["n"] += 1
        raise RuntimeError("write failed: I/O error")
    ctx = fetch.RunCtx(con=object(), on_progress=events.append)
    with mock.patch.object(fetch.register, "archive_path", side_effect=lambda con, l: tmp_path), \
         mock.patch.object(fetch, "_is_annex", side_effect=lambda d: False), \
         mock.patch.object(fetch, "fetch_model", side_effect=boom), \
         mock.patch.object(fetch, "_dest_writable", side_effect=lambda d: False), \
         mock.patch.object(fetch, "_bytes_last_24h", side_effect=lambda c: 0), \
         mock.patch.object(fetch, "_event", side_effect=lambda *a, **k: None), \
         mock.patch.object(fetch.wishlist, "compression",
                           side_effect=lambda: {"threads": 1, "stream_compress": True, "max_compress_ram_gb": 4.0}):
        fetch.run(drive_label="drive-01", repos=["a", "b", "c"], max_24h_gb=0, ctx=ctx)
    assert calls["n"] == 1, f"dead drive must bail after the 1st failure, not churn all 3 (got {calls['n']})"
    assert any(e.get("awaiting_drive") == "drive-01" for e in events), "should emit awaiting on dead-drive bail"


# ---- #37 per-model capacity failsafe (real catalog + real librarian) ------------------------

def _mem():
    con = sqlite3.connect(":memory:", isolation_level=None)   # autocommit, matching db.connect()
    for stmt in db._statements(db.SCHEMA_PATH.read_text()):
        con.execute(stmt)
    return con


def _capacity_run(provisioning, free_bytes, a_stored):
    """Real catalog + real librarian.drives/est/_fits: one small plan drive, two 100-byte models. The
    plan fits BOTH by estimate up front, but model-a's ACTUAL stored (a_stored) exceeds its estimate,
    so after a lands the drive can't hold b → a re-plan finds b unplaceable AFTER progress →
    plan-capacity-stop (resumable), NOT GATE-B blocked (which is only for an up-front-infeasible plan)."""
    con = _mem()
    con.execute("INSERT INTO drives(drive_label,capacity_bytes,free_bytes,role,raid_backed) "
                "VALUES('drive-00',?,?,'primary',0)", [free_bytes, free_bytes])
    for r in ("a", "b"):
        con.execute("INSERT INTO models(repo_id,numcopies) VALUES(?,1)", [r])
        con.execute("INSERT INTO files(repo_id,rfilename,size_bytes,format,quant) "
                    "VALUES(?, 'model.safetensors', 100, 'safetensors', 'bf16')", [r])
        con.execute("INSERT INTO selection(repo_id,finalized_at) VALUES(?, '2026-01-01')", [r])
    plan.bootstrap(con)                                   # plan `ark` owns drive-00, active
    plan.set_provisioning(con, "ark", provisioning)

    def fake_run(drive_label=None, repos=None, max_24h_gb=0, ctx=None, fits=None):
        for r in repos:
            if fits is not None and not fits(r):          # the #37 hook: drive full → break the batch
                break
            stored = a_stored if r == "a" else 100
            con.execute("INSERT INTO archived(repo_id,rfilename,drive_label,orig_bytes,stored_bytes,compressed) "
                        "VALUES(?, 'model.safetensors', 'drive-00', 100, ?, 0)", [r, stored])

    with mock.patch.object(fill.fetch, "run", side_effect=fake_run), \
         mock.patch.object(fill, "_await_drive", side_effect=lambda ctx, l, p: True):
        return fill.execute(fetch.RunCtx(con=con), guided=True, max_24h_gb=0)


def test_plan_capacity_stop_uncompressed(tmp_path):
    # est == raw (100). Both fit at free=220 (rem≈209 ≥ 200); a inflates to 150 → after a, rem≈59 < 100.
    res = _capacity_run("uncompressed", free_bytes=220, a_stored=150)
    assert res["state"] == "plan-capacity-stop", res
    assert res["ok"] is False and res["stopped"] is False


def test_plan_capacity_stop_compressed(tmp_path):
    # est(a)=int(100*0.67*1.08)=72. Both fit at free=180 (rem≈171 ≥ 144); a raw-fallbacks to 100 (> its
    # 72 est) → after a, rem≈71 < 72 → b unplaceable AFTER progress → plan-capacity-stop.
    res = _capacity_run("compressed", free_bytes=180, a_stored=100)
    assert res["state"] == "plan-capacity-stop", res


# ---- DEF-022 fail-soft replica + GATE-C softening -------------------------------------------

def test_run_replica_defers_on_offline_source(tmp_path):
    # A dead/read-only source → deferred + awaiting-drive, NO per-repo copy churn (INC-009).
    events = []
    ctx = fetch.RunCtx(con=object(), on_progress=events.append)
    with mock.patch.object(fetch.register, "archive_path",
                           side_effect=lambda con, l: (tmp_path if l == "drive-04" else tmp_path / "gone")), \
         mock.patch.object(fetch, "_dest_writable", side_effect=lambda p: "gone" not in str(p)):
        res = fetch.run_replica({"drive-04": [{"repo": "m1"}]}, "drive-00", ctx=ctx)
    assert res["deferred"] and res["source_offline"], res
    assert res["copied_targets"] == []
    assert any(e.get("awaiting_drive") == "drive-00" for e in events), "should prompt to re-seat the source"


def test_gatec_pauses_on_deferred_copy2(tmp_path):
    # DEF-022: copy#1 of the must-have is safe but copy#2 is deferred (offline replica source) → GATE-C
    # returns PAUSED (resumable), never the red 'error' INC-009 hit.
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE models(repo_id TEXT, numcopies INT)")
    con.execute("CREATE TABLE selection(repo_id TEXT, finalized_at TEXT)")
    con.execute("INSERT INTO models VALUES('m',2)")
    con.execute("INSERT INTO selection VALUES('m','2026-01-01')")
    placed = {"m": 1}                                          # copy#1 down, copy#2 not

    def fake_plan(con, scope=None, plan_id=None, provisioning=None):
        return {"primary": {"assign": {"drive-00": []}, "unplaceable": []},
                "replica": {"assign": {"drive-04": [{"repo": "m", "size": 1}]},
                            "unplaceable": [], "source": "drive-00"}}

    def deferring_replica(assign, source, ctx=None):
        return {"deferred": True, "source_offline": True, "deferred_targets": ["drive-04"], "copied_targets": []}

    patches = [
        mock.patch.object(fill.plan, "active", side_effect=lambda con: dict(_FAKE_PLAN)),
        mock.patch.object(fill.librarian, "plan_placements", side_effect=fake_plan),
        mock.patch.object(fill.librarian, "placed_copies", side_effect=lambda con: dict(placed)),
        mock.patch.object(fill, "_fits", side_effect=lambda *a: True),
        mock.patch.object(fill.fetch, "run", side_effect=lambda **k: None),
        mock.patch.object(fill.fetch, "run_replica", side_effect=deferring_replica),
        mock.patch.object(fill, "_await_drive", side_effect=lambda ctx, l, p: True),
    ]
    for p in patches:
        p.start()
    try:
        res = fill.execute(fetch.RunCtx(con=con), guided=True, max_24h_gb=0)
    finally:
        for p in patches:
            p.stop()
    assert res["state"] == "paused", res                       # NOT 'error'
    assert res["ok"] is False and res["stopped"] is False


def test_sweep_incomplete(tmp_path):
    # INC-010: after a store, orphaned .incomplete leftovers are reclaimed; a fresh (active) .incomplete
    # + non-.incomplete files are kept (age guard). Missing cache dir → 0, no error.
    import os
    import time as _t
    dl = tmp_path / ".cache" / "huggingface" / "download"
    dl.mkdir(parents=True)
    orphan = dl / "old.incomplete"
    orphan.write_bytes(b"x" * 1000)
    os.utime(orphan, (_t.time() - 120, _t.time() - 120))          # 2 min idle → orphan, swept
    fresh = dl / "new.incomplete"
    fresh.write_bytes(b"y" * 500)                                 # just written → kept (age guard)
    other = dl / "keep.bin"
    other.write_bytes(b"z" * 200)                                 # not .incomplete → kept
    freed = fetch._sweep_incomplete(tmp_path)
    assert freed == 1000, freed
    assert not orphan.exists() and fresh.exists() and other.exists()
    assert fetch._sweep_incomplete(tmp_path / "nope") == 0        # no cache dir → 0, no crash


if __name__ == "__main__":
    import tempfile
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(Path(tempfile.mkdtemp()))
            print(f"ok  {name}")
    print("all passed")
