"""First-class Plan entity (#33, DEF-016) — a fill campaign's identity + its capacity model.

A Plan = {identity, the global catalog selection it fills, a FIXED set of registered drives
(`plan_drives`), its annex, a provisioning mode}. It fuels the level-1 capacity failsafe by exposing
three numbers computed LIVE against the catalog every call (never stored snapshots):

  • uncompressed = Σ raw (full-precision) footprint of the finalized selection, COPY-AWARE (× numcopies).
                   The BOUNDARY CURRENCY (DEF-016): what decides "are we out". Never a bet.
  • compressed   = what's actually archived so far + an observed-ratio estimate for the copies still to
                   write, copy-aware. uncompressed − compressed = the compression dividend, in drive terms.
  • capacity     = Σ (drive capacity − headroom) over `plan_drives`. The only capacity that exists.

Provisioning (DEF-016): 'uncompressed' (default) over-provisions against the raw footprint — the fill
finishes early with drives to spare and never runs out; 'compressed' bets on ZipNN and the per-model
failsafe (#37, fill.execute) carries the risk. BOTH numbers are always reported (the two bars, #36) so
an unexpected inflation (orphans / a bug making actual > expected) is visible in either mode.

`selection` + `archived` stay GLOBAL for the single plan `ark`; a future plan_id column on them is the
multi-plan future (a DEF). Exactly one plan is is_active (the backend/portal's current context); the
#35 UI gate additionally forces an explicit operator pick per session.

Layering: this imports librarian (headroom / observed-ratio / placed-copies — one-directional, librarian
never imports plan) and register (annex root). registration's reverse call into add_drive (#34) is a
LAZY import inside register_drive, so there is no module-level cycle.
"""
from __future__ import annotations

from modelark.core import db
from modelark import archive_manifest, librarian, register, wishlist

DEFAULT_PLAN = "ark"
_FIELDS = ["plan_id", "name", "annex_root", "provisioning", "status", "is_active", "created_at", "notes"]

# DEC-029: treat a RAID-backed LUN's usable capacity conservatively — reserve at least this fraction
# even where the size-tranched headroom (librarian) would reserve less. The LUN is ALSO provisioned at
# ~85% of its Synology volume (the ops half of DEC-029, done at LUN-creation time); this is the belt to
# that suspenders, so the capacity model never reports a near-full LUN as roomy (INC-009, twice-bitten).
_RAID_MIN_HEADROOM_FRAC = 0.03


# ---- CRUD -------------------------------------------------------------------

def create(con, plan_id, name=None, annex_root=None, provisioning="uncompressed", notes=None) -> dict:
    if provisioning not in ("uncompressed", "compressed"):
        raise ValueError(f"provisioning must be 'uncompressed' or 'compressed', got {provisioning!r}")
    db.upsert(con, "plans", {
        "plan_id": plan_id, "name": name or plan_id,
        "annex_root": annex_root or str(register.library_root()),
        "provisioning": provisioning, "status": "active", "notes": notes,
    }, pk=["plan_id"])
    return get(con, plan_id)


def get(con, plan_id) -> dict | None:
    row = con.execute(
        f"SELECT {', '.join(_FIELDS)} FROM plans WHERE plan_id=?", [plan_id]).fetchone()
    if not row:
        return None
    d = dict(zip(_FIELDS, row))
    d["is_active"] = bool(d["is_active"])
    d["drives"] = plan_drive_labels(con, plan_id)
    return d


def list_plans(con) -> list[dict]:
    return [get(con, r[0]) for r in
            con.execute("SELECT plan_id FROM plans ORDER BY created_at, plan_id").fetchall()]


def active(con) -> dict | None:
    row = con.execute("SELECT plan_id FROM plans WHERE is_active LIMIT 1").fetchone()
    return get(con, row[0]) if row else None


def set_active(con, plan_id) -> dict:
    if get(con, plan_id) is None:
        raise ValueError(f"no such plan: {plan_id}")
    con.execute("BEGIN")                                     # atomic flip — a crash between the two UPDATEs
    try:                                                     # must never leave EVERY plan is_active=false
        con.execute("UPDATE plans SET is_active=false")
        con.execute("UPDATE plans SET is_active=true WHERE plan_id=?", [plan_id])
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return get(con, plan_id)


