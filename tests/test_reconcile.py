"""DEC-045 Phase-1 shadow reconciler: exact facts, requirements, and intents."""
from __future__ import annotations

import sqlite3
import time
from argparse import Namespace
from unittest import mock

import pytest

import _admission_compat
from modelark import archive_manifest, cli, reconcile
from modelark.core import db


@pytest.fixture(autouse=True)
def _admission_snapshot_compat():
    """#35-C: synthesize admission evidence from free_bytes (pre-cutover snapshot semantics) so the
    shadow/explain comparison keeps exercising placement, not the evidence seam (covered by PR-04)."""
    with _admission_compat.seam_patch():
        yield


def _mem():
    con = sqlite3.connect(":memory:", isolation_level=None)
    for statement in db._statements(db.SCHEMA_PATH.read_text()):
        con.execute(statement)
    con.execute("INSERT INTO plans(plan_id,name,is_active) VALUES('ark','Ark',1)")
    return con


def _drive(con, label, *, role="primary", raid=False, capacity=10**12, identity=None):
    con.execute(
        "INSERT INTO drives(drive_label,role,raid_backed,capacity_bytes,free_bytes,fs_uuid) "
        "VALUES(?,?,?,?,?,?)",
        [label, role, int(raid), capacity, capacity, identity],
    )
    con.execute("INSERT INTO plan_drives(plan_id,drive_label) VALUES('ark',?)", [label])


def _repo(con, repo, *, copies=1, files=(("model.safetensors", 100, "safetensors", "bf16"),)):
    con.execute("INSERT INTO models(repo_id,numcopies) VALUES(?,?)", [repo, copies])
    con.execute("INSERT INTO selection(repo_id,finalized_at) VALUES(?,'2026-01-01')", [repo])
    # #36a: valid archived content needs provable provenance — give every file a manifest sha256 so its
    # matching archived orig_sha256 (below) binds reuse. Intentional legacy/hashless cases pass sha=None.
    con.executemany(
        "INSERT INTO files(repo_id,rfilename,size_bytes,format,quant,sha256) VALUES(?,?,?,?,?,?)",
        [(repo, name, size, fmt, quant, f"sha-{repo}-{name}") for name, size, fmt, quant in files],
    )


def _archive(con, repo, drive, *names):
    # Reusable archived content: orig_sha256 == the manifest sha256 and orig_bytes == the manifest size.
    for name in names:
        row = con.execute(
            "SELECT size_bytes, sha256 FROM files WHERE repo_id=? AND rfilename=?", [repo, name]
        ).fetchone()
        size, sha = (row[0], row[1]) if row else (1, None)
        con.execute(
            "INSERT INTO archived(repo_id,rfilename,drive_label,orig_sha256,orig_bytes,stored_bytes,compressed) "
            "VALUES(?,?,?,?,?,?,0)",
            [repo, name, drive, sha, size, 1],
        )


def test_incident_shape_creates_only_nine_home_fetches_and_shadow_removes_116_phantoms():
    con = _mem()
    _drive(con, "drive-00", raid=True, capacity=2 * 10**12)
    _drive(con, "drive-04", role="replica", capacity=10**12)
    names = ("model.safetensors", "config.json")
    for index in range(125):
        repo = f"org/model-{index:03d}"
        _repo(
            con, repo, copies=2,
            files=((names[0], 100, "safetensors", "bf16"), (names[1], 10, "aux", None)),
        )
        if index < 116:
            _archive(con, repo, "drive-00", *names)
        elif index < 124:
            _archive(con, repo, "drive-00", names[0])

    result = reconcile.reconcile_plan(con, "ark")
    homes = [i for i in result.intents if i.requirement_id.startswith("protected_home:")]
    replicas = [i for i in result.intents if i.requirement_id.startswith("protected_replica:")]
    assert len(result.requirements) == 250
    assert len(homes) == 9
    assert len(replicas) == 125
    assert sum(i.source_drive == "drive-00" for i in replicas) == 116
    assert sum(i.depends_on_requirement is not None for i in replicas) == 9
    # #36a: reconcile emits no pin. The 8 shard-partial homes carry a finish-in-place candidate on
    # drive-00 (reused files); the 9th (nothing archived) has a fresh-only candidate. #38 chooses.
    by_req = dict(result.candidates.by_requirement)
    home_reqs = [r for r in by_req if r.startswith("protected_home:")]
    assert len(home_reqs) == 9
    assert sum(any(c.reused_files for c in by_req[r]) for r in home_reqs) == 8
    assert sum(all(not c.reused_files for c in by_req[r]) for r in home_reqs) == 1
    assert all(c.target_drive == "drive-00" for r in home_reqs for c in by_req[r])

    report = reconcile.shadow_report(con, "ark")
    assert report["shadow"]["satisfied_legacy_reservations_removed"] == 116
    assert report["shadow"]["new_intents"] == 134
    assert report["shadow"]["executor"].startswith("reconciled")
    assert report["placement_comparison"]["target_equivalent"] is True


