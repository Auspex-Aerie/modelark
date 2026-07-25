"""PR-07 / #37 lifecycle × eligibility pure matrix, bootstrap, and compatibility (tests-first).

Gate 1 pins the accepted Gate-0 matrix BEFORE production:

  Lifecycle   Eligibility    New placement   Verified copy satisfies   Bootstrap adds missing
  active      enabled        yes             yes                       yes
  active      excluded       no              yes                       no
  lost        *              no              no                        no
  retired     *              no              no                        no

Presence (mounted/offline) must not rewrite this matrix. Retired row is the tombstone.

RED until DriveFact carries lifecycle/eligibility, pure candidates filter placement vs satisfaction
orthogonally, plan.bootstrap() only inserts missing active+enabled membership, and librarian
placed_copies / queue totals agree with canonical satisfaction.
"""
from __future__ import annotations

import dataclasses
import sqlite3

from modelark import archive_manifest, capacity, librarian, plan, reconcile, register
from modelark.core import db

try:
    import modelark.candidates as candidates
    _HAS = True
except ModuleNotFoundError:
    candidates = None
    _HAS = False

HW = "1" * 64
_CFG = (("max_compress_ram_gb", 64), ("stream_compress", True), ("threads", 4))
RATIO = capacity.DEFAULT_FLOAT_RATIO


def _require():
    if not _HAS:
        raise AssertionError("modelark.candidates required for lifecycle matrix contracts")
    fields = {f.name for f in dataclasses.fields(candidates.DriveFact)}
    if not {"lifecycle", "eligibility"} <= fields:
        raise AssertionError(
            "DriveFact must carry durable lifecycle and eligibility (PR-07 / #37); "
            f"got fields={sorted(fields)}")


def _mf(name, size, sha256, *, fmt="safetensors", quant="bf16"):
    action = "compress" if fmt == "safetensors" and quant in archive_manifest.FLOAT_QUANTS else "raw"
    return archive_manifest.ManifestFile(
        rfilename=name, size_bytes=size, sha256=sha256, format=fmt, quant=quant,
        storage_action=action)


def _drive(label, *, role="primary", raid=False, cap=10**12,
           lifecycle="active", eligibility="enabled",
           fs_uuid=None, annex_uuid=None, serial=None):
    _require()
    kwargs = dict(
        drive_label=label, role=role, raid_backed=raid, capacity_bytes=cap,
        filesystem_capacity_bytes=cap, identity_epoch=1,
        fs_uuid=fs_uuid, annex_uuid=annex_uuid, serial=serial,
    )
    # Pass lifecycle/eligibility only when the type accepts them (Gate-2 will).
    fields = {f.name for f in dataclasses.fields(candidates.DriveFact)}
    if "lifecycle" in fields:
        kwargs["lifecycle"] = lifecycle
    if "eligibility" in fields:
        kwargs["eligibility"] = eligibility
    return candidates.DriveFact(**kwargs)


def _arch(repo, drive, name, *, sha=None, obytes=None, sbytes=None, key=None):
    return candidates.ArchivedFileFact(
        repo_id=repo, drive_label=drive, rfilename=name,
        orig_sha256=sha, orig_bytes=obytes, stored_bytes=sbytes, annex_key=key)


def _input(*, selection, manifests, numcopies, drives, archived=(), cfg=_CFG, ratio=RATIO):
    return candidates.PlannerInput(
        plan_id="ark",
        selection=tuple(selection),
        manifests=tuple((repo, tuple(files)) for repo, files in manifests),
        numcopies=tuple(numcopies),
        drives=tuple(drives),
        archived=tuple(archived),
        compression_cfg=tuple(cfg),
        float_ratio=ratio,
    )


def _run(inp):
    graph = candidates.requirements(inp)
    return graph, candidates.candidates(inp, graph)


def _cands(cset, rid):
    for req_id, cs in cset.by_requirement:
        if req_id == rid:
            return cs
    return ()


def _targets(cs):
    return {c.target_drive for c in cs}


def _satisfied_drives(cset, rid):
    out = set()
    for sat in cset.satisfied:
        if sat.requirement_id == rid:
            out |= {c.drive_label for c in sat.copies}
    return out