def set_provisioning(con, plan_id, mode) -> dict:
    """Switch a plan between 'uncompressed' (over-provision, never runs out — default) and 'compressed'
    (bet on ZipNN; the per-model failsafe carries the risk). Both totals are always reported regardless."""
    if mode not in ("uncompressed", "compressed"):
        raise ValueError(f"provisioning must be 'uncompressed' or 'compressed', got {mode!r}")
    if get(con, plan_id) is None:
        raise ValueError(f"no such plan: {plan_id}")
    con.execute("UPDATE plans SET provisioning=? WHERE plan_id=?", [mode, plan_id])
    return get(con, plan_id)


def add_drive(con, plan_id, drive_label) -> None:
    """Idempotent — a re-registered drive (stable drive-NN key, DEC-018) re-adds to the same row."""
    db.upsert(con, "plan_drives", {"plan_id": plan_id, "drive_label": drive_label},
              pk=["plan_id", "drive_label"])


def remove_drive(con, plan_id, drive_label) -> None:
    con.execute("DELETE FROM plan_drives WHERE plan_id=? AND drive_label=?", [plan_id, drive_label])


def plan_drive_labels(con, plan_id) -> list[str]:
    return [r[0] for r in con.execute(
        "SELECT drive_label FROM plan_drives WHERE plan_id=? ORDER BY drive_label", [plan_id]).fetchall()]


def bootstrap(con, plan_id=DEFAULT_PLAN) -> dict:
    """Idempotent: ensure plan `ark` exists, owns every currently-registered drive, and is active if no
    plan is. Safe to call on every startup — creates nothing that exists, removes nothing. Called from
    the portal serve() startup, the CLI `plan`/`library plan` commands, and registration (#34)."""
    if get(con, plan_id) is None:
        create(con, plan_id, name="Ark")
    for row in con.execute("SELECT drive_label FROM drives").fetchall():
        add_drive(con, plan_id, row[0])
    if active(con) is None:
        set_active(con, plan_id)
    return get(con, plan_id)


# ---- capacity + the three live numbers --------------------------------------

def _headroom(cap: int, raid_backed: bool) -> int:
    """Reserved headroom for a drive of `cap` bytes — the librarian's size-tranched reserve, with a
    DEC-029 conservative floor for a RAID-backed LUN so a big LUN still keeps real breathing room."""
    h = librarian.headroom_bytes(cap)
    if raid_backed:
        h = max(h, int(cap * _RAID_MIN_HEADROOM_FRAC))
    return h


def capacity(con, plan_id) -> int:
    """Σ (capacity − headroom) over the plan's drives — the nominal fleet size the plan can fill. Uses
    the registration capacity snapshot (stable), NOT live free (that is the per-model failsafe's job)."""
    labels = plan_drive_labels(con, plan_id)
    if not labels:
        return 0
    ph = ",".join(["?"] * len(labels))
    total = 0
    for _, cap, raid in con.execute(
            f"SELECT drive_label, coalesce(capacity_bytes, free_bytes, 0), coalesce(raid_backed, false) "
            f"FROM drives WHERE drive_label IN ({ph})", labels).fetchall():
        total += max(0, cap - _headroom(cap, bool(raid)))
    return total


def _footprint_by_repo(con, repo_ids: list[str]) -> dict[str, tuple[int, int, int]]:
    """Per repo: (raw, compressible, noncompressible) from the canonical bulk manifest.

    Ineligible repositories remain absent, preserving the catalog-gate behavior while the
    shadow reconciler reports their typed manifest-policy diagnostics.
    """
    policy = archive_manifest.ArchivePolicy(allow_pickle=not wishlist.exclude_pickle_only())
    batch = archive_manifest.inspect_manifests_for_repos(con, repo_ids, policy)
    out = {}
    for repo_id, manifest in batch.manifests.items():
        compressible = sum(item.size_bytes for item in manifest if item.storage_action == "compress")
        noncompressible = sum(item.size_bytes for item in manifest if item.storage_action == "raw")
        out[repo_id] = (compressible + noncompressible, compressible, noncompressible)
    return out


