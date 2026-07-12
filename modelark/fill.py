"""The guided fill orchestrator (task #22) — ONE backend for both surfaces.

`library plan --apply` (CLI) and the portal's background worker both call `execute()`, so both
run the identical DEC-019 gates and DEC-020 tiering; they differ only in the injected `RunCtx`
(see fetch.RunCtx) and one flag:

  • CLI       — own connection, nullcontext lock, prints narration, never stops, `guided=False`:
                GATE-A refuses up front if any target is unmounted (don't spend days on the bulk
                only to fail the replica step).
  • worker    — portal's shared connection + `data._lock` (brief per-file writes), progress
                callback, cooperative stop, `guided=True`: AWAITS each drive as it's reached
                (prompts the operator to insert it), so the small replica drive can be hot-swapped
                in after the bulk has landed.

RE-PLANNING (targeted #37 / DEC-025): the PRIMARY tier re-plans from LIVE reality before each
drive-batch, instead of marching a single plan computed once at start. That reclaims a drive's
estimate-vs-actual slack — the fill keeps filling the NAS instead of advancing off it half-empty —
and fills drives in live-priority order. A WRITE-PROBE guards each drive: mounted != healthy (a USB
drop leaves a mounted-but-EIO device), so a faulty drive is AWAITED, never silently skipped.

`execute()` RETURNS a result dict and never raises `SystemExit` (which would escape the worker's
`except Exception` and wedge the thread) — the CLI turns `ok=False` into exit 1, the worker into
an 'error'/'blocked'/'stopped' status.
"""
from __future__ import annotations

import time
from pathlib import Path

from modelark import fetch, librarian, plan, register

_PROBE_NAME = ".modelark-write-probe"
_MAX_REPO_ATTEMPTS = 2          # skip a repo that fails to place this many passes, so one bad repo can't wedge the loop
_GIANT_BYTES = 250 * 1_000_000_000   # >250 GB (raw download) → fetched UP FRONT (operator's giants-first rule)


def _tier_targets(plan: dict) -> tuple[list, list]:
    prim = [(label, items) for label, items in plan["primary"]["assign"].items() if items]
    repl = [(label, items) for label, items in plan["replica"]["assign"].items() if items]
    return prim, repl


def _mounted(ctx, label: str) -> tuple[bool, bool]:
    """(registered_with_fs_uuid, currently_mounted) for `label`, read under the lock."""
    with ctx.lock:
        uuid = (ctx.con.execute("SELECT fs_uuid FROM drives WHERE drive_label=?", [label]).fetchone()
                or [None])[0]
        mounted = uuid is not None and register.archive_path(ctx.con, label) is not None
    return uuid is not None, mounted


def _writable(ctx, label: str) -> bool:
    """Probe that the drive's archive path is actually WRITABLE — a mounted drive can still be dead
    (a USB enclosure drop leaves a mounted device that EIOs on every access; `limit=0`). Write, read
    back, and delete a tiny hidden file; any OSError → not writable. This is what turns 'mounted but
    faulty → silently skip its whole assignment' into 'await + prompt' (the INC that drive-01 hit)."""
    with ctx.lock:
        path = register.archive_path(ctx.con, label)
    if path is None:
        return False
    probe = Path(path) / _PROBE_NAME
    try:
        probe.write_bytes(b"modelark")
        ok = probe.read_bytes() == b"modelark"
        probe.unlink()
        return ok
    except OSError:
        try:
            probe.unlink()
        except OSError:
            pass
        return False


def _await_drive(ctx, label: str, poll_secs: float) -> bool:
    """Guided worker: block until `label` is a live, WRITABLE mount (operator inserts / re-seats it),
    emitting an awaiting prompt; return False if a stop is requested first. A label with no fs_uuid (a
    special remote / never block-registered) isn't awaitable — return True and let fetch handle the
    miss. A mounted-but-unwritable drive is awaited too (never silently skipped)."""
    if ctx.should_stop():           # already stopping (e.g. Stop hit mid-fetch on the PREVIOUS drive) →
        return False                # don't flash a spurious "insert <next drive>" prompt on the way out
    registered, mounted = _mounted(ctx, label)
    if not registered:
        return True                 # special remote / unregistered — let fetch handle the miss
    if mounted and _writable(ctx, label):
        return True
    reason = "insert it" if not mounted else "mounted but not writable (I/O error) — re-seat it"
    ctx.on_progress({"phase": "awaiting-drive", "awaiting_drive": label,
                     "say": f"⏳ drive {label}: {reason} — the fill continues once it's writable."})
    while not ctx.should_stop():
        time.sleep(poll_secs)
        _, mounted = _mounted(ctx, label)
        if mounted and _writable(ctx, label):
            ctx.on_progress({"phase": "running", "awaiting_drive": None,
                             "say": f"✅ {label} writable — continuing."})
            return True
    return False