# --------------------------------------------------------------------------------------------------
# Pure matrix — target eligibility (new placement)
# --------------------------------------------------------------------------------------------------
def test_matrix_new_placement_targets_active_enabled_only():
    """Fresh placement candidates only on active+enabled across the full 3×2 matrix."""
    _require()
    drives = [
        _drive("en", lifecycle="active", eligibility="enabled"),
        _drive("ex", lifecycle="active", eligibility="excluded"),
        _drive("lost-en", lifecycle="lost", eligibility="enabled"),
        _drive("lost-ex", lifecycle="lost", eligibility="excluded"),
        _drive("ret-en", lifecycle="retired", eligibility="enabled"),
        _drive("ret-ex", lifecycle="retired", eligibility="excluded"),
    ]
    inp = _input(
        selection=["org/m"],
        manifests=[("org/m", [_mf("w.safetensors", 100, HW)])],
        numcopies=[("org/m", 1)],
        drives=drives,
        archived=(),
    )
    _, cset = _run(inp)
    cs = _cands(cset, "primary:org/m")
    assert _targets(cs) == {"en"}, f"only active+enabled is placeable; got {_targets(cs)}"


def test_matrix_verified_copy_satisfaction_active_including_excluded():
    """Complete proven copies: active (+enabled or +excluded) satisfy; lost/retired never do."""
    _require()
    drives = [
        _drive("ex", lifecycle="active", eligibility="excluded"),
        _drive("lost-en", lifecycle="lost", eligibility="enabled"),
        _drive("lost-ex", lifecycle="lost", eligibility="excluded"),
        _drive("ret-en", lifecycle="retired", eligibility="enabled"),
        _drive("ret-ex", lifecycle="retired", eligibility="excluded"),
        _drive("en", lifecycle="active", eligibility="enabled"),
    ]
    archived = [
        _arch("org/m", lab, "w.safetensors", sha=HW, obytes=100, sbytes=100)
        for lab in ("ex", "lost-en", "lost-ex", "ret-en", "ret-ex")
    ]
    inp = _input(
        selection=["org/m"],
        manifests=[("org/m", [_mf("w.safetensors", 100, HW)])],
        numcopies=[("org/m", 1)],
        drives=drives,
        archived=archived,
    )
    _, cset = _run(inp)
    sat = _satisfied_drives(cset, "primary:org/m")
    assert "ex" in sat, "active+excluded complete copy must still satisfy"
    assert not (sat & {"lost-en", "lost-ex", "ret-en", "ret-ex"})
    assert _cands(cset, "primary:org/m") == ()  # already satisfied; no new placement


def test_matrix_partial_reuse_excludes_write_targets():
    """Partial archives on excluded/lost are NOT finish-in-place write targets; only fresh enabled."""
    _require()
    drives = [
        _drive("ex", lifecycle="active", eligibility="excluded"),
        _drive("lost", lifecycle="lost", eligibility="enabled"),
        _drive("fresh", lifecycle="active", eligibility="enabled"),
    ]
    archived = [
        _arch("org/m", "ex", "a.safetensors", sha=HW, obytes=100, sbytes=100),
        _arch("org/m", "lost", "a.safetensors", sha=HW, obytes=100, sbytes=100),
    ]
    inp = _input(
        selection=["org/m"],
        manifests=[("org/m", [
            _mf("a.safetensors", 100, HW),
            _mf("b.safetensors", 100, "3" * 64),
        ])],
        numcopies=[("org/m", 1)],
        drives=drives,
        archived=archived,
    )
    _, cset = _run(inp)
    # New placement (including finish-in-place writes) only on active+enabled.
    assert _targets(_cands(cset, "primary:org/m")) == {"fresh"}


def test_matrix_excluded_complete_home_satisfies_and_sources_replica():
    """Active+excluded complete home satisfies and may be a replica SourceIdentity."""
    _require()
    drives = [
        _drive("H-ex", role="primary", raid=True, lifecycle="active", eligibility="excluded",
               fs_uuid="home-uuid"),
        _drive("R", role="replica", lifecycle="active", eligibility="enabled",
               fs_uuid="rep-uuid"),
    ]
    archived = [
        _arch("org/m", "H-ex", "w.safetensors", sha=HW, obytes=100, sbytes=100, key="annex-home"),
    ]
    inp = _input(
        selection=["org/m"],
        manifests=[("org/m", [_mf("w.safetensors", 100, HW)])],
        numcopies=[("org/m", 2)],
        drives=drives,
        archived=archived,
    )
    _, cset = _run(inp)
    assert "H-ex" in _satisfied_drives(cset, "protected_home:org/m")
    rep = _cands(cset, "protected_replica:org/m")
    assert rep, "replica still needs placement on R"
    assert {c.target_drive for c in rep} == {"R"}
    # Proven home on excluded is a valid SourceIdentity for REPLICATE candidates.
    for c in rep:
        assert c.source is not None
        assert not isinstance(c.source, candidates.PendingHome)
        assert isinstance(c.source, candidates.SourceIdentity)
        assert c.source.drive_label == "H-ex"