def test_exact_sets_prevent_extra_file_from_masking_missing_required_file():
    con = _mem()
    _drive(con, "raid", raid=True)
    _drive(con, "replica", role="replica")
    _repo(
        con, "org/model", copies=2,
        files=(
            ("model.safetensors", 100, "safetensors", "bf16"),
            ("config.json", 10, "aux", None),
            ("ignored.onnx", 100, "onnx", None),
        ),
    )
    _archive(con, "org/model", "raid", "model.safetensors", "ignored.onnx")
    result = reconcile.reconcile_plan(con, "ark")
    fact = next(f for f in result.facts if f.drive_label == "raid")
    assert not fact.complete
    assert fact.required_files == {"model.safetensors", "config.json"}
    # #36a: the finish-in-place candidate on raid reuses the proven weight and lists the missing config
    # as exact work — the extra ignored.onnx never masks the missing required file.
    home_cands = dict(result.candidates.by_requirement)["protected_home:org/model"]
    on_raid = next(c for c in home_cands if c.target_drive == "raid")
    assert {f.rfilename for f in on_raid.reused_files} == {"model.safetensors"}
    assert {f.rfilename for f in on_raid.missing_files} == {"config.json"}


def test_multiple_partials_are_emitted_without_unioning_drives():
    con = _mem()
    _drive(con, "raid-a", raid=True)
    _drive(con, "raid-b", raid=True)
    _drive(con, "replica", role="replica")
    _repo(
        con, "org/model", copies=2,
        files=(
            ("model.safetensors", 100, "safetensors", "bf16"),
            ("config.json", 10, "aux", None),
            ("tokenizer.json", 20, "aux", None),
        ),
    )
    _archive(con, "org/model", "raid-a", "model.safetensors", "config.json")
    _archive(con, "org/model", "raid-b", "config.json", "tokenizer.json")
    result = reconcile.reconcile_plan(con, "ark")
    # #36a emits BOTH finish-in-place partials as separate candidates (no union, no choice — #38 ranks
    # by the least-missing cost). raid-a misses only tokenizer.json; raid-b misses model.safetensors.
    by_target = {c.target_drive: c for c in dict(result.candidates.by_requirement)["protected_home:org/model"]}
    assert set(by_target) == {"raid-a", "raid-b"}
    assert {f.rfilename for f in by_target["raid-a"].missing_files} == {"tokenizer.json"}
    assert {f.rfilename for f in by_target["raid-b"].missing_files} == {"model.safetensors"}
    assert not any(f.complete for f in result.facts)


def test_wrong_tier_copy_is_preserved_as_drift_not_home_satisfaction():
    con = _mem()
    _drive(con, "raid", raid=True)
    _drive(con, "plain")
    _drive(con, "replica", role="replica")
    _repo(con, "org/model", copies=2)
    _archive(con, "org/model", "plain", "model.safetensors")
    result = reconcile.reconcile_plan(con, "ark")
    assert "protected_home:org/model" not in result.satisfied
    assert any(i.requirement_id == "protected_home:org/model" for i in result.intents)
    assert any(d.code == "COPY_POLICY_DRIFT" for d in result.diagnostics)


def test_root_target_failure_does_not_spam_source_incomplete():
    con = _mem()
    _drive(con, "replica", role="replica")
    _repo(con, "org/model", copies=2)
    result = reconcile.reconcile_plan(con, "ark")
    codes = [item.code for item in result.diagnostics]
    assert codes.count("TARGET_TIER_MISSING") == 1
    assert "SOURCE_INCOMPLETE" not in codes
    replica = next(i for i in result.intents if i.kind == reconcile.TaskKind.REPLICATE)
    assert replica.depends_on_requirement == "protected_home:org/model"


def test_failure_domain_warning_contains_labels_not_identity():
    con = _mem()
    _drive(con, "raid", raid=True, identity="same-device")
    _drive(con, "replica", role="replica", identity="same-device")
    _repo(con, "org/model", copies=2)
    _archive(con, "org/model", "raid", "model.safetensors")
    _archive(con, "org/model", "replica", "model.safetensors")
    result = reconcile.reconcile_plan(con, "ark")
    warning = next(d for d in result.diagnostics if d.code == "FAILURE_DOMAIN_SUSPECT")
    assert dict(warning.detail)["drives"] == ("raid", "replica")
    assert "same-device" not in str(warning.detail)


def test_normalizer_only_removes_independently_satisfied_requirements():
    con = _mem()
    _drive(con, "raid", raid=True)
    _drive(con, "replica", role="replica")
    _repo(con, "done", copies=2)
    _repo(con, "todo", copies=2)
    _archive(con, "done", "raid", "model.safetensors")
    result = reconcile.reconcile_plan(con, "ark")
    rows = (
        reconcile.LegacyReservation("protected_home:done", "done", "raid", 0),
        reconcile.LegacyReservation("protected_home:todo", "todo", "MUTATED-TARGET", 9),
        reconcile.LegacyReservation("unexpected:todo", "todo", "replica", 10),
    )
    normalized = reconcile.normalize_legacy_reservations(rows, result)
    assert normalized == rows[1:]
    assert normalized[0].target_drive == "MUTATED-TARGET" and normalized[0].order == 9


