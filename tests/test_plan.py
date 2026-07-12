"""The first-class Plan entity (#33): bootstrap idempotency, active-plan exclusivity, plan_drives
ownership, and the three LIVE totals (uncompressed / compressed / capacity) — copy-aware, with the
compressed number blending real archived bytes + an observed-ratio estimate for the rest.

Runs on an in-memory catalog built from the real schema.sql (so the plans/plan_drives DDL + the views
are exercised too). No portal, no drives mounted — totals() reads the catalog tables only.
"""
from __future__ import annotations

import sqlite3

from modelark.core import db
from modelark import librarian, plan


def _mem() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:", isolation_level=None)   # autocommit, matching db.connect()
    for stmt in db._statements(db.SCHEMA_PATH.read_text()):
        con.execute(stmt)
    return con


def _seed_drives(con):
    # drive-00 = RAID 1000 B, drive-01 = plain 1000 B. Small byte caps → the 5% first-tranche headroom
    # applies to both (cap < 1 TB), and the RAID 3% floor (30) loses to the 5% tranche (50) → both 50.
    con.execute("INSERT INTO drives(drive_label,capacity_bytes,free_bytes,role,raid_backed) "
                "VALUES('drive-00',1000,1000,'primary',1)")
    con.execute("INSERT INTO drives(drive_label,capacity_bytes,free_bytes,role,raid_backed) "
                "VALUES('drive-01',1000,1000,'primary',0)")


def _seed_selection(con):
    # m1 numcopies=1: one bf16 safetensors (100, compressible) + one aux (10). raw=110, comp=100, non=10.
    # m2 numcopies=2: one bf16 safetensors (200, compressible).                raw=200, comp=200, non=0.
    con.execute("INSERT INTO models(repo_id,numcopies) VALUES('m1',1)")
    con.execute("INSERT INTO models(repo_id,numcopies) VALUES('m2',2)")
    con.execute("INSERT INTO files(repo_id,rfilename,size_bytes,format,quant) VALUES('m1','model.safetensors',100,'safetensors','bf16')")
    con.execute("INSERT INTO files(repo_id,rfilename,size_bytes,format,quant) VALUES('m1','config.json',10,'aux',NULL)")
    con.execute("INSERT INTO files(repo_id,rfilename,size_bytes,format,quant) VALUES('m2','model.safetensors',200,'safetensors','bf16')")
    con.execute("INSERT INTO selection(repo_id,finalized_at) VALUES('m1','2026-01-01')")
    con.execute("INSERT INTO selection(repo_id,finalized_at) VALUES('m2','2026-01-01')")


# ---- bootstrap + CRUD --------------------------------------------------------

def test_bootstrap_idempotent_and_owns_drives():
    con = _mem()
    _seed_drives(con)
    p = plan.bootstrap(con)
    assert p["plan_id"] == "ark" and p["is_active"] is True
    assert p["drives"] == ["drive-00", "drive-01"], p["drives"]
    # second call changes nothing (no dup plans, no dup drive rows, still exactly one active)
    plan.bootstrap(con)
    assert con.execute("SELECT count(*) FROM plans").fetchone()[0] == 1
    assert con.execute("SELECT count(*) FROM plan_drives WHERE plan_id='ark'").fetchone()[0] == 2
    assert con.execute("SELECT count(*) FROM plans WHERE is_active").fetchone()[0] == 1


def test_active_exclusivity():
    con = _mem()
    plan.bootstrap(con)
    plan.create(con, "scratch", name="Scratch")
    plan.set_active(con, "scratch")
    assert plan.active(con)["plan_id"] == "scratch"
    assert con.execute("SELECT count(*) FROM plans WHERE is_active").fetchone()[0] == 1
    plan.set_active(con, "ark")
    assert plan.active(con)["plan_id"] == "ark"


def test_add_drive_idempotent_and_bad_provisioning():
    con = _mem()
    plan.bootstrap(con)
    plan.add_drive(con, "ark", "drive-09")
    plan.add_drive(con, "ark", "drive-09")                                   # idempotent
    assert plan.plan_drive_labels(con, "ark") == ["drive-09"]               # (no drives seeded)
    try:
        plan.create(con, "bad", provisioning="lz4")
        assert False, "bad provisioning must raise"
    except ValueError:
        pass


# ---- totals: the three live numbers -----------------------------------------

def test_totals_empty_archive():
    con = _mem()
    _seed_drives(con)
    _seed_selection(con)
    plan.bootstrap(con)
    t = plan.totals(con, "ark")
    # capacity = (1000-50) + (1000-50) = 1900
    assert t["capacity"] == 1900, t
    # uncompressed = raw(m1)*1 + raw(m2)*2 = 110 + 400 = 510  (copy-aware)
    assert t["uncompressed"] == 510, t
    # compressed = 0 archived + est: m1 (100*0.67+10)*1.08=int(83.16)=83 ×1 ; m2 (200*0.67)*1.08=int(144.72)=144 ×2
    #            = 83 + 288 = 371
    assert t["compressed"] == 371, t
    assert t["n_selection"] == 2 and t["n_must"] == 1, t
    assert t["over_uncompressed"] is False and t["over_compressed"] is False