# Graduated catalog gate (#38): tiers on the COMPRESSED footprint vs capacity (compressed is what
# actually lands; if even it exceeds capacity the set definitely won't fit → prevent).
_GATE_SOFT = 0.70          # below this × capacity → just show the bars
_GATE_WARN = 0.90          # [soft, warn) → soft warn; [warn, 1.0) → warn harder; ≥ 1.0 → prevent


def _footprint(con, plan_id, repo_ids) -> tuple[int, int, int]:
    """Copy-aware (uncompressed, compressed, n_must) footprint over `repo_ids`. uncompressed = Σ raw ×
    numcopies (the boundary currency); compressed = bytes actually archived so far + an observed-ratio
    estimate for the copies still to write. Over-counts one in-flight partial copy at most (transient)."""
    numcopies = dict(con.execute("SELECT repo_id, coalesce(numcopies,1) FROM models").fetchall())
    placed = librarian.placed_copies(con)
    ratio = librarian.plan_float_ratio(con)                 # observed float ratio, floored at 0.67
    margin = librarian._SIZE_MARGIN                         # single source of truth for the est margin
    fp = _footprint_by_repo(con, repo_ids)
    uncompressed = est_remaining = n_must = 0
    for repo in repo_ids:
        nc = numcopies.get(repo, 1)
        n_must += nc >= 2
        raw, comp, noncomp = fp.get(repo, (0, 0, 0))
        uncompressed += raw * nc
        est_per_copy = int((comp * ratio + noncomp) * margin)
        est_remaining += est_per_copy * max(0, nc - placed.get(repo, 0))
    # actual archived bytes MUST be scoped to `repo_ids` — an archived row for a repo no longer in the
    # selection (e.g. after re-curating the cart) would otherwise inflate the compressed footprint.
    if repo_ids:
        ph = ",".join(["?"] * len(repo_ids))
        actual_stored = con.execute(
            f"SELECT coalesce(sum(stored_bytes), 0) FROM archived WHERE repo_id IN ({ph})",
            repo_ids).fetchone()[0]
    else:
        actual_stored = 0
    return uncompressed, actual_stored + est_remaining, int(n_must)


def gate_tier(compressed: int, capacity: int) -> str:
    """#38 gate tier from the compressed footprint vs capacity: ok | soft | warn | prevent."""
    if not capacity:
        return "ok"
    if compressed >= capacity:
        return "prevent"
    r = compressed / capacity
    return "warn" if r >= _GATE_WARN else ("soft" if r >= _GATE_SOFT else "ok")


def _numbers(con, plan_id, repo_ids, count_key) -> dict:
    unc, comp, n_must = _footprint(con, plan_id, repo_ids)
    cap = capacity(con, plan_id)
    p = get(con, plan_id)
    return {
        "plan_id": plan_id,
        "provisioning": p["provisioning"] if p else "uncompressed",
        "uncompressed": unc, "compressed": comp, "capacity": cap,
        count_key: len(repo_ids), "n_must": n_must,
        "n_drives": len(plan_drive_labels(con, plan_id)),
        "uncompressed_pct": round(unc / cap, 4) if cap else 0.0,
        "compressed_pct": round(comp / cap, 4) if cap else 0.0,
        "over_uncompressed": bool(cap and unc > cap),
        "over_compressed": bool(cap and comp > cap),
        "tier": gate_tier(comp, cap),
    }


def totals(con, plan_id) -> dict:
    """The three LIVE numbers over the FINALIZED selection — the fill's actual footprint (#36 bars, #37
    failsafe). Copy-aware: a must-have (numcopies≥2) counts every physical copy against capacity."""
    sel = [r[0] for r in con.execute(
        "SELECT repo_id FROM selection WHERE finalized_at IS NOT NULL").fetchall()]
    return _numbers(con, plan_id, sel, "n_selection")


def cart_totals(con, plan_id) -> dict:
    """The live numbers over the WHOLE cart (every selection row, finalized or not) — powers the
    graduated catalog gate (#38) so the operator sees the footprint climb + a prevent tier while
    BUILDING the set, before Finish commits it."""
    cart = [r[0] for r in con.execute("SELECT repo_id FROM selection").fetchall()]
    return _numbers(con, plan_id, cart, "n_cart")