def test_graph_json_is_deterministic_and_tracks_stored_byte_facts():
    con = _mem()
    _drive(con, "primary")
    _repo(con, "org/model")
    _archive(con, "org/model", "primary", "model.safetensors")
    first = reconcile.reconcile_plan(con, "ark").to_dict()
    again = reconcile.reconcile_plan(con, "ark").to_dict()
    assert first == again
    assert first["facts"][0]["stored_bytes_by_file"] == {"model.safetensors": 1}

    con.execute(
        "UPDATE archived SET stored_bytes=2 WHERE repo_id='org/model' AND drive_label='primary'"
    )
    changed = reconcile.reconcile_plan(con, "ark").to_dict()
    assert changed["graph_hash"] != first["graph_hash"]


def test_pickle_manifest_is_not_aux_only_complete():
    con = _mem()
    _drive(con, "primary")
    _repo(
        con, "pickle/model",
        files=(("pytorch_model.bin", 100, "pytorch", "fp16"), ("config.json", 10, "aux", None)),
    )
    _archive(con, "pickle/model", "primary", "config.json")
    policy = archive_manifest.ArchivePolicy(allow_pickle=True)
    result = reconcile.reconcile_plan(con, "ark", policy=policy)
    fact = result.facts[0]
    assert not fact.complete and "pytorch_model.bin" in fact.required_files
    assert len(result.intents) == 1


def test_cli_explain_is_read_only_and_reports_shadow_json(capsys):
    con = _mem()
    _drive(con, "primary")
    _repo(con, "org/model")
    args = Namespace(explain=True, apply=False, repo=None, json=False, max_24h_gb=0)
    with mock.patch.object(cli.db, "connect", return_value=con) as connect:
        cli.cmd_library_plan(args)
    connect.assert_called_once_with(read_only=True)
    output = capsys.readouterr().out
    assert '"executor": "reconciled (legacy data is comparison-only)"' in output
    assert '"graph_hash"' in output


def test_cli_explain_refuses_apply_without_opening_database():
    args = Namespace(explain=True, apply=True, repo=None, json=False, max_24h_gb=0)
    with mock.patch.object(cli.db, "connect") as connect:
        try:
            cli.cmd_library_plan(args)
            raise AssertionError("--explain --apply must fail")
        except SystemExit as exc:
            assert "read-only" in str(exc)
    connect.assert_not_called()


def test_shadow_report_preserves_new_graph_when_legacy_adapter_breaks():
    con = _mem()
    _drive(con, "primary")
    _repo(con, "org/model")
    with mock.patch("modelark.librarian.plan_placements", return_value={}):
        report = reconcile.shadow_report(con, "ark")
    assert report["intents"][0]["repo"] == "org/model"
    assert report["shadow"]["legacy_error"].startswith("KeyError:")
    assert report["shadow"]["legacy_reservations"] == 0


def test_reconciliation_100k_archived_rows_stays_bounded():
    con = _mem()
    drives = [f"drive-{index:02d}" for index in range(10)]
    for drive in drives:
        _drive(con, drive)
    repos = [f"org/model-{index:04d}" for index in range(1000)]
    con.execute("BEGIN")
    con.executemany(
        "INSERT INTO models(repo_id,numcopies) VALUES(?,1)", [(repo,) for repo in repos]
    )
    con.executemany(
        "INSERT INTO selection(repo_id,finalized_at) VALUES(?,'2026-01-01')",
        [(repo,) for repo in repos],
    )
    files = [
        (repo, f"shard-{index:02d}.safetensors", 100, "safetensors", "bf16")
        for repo in repos for index in range(10)
    ]
    con.executemany(
        "INSERT INTO files(repo_id,rfilename,size_bytes,format,quant) VALUES(?,?,?,?,?)", files
    )
    archived = [
        (repo, f"shard-{index:02d}.safetensors", drive, 67, 1)
        for repo in repos for index in range(10) for drive in drives
    ]
    con.executemany(
        "INSERT INTO archived(repo_id,rfilename,drive_label,stored_bytes,compressed) VALUES(?,?,?,?,?)",
        archived,
    )
    con.execute("COMMIT")
    # Do not let fixture-construction memory distort the reconciliation measurement
    # or remain live during the rest of the suite.
    del archived, files

    try:
        started = time.perf_counter()
        result = reconcile.reconcile_plan(con, "ark")
        elapsed = time.perf_counter() - started
        assert elapsed < 2.0, f"100k-row reconciliation took {elapsed:.3f}s"
        assert len(result.facts) == 10_000
        assert len(result.requirements) == 1000
        assert sum(len(item.eligible_drives) for item in result.requirements) == 10_000
        assert not result.intents
    finally:
        con.close()