def test_matrix_unsatisfied_home_targets_only_active_enabled():
    """Unsatisfied protected home placement never targets excluded/lost/retired primaries."""
    _require()
    drives = [
        _drive("H-ex", role="primary", raid=True, lifecycle="active", eligibility="excluded",
               fs_uuid="ex-uuid"),
        _drive("H-lost", role="primary", raid=True, lifecycle="lost", eligibility="enabled",
               fs_uuid="lost-uuid"),
        _drive("H-en", role="primary", raid=True, lifecycle="active", eligibility="enabled",
               fs_uuid="en-uuid"),
        _drive("R", role="replica", lifecycle="active", eligibility="enabled",
               fs_uuid="rep-uuid"),
    ]
    inp = _input(
        selection=["org/m"],
        manifests=[("org/m", [_mf("w.safetensors", 100, HW)])],
        numcopies=[("org/m", 2)],
        drives=drives,
        archived=(),
    )
    _, cset = _run(inp)
    home_place = _targets(_cands(cset, "protected_home:org/m"))
    assert home_place == {"H-en"}, f"fresh home only on active+enabled; got {home_place}"


def test_matrix_no_raid_fallback_skips_excluded_largest_primary():
    """No-RAID protected-home fallback must not select an excluded largest primary."""
    _require()
    drives = [
        # Largest primary is excluded — fallback must skip it.
        _drive("big-ex", role="primary", raid=False, cap=10**15,
               lifecycle="active", eligibility="excluded", fs_uuid="big"),
        _drive("small-en", role="primary", raid=False, cap=10**12,
               lifecycle="active", eligibility="enabled", fs_uuid="small"),
        _drive("R", role="replica", lifecycle="active", eligibility="enabled",
               fs_uuid="rep"),
    ]
    inp = _input(
        selection=["org/m"],
        manifests=[("org/m", [_mf("w.safetensors", 100, HW)])],
        numcopies=[("org/m", 2)],
        drives=drives,
        archived=(),
    )
    _, cset = _run(inp)
    home_place = _targets(_cands(cset, "protected_home:org/m"))
    assert home_place == {"small-en"}, (
        f"no-RAID fallback must not choose excluded largest primary; got {home_place}")


def test_matrix_no_raid_smaller_primary_complete_does_not_satisfy_home():
    """Finding 12: no-RAID protected-home satisfaction is the largest primary only — not any primary."""
    _require()
    drives = [
        _drive("big", role="primary", raid=False, cap=10**15,
               lifecycle="active", eligibility="enabled", fs_uuid="big"),
        _drive("small", role="primary", raid=False, cap=10**12,
               lifecycle="active", eligibility="enabled", fs_uuid="small"),
        _drive("R", role="replica", lifecycle="active", eligibility="enabled",
               fs_uuid="rep"),
    ]
    archived = [
        _arch("org/m", "small", "w.safetensors", sha=HW, obytes=100, sbytes=100),
    ]
    inp = _input(
        selection=["org/m"],
        manifests=[("org/m", [_mf("w.safetensors", 100, HW)])],
        numcopies=[("org/m", 2)],
        drives=drives,
        archived=archived,
    )
    _, cset = _run(inp)
    home_sat = _satisfied_drives(cset, "protected_home:org/m")
    assert "small" not in home_sat, (
        f"complete copy on smaller primary must not satisfy protected-home; sat={home_sat}")
    assert "big" not in home_sat
    # Unsatisfied home still places only on the canonical largest primary.
    assert _targets(_cands(cset, "protected_home:org/m")) == {"big"}, (
        f"home placement must target largest primary; got {_targets(_cands(cset, 'protected_home:org/m'))}")


def test_matrix_excluded_raid_plus_plain_emits_plain_home_candidate():
    """Finding 12: active+excluded RAID + placeable plain primary → plain is a home placement target."""
    _require()
    drives = [
        _drive("raid-ex", role="primary", raid=True, cap=10**15,
               lifecycle="active", eligibility="excluded", fs_uuid="raid"),
        _drive("plain-en", role="primary", raid=False, cap=10**12,
               lifecycle="active", eligibility="enabled", fs_uuid="plain"),
        _drive("R", role="replica", lifecycle="active", eligibility="enabled",
               fs_uuid="rep"),
    ]
    inp = _input(
        selection=["org/m"],
        manifests=[("org/m", [_mf("w.safetensors", 100, HW)])],
        numcopies=[("org/m", 2)],
        drives=drives,
        archived=(),
    )
    graph, cset = _run(inp)
    home_req = next(r for r in graph.desired if r.requirement_id == "protected_home:org/m")
    assert home_req.eligible_drives == ("plain-en",), home_req.eligible_drives
    home_place = _targets(_cands(cset, "protected_home:org/m"))
    assert home_place == {"plain-en"}, (
        f"plain primary must be the home candidate when RAID is excluded; got {home_place} "
        f"blocked={cset.blocked}")
    assert "protected_home:org/m" not in {b.requirement_id for b in cset.blocked}