def test_totals_with_a_placed_copy():
    con = _mem()
    _seed_drives(con)
    _seed_selection(con)
    plan.bootstrap(con)
    # m1's ONE copy is fully down on drive-01 (both planned files present) → placed_copies(m1)=1.
    for rf, sb in [("model.safetensors", 70), ("config.json", 10)]:
        con.execute("INSERT INTO archived(repo_id,rfilename,drive_label,orig_bytes,stored_bytes,compressed) "
                    "VALUES('m1',?, 'drive-01', ?, ?, 1)", [rf, sb, sb])
    t = plan.totals(con, "ark")
    assert librarian.placed_copies(con).get("m1") == 1, "m1 should read as a complete copy"
    # uncompressed unchanged (footprint of the selection): 510
    assert t["uncompressed"] == 510, t
    # compressed = actual archived (70+10=80) + est for the REST (m1 remaining 0 → 0; m2 remaining 2 → 288)
    assert t["compressed"] == 80 + 288, t


def test_compressed_scoped_to_selection():
    # Greptile P1: `compressed` must count archived bytes ONLY for the selection — an archived row for a
    # repo no longer selected (e.g. after re-curating the cart) must NOT inflate the footprint.
    con = _mem()
    _seed_drives(con)
    _seed_selection(con)                                   # selection = {m1, m2}
    plan.bootstrap(con)
    baseline = plan.totals(con, "ark")["compressed"]       # 371 (est only; nothing archived)
    # a stray archived row for a repo NOT in the selection
    con.execute("INSERT INTO models(repo_id,numcopies) VALUES('stray',1)")
    con.execute("INSERT INTO archived(repo_id,rfilename,drive_label,orig_bytes,stored_bytes,compressed) "
                "VALUES('stray','x.safetensors','drive-00',9000,9000,1)")
    assert plan.totals(con, "ark")["compressed"] == baseline, "stray archived bytes must not leak in"
    # but a placed copy of a SELECTED repo does count (m1 fully archived → its actual bytes replace its est)
    for rf, sb in [("model.safetensors", 60), ("config.json", 10)]:
        con.execute("INSERT INTO archived(repo_id,rfilename,drive_label,orig_bytes,stored_bytes,compressed) "
                    "VALUES('m1',?, 'drive-01', ?, ?, 1)", [rf, sb, sb])
    assert plan.totals(con, "ark")["compressed"] == 70 + 288, "selected-repo archived bytes DO count"


def test_capacity_only_counts_plan_drives():
    con = _mem()
    _seed_drives(con)
    plan.bootstrap(con)                                    # ark owns drive-00 + drive-01 → 1900
    con.execute("INSERT INTO drives(drive_label,capacity_bytes,free_bytes,role) VALUES('drive-77',5000,5000,'primary')")
    # drive-77 exists but is NOT in ark's plan_drives → capacity stays 1900 (the plan is the boundary)
    assert plan.totals(con, "ark")["capacity"] == 1900
    plan.add_drive(con, "ark", "drive-77")
    assert plan.totals(con, "ark")["capacity"] == 1900 + (5000 - librarian.headroom_bytes(5000))


def test_registration_adds_to_active_plan():
    # #34: register._add_to_active_plan folds a freshly-registered drive into the ACTIVE plan (the
    # drives row already exists — register_drive upserts it just before calling this).
    from modelark import register
    con = _mem()
    con.execute("INSERT INTO drives(drive_label,capacity_bytes,free_bytes,role) VALUES('drive-00',1000,1000,'primary')")
    pid = register._add_to_active_plan(con, "drive-00")                 # fresh catalog → bootstraps ark
    assert pid == "ark"
    assert "drive-00" in plan.plan_drive_labels(con, "ark")
    assert plan.totals(con, "ark")["capacity"] == 1000 - librarian.headroom_bytes(1000)
    # a drive registered while a NON-ark plan is active joins THAT plan, and does NOT leak into ark
    plan.create(con, "scratch", name="Scratch")
    plan.set_active(con, "scratch")
    con.execute("INSERT INTO drives(drive_label,capacity_bytes,free_bytes,role) VALUES('drive-01',2000,2000,'primary')")
    assert register._add_to_active_plan(con, "drive-01") == "scratch"
    assert plan.plan_drive_labels(con, "scratch") == ["drive-01"]
    assert "drive-01" not in plan.plan_drive_labels(con, "ark")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all passed")