def _replan(ctx, plan_id: str, provisioning: str, repo_scope: list[str] | None) -> dict:
    """Recompute the placement from LIVE reality (live disk free + observed ratio, DEC-025), scoped to
    the Plan's drive set (#33) + provisioning currency (DEF-016), under the lock. Called before each
    primary drive-batch so freed slack is reclaimed and a filled drive surfaces as unplaceable."""
    with ctx.lock:
        return librarian.plan_placements(ctx.con, repo_scope, plan_id, provisioning)


def _fits(ctx, plan_id: str, provisioning: str, repo_id: str, label: str) -> bool:
    """#37 per-model boundary check: does `repo_id` still fit drive `label`'s LIVE remaining (live free
    − headroom) in the plan's provisioning currency? Uses the SAME `drives()` remaining + est size the
    librarian packs with, so the fill's per-model check and the re-plan can never disagree — a
    'break the batch' only ever happens because a PRIOR model in the same batch consumed the slack."""
    with ctx.lock:
        d = next((x for x in librarian.drives(ctx.con, plan_id) if x["label"] == label), None)
        if d is None:
            return False
        size = librarian.est_stored_bytes(ctx.con, repo_id, provisioning=provisioning)
    return d["remaining"] >= size


def _primary_order(ctx, plan: dict, blocked: set) -> list[tuple[str, str]]:
    """GLOBAL copy#1 fetch priority (operator's rule): GIANTS (>250 GB raw download) first, then
    MUST-HAVES, then the rest — largest-first within each tier. Returns a flat [(drive_label, repo)].
    The fill DRAINS one drive at a time from this order (the hot-swap workflow — the operator can't keep
    every USB drive mounted at once), so this ordering picks BOTH the drive asked for first (the one
    holding the top-priority model — the giant-heaviest) AND the within-drive order (giants first). Each
    repo lands on the drive the librarian assigned it. Excludes copy#1-done + `blocked` repos.
    De-risking rationale: a 400 GB model that fails at 90% late in the run wastes the most — get it early."""
    with ctx.lock:
        placed = librarian.placed_copies(ctx.con)
    prim, _ = _tier_targets(plan)
    pending = [(label, i["repo"]) for label, items in prim for i in items
               if placed.get(i["repo"], 0) == 0 and i["repo"] not in blocked]
    if not pending:
        return []
    repos = [r for _, r in pending]
    with ctx.lock:
        raw = librarian.raw_sizes(ctx.con, repos)
        ph = ",".join(["?"] * len(repos))
        must = {r for (r,) in ctx.con.execute(
            f"SELECT repo_id FROM models WHERE coalesce(numcopies,1) >= 2 AND repo_id IN ({ph})", repos).fetchall()}

    def rank(item):
        repo = item[1]
        sz = raw.get(repo, 0)
        tier = 0 if sz > _GIANT_BYTES else (1 if repo in must else 2)   # giant → must-have → rest
        return (tier, -sz, repo)                                        # largest-first within a tier; repo = stable tie-break
    return sorted(pending, key=rank)