def test_matrix_replica_sources_never_lost_or_retired():
    """Lost/retired proven copies never become SourceIdentity; active+excluded may."""
    _require()
    drives = [
        _drive("H-ex", role="primary", raid=True, lifecycle="active", eligibility="excluded",
               fs_uuid="hex"),
        _drive("H-lost", role="primary", raid=True, lifecycle="lost", eligibility="enabled",
               fs_uuid="hlost"),
        _drive("H-ret", role="primary", raid=True, lifecycle="retired", eligibility="excluded",
               fs_uuid="hret"),
        _drive("R", role="replica", lifecycle="active", eligibility="enabled",
               fs_uuid="rep"),
    ]
    archived = [
        _arch("org/m", "H-ex", "w.safetensors", sha=HW, obytes=100, sbytes=100, key="k-ex"),
        _arch("org/m", "H-lost", "w.safetensors", sha=HW, obytes=100, sbytes=100, key="k-lost"),
        _arch("org/m", "H-ret", "w.safetensors", sha=HW, obytes=100, sbytes=100, key="k-ret"),
    ]
    inp = _input(
        selection=["org/m"],
        manifests=[("org/m", [_mf("w.safetensors", 100, HW)])],
        numcopies=[("org/m", 2)],
        drives=drives,
        archived=archived,
    )
    _, cset = _run(inp)
    # Home satisfied only by active copies (excluded ok).
    home_sat = _satisfied_drives(cset, "protected_home:org/m")
    assert "H-ex" in home_sat
    assert "H-lost" not in home_sat and "H-ret" not in home_sat
    rep = _cands(cset, "protected_replica:org/m")
    assert rep
    source_labels = set()
    for c in rep:
        assert c.target_drive == "R"
        assert isinstance(c.source, candidates.SourceIdentity)
        source_labels.add(c.source.drive_label)
    assert "H-ex" in source_labels
    assert "H-lost" not in source_labels and "H-ret" not in source_labels


def test_matrix_shuffled_drive_order_deterministic():
    _require()
    def run(order):
        drives = [
            _drive(lab, lifecycle=lc, eligibility=el)
            for lab, lc, el in order
        ]
        inp = _input(
            selection=["org/a", "org/b"],
            manifests=[
                ("org/a", [_mf("a.safetensors", 10, HW)]),
                ("org/b", [_mf("b.safetensors", 20, "3" * 64)]),
            ],
            numcopies=[("org/a", 1), ("org/b", 1)],
            drives=drives,
            archived=(),
        )
        return _run(inp)

    a = [
        ("z", "active", "enabled"),
        ("a", "active", "excluded"),
        ("m", "lost", "enabled"),
        ("n", "lost", "excluded"),
        ("r", "retired", "excluded"),
    ]
    b = list(reversed(a))
    g1, c1 = run(a)
    g2, c2 = run(b)
    assert c1 == c2 and g1.requirement_set_hash == g2.requirement_set_hash


# --------------------------------------------------------------------------------------------------
# Bootstrap — plan.bootstrap() only
# --------------------------------------------------------------------------------------------------
def _mem():
    con = sqlite3.connect(":memory:", isolation_level=None)
    for stmt in db._statements(db.SCHEMA_PATH.read_text()):
        con.execute(stmt)
    return con


def _insert_drive(con, label, *, lifecycle=None, eligibility=None, role="primary", raid=0):
    cols = "drive_label,capacity_bytes,free_bytes,role,raid_backed"
    vals = [label, 10_000, 10_000, role, raid]
    # After Gate 2 these columns exist; Gate 1 red if missing when we try to set non-default.
    dcols = {r[1] for r in con.execute("PRAGMA table_info(drives)").fetchall()}
    if "lifecycle" in dcols and "eligibility" in dcols:
        cols += ",lifecycle,eligibility"
        vals.extend([lifecycle or "active", eligibility or "enabled"])
    elif lifecycle is not None or eligibility is not None:
        raise AssertionError(
            "cannot set lifecycle/eligibility: v4 columns missing (expected Gate-1 red)")
    ph = ",".join("?" * len(vals))
    con.execute(f"INSERT INTO drives({cols}) VALUES({ph})", vals)


def test_bootstrap_adds_only_missing_active_enabled():
    con = _mem()
    dcols = {r[1] for r in con.execute("PRAGMA table_info(drives)").fetchall()}
    if not {"lifecycle", "eligibility"} <= dcols:
        raise AssertionError(
            "v4 drives.lifecycle/eligibility columns required for bootstrap contract "
            "(expected Gate-1 red)")
    _insert_drive(con, "en", lifecycle="active", eligibility="enabled")
    _insert_drive(con, "ex", lifecycle="active", eligibility="excluded")
    _insert_drive(con, "lost", lifecycle="lost", eligibility="enabled")
    _insert_drive(con, "ret", lifecycle="retired", eligibility="enabled")
    # Pre-seed plan with excluded already a member — must be preserved, not re-added as "new".
    plan.create(con, "ark", name="Ark")
    plan.add_drive(con, "ark", "ex")
    out = plan.bootstrap(con, "ark")
    labels = set(plan.plan_drive_labels(con, "ark"))
    assert "en" in labels, "active+enabled missing membership must be added"
    assert "ex" in labels, "pre-existing excluded membership must be preserved"
    assert "lost" not in labels and "ret" not in labels
    # Second call idempotent.
    plan.bootstrap(con, "ark")
    assert set(plan.plan_drive_labels(con, "ark")) == labels
    # Never mutated drive axes (three-column SELECT → label → (lifecycle, eligibility)).
    rows = {
        label: (lifecycle, eligibility)
        for label, lifecycle, eligibility in con.execute(
            "SELECT drive_label, lifecycle, eligibility FROM drives")
    }
    assert rows["en"] == ("active", "enabled")
    assert rows["ex"] == ("active", "excluded")
    assert rows["lost"] == ("lost", "enabled")
    assert rows["ret"] == ("retired", "enabled")
    assert out["plan_id"] == "ark"


def test_bootstrap_does_not_delete_or_mutate_membership_axes():
    con = _mem()
    dcols = {r[1] for r in con.execute("PRAGMA table_info(drives)").fetchall()}
    if not {"lifecycle", "eligibility"} <= dcols:
        raise AssertionError("v4 columns missing (expected Gate-1 red)")
    _insert_drive(con, "kept-lost", lifecycle="lost", eligibility="enabled")
    plan.create(con, "ark", name="Ark")
    plan.add_drive(con, "ark", "kept-lost")  # historical membership of a now-lost drive
    plan.bootstrap(con, "ark")
    assert "kept-lost" in plan.plan_drive_labels(con, "ark"), (
        "bootstrap must not delete existing plan_drives rows")
    assert con.execute(
        "SELECT lifecycle, eligibility FROM drives WHERE drive_label='kept-lost'"
    ).fetchone() == ("lost", "enabled")


# --------------------------------------------------------------------------------------------------
# Compatibility totals / queue
# --------------------------------------------------------------------------------------------------
def test_placed_copies_respects_lifecycle_not_lost_or_retired():
    con = _mem()
    dcols = {r[1] for r in con.execute("PRAGMA table_info(drives)").fetchall()}
    if not {"lifecycle", "eligibility"} <= dcols:
        raise AssertionError("v4 columns missing (expected Gate-1 red)")
    con.execute("INSERT INTO models(repo_id,numcopies) VALUES('org/m',1)")
    con.execute(
        "INSERT INTO files(repo_id,rfilename,size_bytes,format,quant) "
        "VALUES('org/m','model.safetensors',100,'safetensors','bf16')")
    for lab, lc, el in (
        ("ok", "active", "enabled"),
        ("ex", "active", "excluded"),
        ("lost-en", "lost", "enabled"),
        ("lost-ex", "lost", "excluded"),
        ("ret-en", "retired", "enabled"),
        ("ret-ex", "retired", "excluded"),
    ):
        _insert_drive(con, lab, lifecycle=lc, eligibility=el)
        con.execute(
            "INSERT INTO archived(repo_id,rfilename,drive_label,orig_bytes,stored_bytes,compressed) "
            "VALUES('org/m','model.safetensors',?,100,100,0)", [lab])
    counts = librarian.placed_copies(con)
    # Active complete copies (enabled + excluded) count; all lost/retired pairs do not.
    assert counts.get("org/m") == 2, (
        f"placed_copies must count only active complete drives; got {counts} "
        "(expected Gate-1 red until lifecycle join exists)")