def execute(ctx, *, plan_id: str | None = None, max_24h_gb: float = 2000,
            repo_scope: list[str] | None = None, guided: bool = False, poll_secs: float = 3.0) -> dict:
    """Run the active Plan's finalized selection through both tiers behind the DEC-019 gates,
    RE-PLANNING the primary tier from live reality before each drive-batch and enforcing the #37
    per-model capacity failsafe. Emits progress via `ctx.on_progress`; returns {ok, state, ...} and
    never raises SystemExit (which would escape the worker's `except` and wedge the thread). `plan_id`
    selects the plan (default: the active one); its provisioning mode sets the packing currency;
    `repo_scope` (a `--repo` list) narrows planning + GATE-C to the requested repos."""
    con = ctx.con
    ctx.stats["t0"] = time.monotonic()
    ctx.stats.setdefault("by_drive", {})

    # Resolve the Plan (#33): its FIXED drive set + provisioning currency drive everything below.
    with ctx.lock:
        prow = (plan.get(con, plan_id) if plan_id else plan.active(con)) or plan.bootstrap(con)
    pid, provisioning = prow["plan_id"], prow["provisioning"]
    ctx.on_progress({"phase": "plan", "plan_id": pid, "provisioning": provisioning,
                     "say": f"plan '{pid}' · provisioning={provisioning} · {len(prow['drives'])} drive(s)"})

    # GATE-A (DEC-019, CLI only): every drive we FETCH onto must resolve to a live mount (a special
    # remote or unmounted clone silently no-ops in fetch.run). The guided worker awaits+probes each
    # drive when it reaches it (below), so it doesn't need the up-front check.
    if not guided:
        plan0 = _replan(ctx, pid, provisioning, repo_scope)
        prim0, repl0 = _tier_targets(plan0)
        targets = [label for label, _ in prim0] + [label for label, _ in repl0]
        unmounted = [label for label in dict.fromkeys(targets) if _mounted(ctx, label) == (True, False)]
        if unmounted:
            msg = f"fetch target(s) not mounted: {', '.join(unmounted)}. Mount them, then re-run. (No bytes fetched.)"
            ctx.on_progress({"phase": "blocked", "gate": "A", "say": "🔴 refusing --apply: " + msg})
            return {"ok": False, "stopped": False, "gate": "A", "state": "blocked",
                    "message": msg, "unmounted": unmounted}

    # PRIMARY tier (bulk + must-have copy #1), RE-PLANNED per drive-batch, with the #37 failsafe.
    ctx.on_progress({"phase": "primary", "say": "=== PRIMARY tier (bulk + must-have copy #1) ==="})
    blocked: set = set()
    attempts: dict = {}
    fetched_any = False
    while not ctx.should_stop():
        placement = _replan(ctx, pid, provisioning, repo_scope)
        # GATE-B / #37 boundary: a model that fits NO plan drive's live free. Up front (nothing fetched
        # this run) → the selection is too big for the fleet (or a single model exceeds every drive) →
        # GATE-B 'blocked'. After we've been filling → a drive ran out → 'plan-capacity-stop' (resumable:
        # add a drive to the plan, re-run). Both say "add capacity"; the state distinguishes the cause.
        un_p, un_r = placement["primary"]["unplaceable"], placement["replica"]["unplaceable"]
        if un_p or un_r:
            tb = sum(i["size"] for i in un_p + un_r) / 1e12
            if fetched_any:
                msg = (f"a drive is full — {len(un_p) + len(un_r)} model(s) / {tb:.2f} TB no longer fit "
                       f"plan '{pid}' live free. Add a drive to the plan, then re-run. (Nothing lost.)")
                ctx.on_progress({"phase": "plan-capacity-stop", "plan_id": pid, "say": "🟠 " + msg})
                return {"ok": False, "stopped": False, "state": "plan-capacity-stop",
                        "message": msg, "plan_id": pid}
            msg = (f"{len(un_p)} bulk model(s) unplaceable, {len(un_r)} must-have(s) short of their copies "
                   f"({tb:.2f} TB) — the selection exceeds plan '{pid}'. Add a drive or trim. (Nothing fetched.)")
            ctx.on_progress({"phase": "blocked", "gate": "B", "say": "🔴 " + msg})
            return {"ok": False, "stopped": False, "gate": "B", "state": "blocked", "message": msg}

        order = _primary_order(ctx, placement, blocked)     # global giants-first priority [(drive, repo)]
        if not order:
            break                                            # all first copies down → primary tier complete

        # DRAIN ONE DRIVE PER PASS (the hot-swap workflow: the operator can't keep every USB drive mounted
        # at once — DEC-023 / _await_drive prompts for each in turn). Pick the drive holding the single
        # highest-priority pending model, then fetch ALL of that drive's pending models — giants-first
        # WITHIN the drive — before asking for the next drive. So giants land early without swap-thrashing.
        label = order[0][0]
        batch = [repo for d, repo in order if d == label]
        # DEF-024: AWAIT + write-probe the target FIRST. A dead / absent / unwritable drive PARKS here
        # (insert/re-seat prompt) — never mistaken for "no progress", never gets a repo blocked.
        if guided and not _await_drive(ctx, label, poll_secs):
            return {"ok": False, "stopped": True, "state": "stopped", "message": "stopped by request"}
        if ctx.should_stop():
            return {"ok": False, "stopped": True, "state": "stopped", "message": "stopped by request"}
        ctx.on_progress({"phase": "primary", "drive": label, "n_repos": len(batch),
                         "say": f"== {label} ({len(batch)} model(s) need copy #1, giants first) =="})
        # #37: the per-model fits hook breaks the batch if THIS drive's live free runs out mid-batch, so
        # the next re-plan re-homes the overflow (or stops as plan-capacity-stop) — never an ENOSPC.
        fetch.run(drive_label=label, repos=batch, max_24h_gb=max_24h_gb, ctx=ctx,
                  fits=lambda rid, _l=label: _fits(ctx, pid, provisioning, rid, _l))

        # Progress judged on THIS batch (the drive was writable — await passed). Any copy#1 landed → next
        # pass. NOTHING landed: 24h cap (clean pause) · the fits hook broke on batch[0] because the drive
        # is full (let the re-plan re-home / stop — DON'T block) · else these repos fail → block ONLY them.
        with ctx.lock:
            placed_after = librarian.placed_copies(con)
        if any(placed_after.get(r, 0) >= 1 for r in batch):
            fetched_any = True
            continue
        if max_24h_gb:
            with ctx.lock:
                used = fetch._bytes_last_24h(con)
            if used >= max_24h_gb * 1e9:
                ctx.on_progress({"phase": "throttled",
                                 "say": "24h download cap reached — stopping (resumable, re-run to continue)."})
                return {"ok": False, "stopped": True, "state": "paused",
                        "message": "24h download cap reached (resumable)"}
        # `_fits` takes ctx.lock internally; the lock is NOT held here. If batch[0] still fits the drive,
        # nothing landing was a real fetch failure → block after N; else the drive filled → re-plan handles it.
        if _fits(ctx, pid, provisioning, batch[0], label):
            for r in batch:
                attempts[r] = attempts.get(r, 0) + 1
                if attempts[r] >= _MAX_REPO_ATTEMPTS:
                    blocked.add(r)

    if ctx.should_stop():
        return {"ok": False, "stopped": True, "state": "stopped", "message": "stopped by request"}

    # REPLICA tier (must-have copy #2 ← local copy from the RAID/primary home) — fresh plan.
    placement = _replan(ctx, pid, provisioning, repo_scope)
    _, repl = _tier_targets(placement)
    repl_result = {"deferred": False}
    if repl and not ctx.should_stop():
        ctx.on_progress({"phase": "replica",
                         "say": "=== REPLICA tier (must-have copy #2 ← local copy from the home) ==="})
        if guided:
            for label, _ in repl:
                if not _await_drive(ctx, label, poll_secs):
                    return {"ok": False, "stopped": True, "state": "stopped", "message": "stopped by request"}
        # DEF-022: run_replica probes the source (INC-009: the RAID can die) + targets and returns a
        # deferral report, so GATE-C can PAUSE on an offline copy#2 drive instead of churning + erroring.
        repl_result = fetch.run_replica(placement["replica"]["assign"],
                                        placement["replica"]["source"], ctx=ctx) or {"deferred": False}

    if ctx.should_stop():
        return {"ok": False, "stopped": True, "state": "stopped"}

    # GATE-C (DEC-019): post-condition — every finalized must-have now holds >= numcopies COMPLETE
    # copies. Silent under-replication becomes a loud failure (exit 1 on the CLI, error in the UI).
    ctx.on_progress({"phase": "gate-c", "say": "checking copy counts…"})
    with ctx.lock:
        placed = librarian.placed_copies(con)
        if repo_scope:
            musts = con.execute(
                "SELECT repo_id, coalesce(numcopies,1) FROM models WHERE coalesce(numcopies,1) >= 2 "
                f"AND repo_id IN ({','.join(['?']*len(repo_scope))})", repo_scope).fetchall()
        else:
            musts = con.execute(
                "SELECT repo_id, coalesce(numcopies,1) FROM models WHERE coalesce(numcopies,1) >= 2 "
                "AND repo_id IN (SELECT repo_id FROM selection WHERE finalized_at IS NOT NULL)").fetchall()
    failed = [{"repo": r, "need": nc, "have": placed.get(r, 0)} for r, nc in musts if placed.get(r, 0) < nc]
    if failed:
        # DEF-022: copy #1 of every must-have is safe (have >= 1) and the only shortfall is copy #2,
        # deferred because the replica source/target is offline → PAUSE (resumable), not a red error
        # (INC-009: a dead RAID source hard-errored a run whose copy #1 was all safe). A genuinely
        # missing copy #1 (have == 0) is real under-replication → error.
        c1_missing = [f for f in failed if f["have"] == 0]
        if not c1_missing and repl_result.get("deferred"):
            msg = (f"copy #1 of every must-have is safe; copy #2 of {len(failed)} is deferred — the replica "
                   f"source/target is offline. Re-seat it, then re-run to finish replication. (Nothing lost.)")
            ctx.on_progress({"phase": "awaiting-drive", "say": "⏸ " + msg})
            return {"ok": False, "stopped": False, "state": "paused", "failed": failed, "message": msg}
        lines = "; ".join(f"{f['repo']} {f['have']}/{f['need']}" for f in failed[:12])
        ctx.on_progress({"phase": "error", "gate": "C",
                         "say": f"🔴 POST-CHECK FAILED (DEC-019): {len(failed)} must-have(s) below copies: {lines}"})
        return {"ok": False, "stopped": False, "gate": "C", "state": "error", "failed": failed,
                "message": f"{len(failed)} must-have(s) below their copy count"}
    ctx.on_progress({"phase": "done",
                     "say": "✅ post-check: all finalized must-haves hold their required copies."})
    return {"ok": True, "stopped": False, "state": "done", "n_must": len(musts),
            "message": f"fill complete — all {len(musts)} finalized must-have(s) hold their copies"}