def test_queue_completion_agrees_with_canonical_satisfaction():
    """Lifecycle — not membership absence — must prevent lost-drive satisfaction.

    Pre-seed historical plan membership so bootstrap does not simply omit the drive; the
    catalog still sees the complete archived copy, but lifecycle forbids counting it.
    """
    con = _mem()
    dcols = {r[1] for r in con.execute("PRAGMA table_info(drives)").fetchall()}
    if not {"lifecycle", "eligibility"} <= dcols:
        raise AssertionError("v4 columns missing (expected Gate-1 red)")
    con.execute("INSERT INTO models(repo_id,numcopies) VALUES('org/m',1)")
    con.execute(
        "INSERT INTO files(repo_id,rfilename,size_bytes,format,quant,sha256) "
        "VALUES('org/m','model.safetensors',100,'safetensors','bf16',?)", [HW])
    con.execute("INSERT INTO selection(repo_id,finalized_at) VALUES('org/m','2026-01-01')")
    _insert_drive(con, "lost", lifecycle="lost", eligibility="enabled")
    con.execute(
        "INSERT INTO archived(repo_id,rfilename,drive_label,orig_sha256,orig_bytes,stored_bytes,compressed) "
        "VALUES('org/m','model.safetensors','lost',?,100,100,0)", [HW])
    plan.create(con, "ark", name="Ark")
    plan.add_drive(con, "ark", "lost")  # historical membership — still on the plan
    plan.bootstrap(con, "ark")
    assert "lost" in plan.plan_drive_labels(con, "ark")
    assert librarian.placed_copies(con).get("org/m", 0) == 0, (
        "placed_copies must not count complete copies on lost drives")
    graph = reconcile.reconcile_plan(con, "ark")
    sat_ids = {s.requirement_id for s in graph.candidates.satisfied}
    assert "primary:org/m" not in sat_ids, (
        "lost drive must not satisfy canonical requirement even with plan membership")


# --------------------------------------------------------------------------------------------------
# Reconcile → Gate-B fail-closed on state change
# --------------------------------------------------------------------------------------------------
def test_drive_facts_capture_excluded_member_exactly():
    """_drive_facts / reconcile capture eligibility+lifecycle; excluded complete copy satisfies,
    but is never a fresh placement target."""
    con = _mem()
    dcols = {r[1] for r in con.execute("PRAGMA table_info(drives)").fetchall()}
    if not {"lifecycle", "eligibility"} <= dcols:
        raise AssertionError("v4 columns missing (expected Gate-1 red)")
    con.execute("INSERT INTO models(repo_id,numcopies) VALUES('org/m',1)")
    con.execute(
        "INSERT INTO files(repo_id,rfilename,size_bytes,format,quant,sha256) "
        "VALUES('org/m','model.safetensors',100,'safetensors','bf16',?)", [HW])
    con.execute("INSERT INTO selection(repo_id,finalized_at) VALUES('org/m','2026-01-01')")
    _insert_drive(con, "ex", lifecycle="active", eligibility="excluded")
    _insert_drive(con, "en", lifecycle="active", eligibility="enabled")
    con.execute(
        "INSERT INTO archived(repo_id,rfilename,drive_label,orig_sha256,orig_bytes,stored_bytes,compressed) "
        "VALUES('org/m','model.safetensors','ex',?,100,100,0)", [HW])
    plan.create(con, "ark", name="Ark")
    plan.add_drive(con, "ark", "ex")  # existing plan member
    plan.add_drive(con, "ark", "en")
    facts = reconcile._drive_facts(con, "ark")
    by_label = {f.drive_label: f for f in facts}
    assert "ex" in by_label and "en" in by_label
    assert getattr(by_label["ex"], "lifecycle") == "active"
    assert getattr(by_label["ex"], "eligibility") == "excluded"
    assert getattr(by_label["en"], "lifecycle") == "active"
    assert getattr(by_label["en"], "eligibility") == "enabled"
    graph = reconcile.reconcile_plan(con, "ark")
    sat = {s.requirement_id for s in graph.candidates.satisfied}
    assert "primary:org/m" in sat, "active+excluded complete copy must satisfy via capture"
    # No unsatisfied placement targeting the excluded member.
    for rid, cs in graph.candidates.by_requirement:
        assert "ex" not in {c.target_drive for c in cs}, (
            f"{rid} must not place onto excluded drive; targets={[c.target_drive for c in cs]}")


def test_plan_capacity_counts_only_active_enabled_members():
    """plan.capacity() usable fleet must ignore excluded/lost/retired historical members."""
    con = _mem()
    dcols = {r[1] for r in con.execute("PRAGMA table_info(drives)").fetchall()}
    if not {"lifecycle", "eligibility"} <= dcols:
        raise AssertionError("v4 columns missing (expected Gate-1 red)")
    # Equal nominal capacity so the active+enabled subset is a clean fraction of the all-members sum.
    for lab, lc, el in (
        ("en", "active", "enabled"),
        ("ex", "active", "excluded"),
        ("lost", "lost", "enabled"),
        ("ret", "retired", "enabled"),
    ):
        _insert_drive(con, lab, lifecycle=lc, eligibility=el)
        con.execute(
            "UPDATE drives SET capacity_bytes=1000, free_bytes=1000 WHERE drive_label=?", [lab])
    plan.create(con, "ark", name="Ark")
    for lab in ("en", "ex", "lost", "ret"):
        plan.add_drive(con, "ark", lab)
    usable = plan.capacity(con, "ark")
    # Only "en" contributes: capacity 1000 − headroom(1000, False).
    only_en = plan._headroom(1000, False)
    expected = max(0, 1000 - only_en)
    assert usable == expected, (
        f"plan.capacity must count only active+enabled members; got {usable}, expected {expected} "
        f"(all four members would be ~{4 * expected})")


def _gate_b_post_capture_state_change(con, *, column: str, value: str):
    """Shared race: capture reconcile, mutate drive state, plan_capacity must not assign."""
    con.execute("INSERT INTO models(repo_id,numcopies) VALUES('org/m',1)")
    con.execute(
        "INSERT INTO files(repo_id,rfilename,size_bytes,format,quant) "
        "VALUES('org/m','model.safetensors',100,'safetensors','bf16')")
    con.execute("INSERT INTO selection(repo_id,finalized_at) VALUES('org/m','2026-01-01')")
    _insert_drive(con, "d0", lifecycle="active", eligibility="enabled")
    plan.bootstrap(con, "ark")
    graph = reconcile.reconcile_plan(con, "ark")
    con.execute(f"UPDATE drives SET {column}=? WHERE drive_label='d0'", [value])
    from modelark import capacity_evidence
    evidence = {
        "d0": capacity_evidence.Evidence(
            kind="live", executable=True, admissible_free=10**9,
            optimistic_usable_max=10**9, observed_free=10**9),
    }
    return capacity.plan_capacity(con, graph, evidence_by_drive=evidence)


def test_reconcile_to_gate_b_fails_closed_when_drive_is_retired():
    """If a drive is retired after reconcile capture, plan_capacity must not assign tasks."""
    con = _mem()
    dcols = {r[1] for r in con.execute("PRAGMA table_info(drives)").fetchall()}
    if not {"lifecycle", "eligibility"} <= dcols:
        raise AssertionError("v4 columns missing (expected Gate-1 red)")
    result = _gate_b_post_capture_state_change(con, column="lifecycle", value="retired")
    assert result.feasible is False, (
        f"retired post-reconcile must be non-feasible; gate={getattr(result, 'gate_b_code', None)}")
    assert result.tasks == (), "must not expose executable tasks after fail-closed revalidation"
    gate = getattr(result, "gate_b_code", None)
    fail_codes = {
        (f.code.value if hasattr(f.code, "value") else str(f.code)) for f in result.failures
    }
    assert gate == "TARGET_DRIVE_CHANGED" or "TARGET_DRIVE_CHANGED" in fail_codes, (
        f"expected TARGET_DRIVE_CHANGED pin; gate={gate!r} failures={fail_codes}")


def test_reconcile_to_gate_b_fails_closed_when_drive_becomes_excluded():
    """Eligibility flip to excluded after capture must also fail closed (no new placement tasks)."""
    con = _mem()
    dcols = {r[1] for r in con.execute("PRAGMA table_info(drives)").fetchall()}
    if not {"lifecycle", "eligibility"} <= dcols:
        raise AssertionError("v4 columns missing (expected Gate-1 red)")
    result = _gate_b_post_capture_state_change(con, column="eligibility", value="excluded")
    assert result.feasible is False, (
        f"excluded post-reconcile must be non-feasible; gate={getattr(result, 'gate_b_code', None)}")
    assert result.tasks == (), "must not place onto a drive that lost eligibility after capture"
    gate = getattr(result, "gate_b_code", None)
    fail_codes = {
        (f.code.value if hasattr(f.code, "value") else str(f.code)) for f in result.failures
    }
    assert gate == "TARGET_DRIVE_CHANGED" or "TARGET_DRIVE_CHANGED" in fail_codes, (
        f"expected TARGET_DRIVE_CHANGED pin; gate={gate!r} failures={fail_codes}")


def test_reconcile_to_gate_b_fails_closed_when_one_of_multiple_targets_loses_eligibility():
    """Finding 13: multi-target race — any captured target losing placeability fails closed.

    Captured candidates target both a and b; excluding only a must not leave assignment on a
    (or silently drop a and assign b). Full fail-closed before assignment.
    """
    con = _mem()
    dcols = {r[1] for r in con.execute("PRAGMA table_info(drives)").fetchall()}
    if not {"lifecycle", "eligibility"} <= dcols:
        raise AssertionError("v4 columns missing (expected Gate-1 red)")
    con.execute("INSERT INTO models(repo_id,numcopies) VALUES('org/m',1)")
    con.execute(
        "INSERT INTO files(repo_id,rfilename,size_bytes,format,quant) "
        "VALUES('org/m','model.safetensors',100,'safetensors','bf16')")
    con.execute("INSERT INTO selection(repo_id,finalized_at) VALUES('org/m','2026-01-01')")
    _insert_drive(con, "a", lifecycle="active", eligibility="enabled")
    _insert_drive(con, "b", lifecycle="active", eligibility="enabled")
    plan.bootstrap(con, "ark")
    graph = reconcile.reconcile_plan(con, "ark")
    targets = set()
    for rid, cs in graph.candidates.by_requirement:
        if rid == "primary:org/m":
            targets = {c.target_drive for c in cs}
    assert targets == {"a", "b"}, f"fixture must capture multi-target candidates; got {targets}"
    # One of two targets loses eligibility after capture.
    con.execute("UPDATE drives SET eligibility='excluded' WHERE drive_label='a'")
    from modelark import capacity_evidence
    evidence = {
        lab: capacity_evidence.Evidence(
            kind="live", executable=True, admissible_free=10**9,
            optimistic_usable_max=10**9, observed_free=10**9)
        for lab in ("a", "b")
    }
    result = capacity.plan_capacity(con, graph, evidence_by_drive=evidence)
    assert result.feasible is False, (
        f"partial placeability race must be non-feasible; gate={getattr(result, 'gate_b_code', None)} "
        f"tasks={[t.target_drive for t in result.tasks]}")
    assert result.tasks == (), (
        f"must not assign after multi-target lifecycle/eligibility race; "
        f"tasks={[t.target_drive for t in result.tasks]}")
    gate = getattr(result, "gate_b_code", None)
    fail_codes = {
        (f.code.value if hasattr(f.code, "value") else str(f.code)) for f in result.failures
    }
    assert gate == "TARGET_DRIVE_CHANGED" or "TARGET_DRIVE_CHANGED" in fail_codes, (
        f"expected TARGET_DRIVE_CHANGED pin; gate={gate!r} failures={fail_codes}")


# --------------------------------------------------------------------------------------------------
# Registration refuses existing retired label
# --------------------------------------------------------------------------------------------------
def test_retired_label_registration_still_refused_before_mutation():
    con = _mem()
    dcols = {r[1] for r in con.execute("PRAGMA table_info(drives)").fetchall()}
    if "lifecycle" in dcols:
        _insert_drive(con, "retired-drive", lifecycle="retired", eligibility="excluded")
    else:
        con.execute("INSERT INTO drives(drive_label) VALUES('retired-drive')")
    # Existing-label guard is lifecycle-blind and must still refuse.
    try:
        register._guard_existing_label(con, "retired-drive")
        raise AssertionError("retired label must be refused before physical/catalog mutation")
    except RuntimeError as exc:
        assert "retired-drive" in str(exc).lower() or "exist" in str(exc).lower(), exc


def main():
    tests = sorted((n, f) for n, f in globals().items()
                   if n.startswith("test_") and callable(f))
    passed, failed = [], []
    for name, fn in tests:
        try:
            fn()
            passed.append(name)
            print(f"PASS  {name}")
        except Exception as exc:  # noqa: BLE001
            failed.append((name, type(exc).__name__, str(exc)[:220]))
            print(f"FAIL  {name}  -> {type(exc).__name__}: {exc}")
    print(f"\n{len(passed)} passed, {len(failed)} failed")
    print("Gate-1 tests-only: lifecycle/eligibility contracts EXPECTED RED until PR-07 production.")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
