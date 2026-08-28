"""INC-028 Gate-1 contracts — content satisfaction and exact completion.

Expected-red until Gate 2.  These contracts deliberately cross the canonical planner,
FETCH-resume, Fill-drain, and replica seams so a correct-but-unused predicate cannot
green the gate.  Production code is unchanged in Gate 1.
"""
from __future__ import annotations

import inspect
import sqlite3
from contextlib import contextmanager
from types import SimpleNamespace
from unittest import mock

import pytest

import _pr09_gate1_fixtures as f
from modelark import archive_hash, archive_manifest, candidates, execution_projection, fetch, wishlist
from modelark import fill as fill_mod
from modelark.core import db


REPO = "org/inc028"
FILE = "weights-main.safetensors"
APPROVED = "a" * 64
OTHER = "b" * 64
RID = f"primary:{REPO}"


def _shared_predicate():
    fn = getattr(archive_hash, "content_satisfies", None)
    assert callable(fn), (
        "INC-028: export one pure archive_hash.content_satisfies predicate; "
        "expected red until Gate 2"
    )
    return fn


def _call_shared(*, approved=APPROVED, orig=APPROVED, compressed=False,
                 annex_key=None):
    return _shared_predicate()(
        approved_sha256=approved,
        orig_sha256=orig,
        compressed=compressed,
        annex_key=annex_key,
    )


def _seed_repo(con, *, repo=REPO, catalog_sha=APPROVED, copies=1):
    con.execute(
        "INSERT OR IGNORE INTO models(repo_id,numcopies) VALUES(?,?)",
        [repo, copies],
    )
    con.execute(
        "INSERT OR IGNORE INTO files(repo_id,rfilename,size_bytes,format,quant,sha256) "
        "VALUES(?,?,100,'safetensors','bf16',?)",
        [repo, FILE, catalog_sha],
    )


def _seed_archived(con, *, repo=REPO, drive="d0", orig=APPROVED,
                   compressed=False, annex_key=None, provenance=None):
    con.execute(
        "INSERT INTO archived(repo_id,rfilename,stored_name,stored_relpath,drive_label,"
        "orig_sha256,orig_sha256_provenance,orig_bytes,stored_bytes,compressed,annex_key) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        [repo, FILE, FILE, FILE, drive, orig, provenance, 100, 100,
         int(compressed), annex_key],
    )


def _manifest(*, sha=APPROVED):
    return (
        archive_manifest.ManifestFile(
            rfilename=FILE,
            size_bytes=100,
            sha256=sha,
            format="safetensors",
            quant="bf16",
            storage_action="compress",
        ),
    )


def _canonical_satisfaction(*, planned, orig, compressed, annex_key, orig_bytes=100):
    inp = candidates.PlannerInput(
        plan_id="ark",
        selection=(REPO,),
        manifests=((REPO, tuple(planned)),),
        numcopies=((REPO, 1),),
        drives=(candidates.DriveFact(
            drive_label="d0",
            role="primary",
            raid_backed=False,
            capacity_bytes=10_000,
            filesystem_capacity_bytes=10_000,
            identity_epoch=1,
        ),),
        archived=(candidates.ArchivedFileFact(
            repo_id=REPO,
            drive_label="d0",
            rfilename=FILE,
            orig_sha256=orig,
            orig_bytes=orig_bytes,
            stored_bytes=100,
            annex_key=annex_key,
            compressed=compressed,
        ),),
        compression_cfg=(),
        float_ratio=1.0,
    )
    graph = candidates.requirements(inp)
    return candidates.candidates(inp, graph)


def _satisfying_labels(candidate_set) -> list[str]:
    return [
        copy.drive_label
        for satisfaction in candidate_set.satisfied
        for copy in satisfaction.copies
    ]


def _fetch_outcome(*, stored=()):
    return {
        "stored_repos": list(stored),
        "failed_repos": [],
        "capacity_failure": None,
        "terminal_failure": None,
        "terminal_repo": None,
        "throttled": False,
        "stopped": False,
        "drive_unwritable": False,
        "gated_repos": [],
        "gated_retry": None,
    }


def _projection(*, repo=REPO, source=None, rid=RID, target="d0"):
    return SimpleNamespace(tasks=(SimpleNamespace(
        row_kind="executable",
        repo_id=repo,
        target_drive=target,
        source_drive=source,
        requirement_id=rid,
        schedule_state="ready",
        order_key=1,
        guaranteed_durable=100,
        expected_durable=100,
    ),))


def _proposal_files(*, repo=REPO, rid=RID, sha=APPROVED):
    return [{
        "requirement_id": rid,
        "rfilename": FILE,
        "size_bytes": 100,
        "orig_sha256": sha,
        "format": "safetensors",
        "quant": "bf16",
        "storage_action": "compress",
    }]


def _session_start(projection, pfiles):
    return SimpleNamespace(
        projection=projection,
        session=SimpleNamespace(
            approved_proposal_id="inc028-g1",
            fencing_token=1,
            session_id="s-inc028",
        ),
        execution_config=SimpleNamespace(capacity_mode="guaranteed"),
        _proposal_files=list(pfiles),
    )


# ---------------------------------------------------------------------------
# Pure shared rule — exact matrix plus a falsifiable no-live-lookup pin.
# ---------------------------------------------------------------------------


def test_c01_shared_predicate_has_scalar_frozen_evidence_signature():
    fn = _shared_predicate()
    names = set(inspect.signature(fn).parameters)
    assert names == {
        "approved_sha256", "orig_sha256", "compressed", "annex_key",
    }, names
    forbidden = {"con", "connection", "repo_id", "drive_label", "policy"}
    assert names.isdisjoint(forbidden), names


def test_c02_shared_predicate_matrix_and_no_live_lookup(monkeypatch):
    def poisoned(*_args, **_kwargs):
        raise AssertionError("pure content predicate attempted a live lookup")

    monkeypatch.setattr(archive_manifest, "manifest_for_repo", poisoned)
    monkeypatch.setattr(wishlist, "load", poisoned)
    monkeypatch.setattr(db, "connect", poisoned)

    assert _call_shared(approved=APPROVED, orig=APPROVED) is True
    assert _call_shared(approved=APPROVED, orig=OTHER) is False
    assert _call_shared(approved=APPROVED, orig=None, compressed=True) is False
    approved_key = f"SHA256E-s100--{APPROVED}.bin"
    raw_key = f"SHA256E-s100--{OTHER}.bin"
    assert _call_shared(approved=APPROVED, orig=None, compressed=False,
                        annex_key=approved_key) is True
    assert _call_shared(approved=APPROVED, orig=None, compressed=False,
                        annex_key=raw_key) is False
    assert _call_shared(approved=None, orig=None, compressed=False,
                        annex_key=raw_key) is True
    assert _call_shared(approved=None, orig=None, compressed=True,
                        annex_key=raw_key) is False
    assert _call_shared(approved=None, orig=None, compressed=False,
                        annex_key="MD5E-s100--deadbeef.bin") is False


def test_c02b_existing_content_rules_delegate_to_shared_predicate(monkeypatch):
    raw_key = f"SHA256E-s100--{APPROVED}.bin"
    trace = mock.Mock(side_effect=[True, False])
    monkeypatch.setattr(archive_hash, "content_satisfies", trace, raising=False)

    fill_result = fill_mod._archive_content_satisfies(
        APPROVED, orig_sha256=None, compressed=False, annex_key=raw_key,
    )
    projection_result = execution_projection._file_content_satisfied(
        {(REPO, FILE, "d0"): {
            "orig_sha256": None, "compressed": False, "annex_key": raw_key,
        }},
        REPO, FILE, "d0", APPROVED,
    )

    assert fill_result is True
    assert projection_result is False, "spy return must control the existing projection helper"
    assert trace.call_args_list == [mock.call(
        approved_sha256=APPROVED,
        orig_sha256=None,
        compressed=False,
        annex_key=raw_key,
    ), mock.call(
        approved_sha256=APPROVED,
        orig_sha256=None,
        compressed=False,
        annex_key=raw_key,
    )]


# ---------------------------------------------------------------------------
# Canonical planner and Fill guard wiring — spy return must control the real consumer.
# ---------------------------------------------------------------------------


def test_c03_canonical_planner_calls_shared_rule_with_archive_evidence():
    planned = _manifest(sha=None)
    trace = mock.Mock(return_value=False)

    with mock.patch.object(archive_hash, "content_satisfies", trace, create=True):
        result = _canonical_satisfaction(
            planned=planned,
            orig=None,
            compressed=True,
            annex_key=None,
        )

    assert trace.call_count == 1, "canonical satisfaction must call the shared predicate"
    assert _satisfying_labels(result) == [], "size agreement cannot replace digest evidence"
    assert trace.call_args.kwargs == {
        "approved_sha256": None,
        "orig_sha256": None,
        "compressed": True,
        "annex_key": None,
    }


def test_c04_canonical_hashless_raw_sha256e_can_satisfy_but_size_still_must_match():
    key = f"SHA256E-s100--{OTHER}.bin"
    planned = _manifest(sha=None)
    trace = mock.Mock(return_value=True)

    with mock.patch.object(archive_hash, "content_satisfies", trace, create=True):
        satisfied = _canonical_satisfaction(
            planned=planned,
            orig=None,
            compressed=False,
            annex_key=key,
        )
        mismatched_size = _canonical_satisfaction(
            planned=planned,
            orig=None,
            compressed=False,
            annex_key=key,
            orig_bytes=99,
        )

    assert _satisfying_labels(satisfied) == ["d0"]
    assert _satisfying_labels(mismatched_size) == []
    reused = satisfied.satisfied[0].copies[0].reused_files[0]
    assert reused.bound_hash == OTHER
    assert reused.proof_source == candidates.ProofSource.RAW_ANNEX_SHA256E
    assert trace.call_count == 1, "size mismatch must fail before digest satisfaction"


def test_c05_fill_file_guard_uses_shared_rule_not_presence(monkeypatch):
    con = f.mem_con()
    f.seed_plan_selection(con, repos=(REPO,))
    con.execute(
        "UPDATE files SET rfilename=?,size_bytes=100,sha256=? WHERE repo_id=?",
        [FILE, APPROVED, REPO],
    )
    _seed_archived(con, orig=OTHER)
    task = SimpleNamespace(
        requirement_id=RID,
        task_id=RID,
        target_drive="d0",
        budget=SimpleNamespace(file_budgets=(SimpleNamespace(rfilename=FILE),)),
    )
    item = _manifest()[0]
    trace = mock.Mock(return_value=False)

    monkeypatch.setattr(archive_hash, "content_satisfies", trace, raising=False)
    monkeypatch.setattr(fill_mod.fetch, "_observe_drive", lambda *_a: object())
    monkeypatch.setattr(fill_mod.admission, "execution_evidence", lambda *_a, **_k: object())
    monkeypatch.setattr(
        fill_mod.capacity, "inspect_drives",
        lambda *_a, **_k: [SimpleNamespace(drive_label="d0")],
    )
    monkeypatch.setattr(fill_mod.capacity, "preflight_file", lambda *_a, **_k: None)

    allowed = fill_mod._file_guard(
        fetch.RunCtx(con=con), "ark", "guaranteed", task,
    )(REPO, item)
    assert trace.call_count == 1, "_file_guard must call the shared predicate"
    assert allowed is True, "present-but-unsatisfied evidence must not be skipped"


# ---------------------------------------------------------------------------
# FETCH resume classification and completion re-derivation.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("orig", "compressed", "annex_key", "code", "resolved"),
    [
        (OTHER, False, None, "APPROVED_TARGET_DIGEST_MISMATCH", OTHER),
        (None, False, f"SHA256E-s100--{OTHER}.bin",
         "APPROVED_TARGET_DIGEST_MISMATCH", OTHER),
        (None, True, None, "APPROVED_TARGET_DIGEST_UNPROVABLE", None),
        (None, False, "MD5E-s100--deadbeef.bin",
         "APPROVED_TARGET_DIGEST_UNPROVABLE", None),
    ],
)
def test_c06_explicit_fetch_refuses_unsatisfied_present_target_before_download(
        tmp_path, orig, compressed, annex_key, code, resolved):
    con = f.mem_con()
    _seed_repo(con)
    _seed_archived(
        con, orig=orig, compressed=compressed, annex_key=annex_key,
    )
    target = tmp_path / FILE
    target.write_bytes(b"preserve-existing-archive-bytes")
    row_before = con.execute(
        "SELECT * FROM archived WHERE repo_id=? AND rfilename=? AND drive_label='d0'",
        [REPO, FILE],
    ).fetchone()
    bytes_before = target.read_bytes()
    trace = mock.Mock(return_value=False)
    refusal_type = getattr(fetch, "FetchRequirementRefusal", None)
    assert isinstance(refusal_type, type), (
        "approved-target conflicts are per-requirement refusals, not run terminals"
    )
    assert not issubclass(refusal_type, fetch.FetchTerminalError)

    with mock.patch.object(archive_hash, "content_satisfies", trace, create=True), \
            mock.patch.object(fetch, "_download_shard") as download:
        with pytest.raises(refusal_type) as ei:
            fetch.fetch_model(
                fetch.RunCtx(con=con), REPO, tmp_path, "d0", False,
                {"max_compress_ram_gb": 4.0, "threads": 1},
                manifest=_manifest(),
            )

    exc = ei.value
    assert exc.code == code, exc
    assert download.call_count == 0, "catalog conflict must be decided before transfer"
    assert trace.call_count == 1, "explicit-manifest resume must call shared predicate"
    assert exc.evidence == {
        "repo_id": REPO,
        "rfilename": FILE,
        "drive_label": "d0",
        "approved_sha256": APPROVED,
        "resolved_sha256": resolved,
        "archived_orig_sha256": orig,
        "compressed": bool(compressed),
        "annex_key": annex_key,
    }
    if code.endswith("UNPROVABLE"):
        assert exc.actions[0] == "run_repair_drive", exc.actions
        assert not any("refetch" in action for action in exc.actions[:1])
    else:
        assert exc.actions[:2] == ("inspect_target", "disposition_required"), exc.actions
    row_after = con.execute(
        "SELECT * FROM archived WHERE repo_id=? AND rfilename=? AND drive_label='d0'",
        [REPO, FILE],
    ).fetchone()
    assert row_after == row_before, "pre-download refusal must not launder archive metadata"
    assert target.read_bytes() == bytes_before, "pre-download refusal must preserve worktree bytes"


@pytest.mark.parametrize(
    ("code", "actions"),
    [
        ("APPROVED_TARGET_DIGEST_MISMATCH", ("inspect_target", "disposition_required")),
        ("APPROVED_TARGET_DIGEST_UNPROVABLE", ("run_repair_drive", "resume_same_approval")),
    ],
)
def test_c06b_conflict_is_recorded_per_repo_and_fetch_continues(
        tmp_path, code, actions):
    refusal_type = getattr(fetch, "FetchRequirementRefusal", None)
    assert isinstance(refusal_type, type), "define a non-terminal per-requirement refusal"
    refusal = refusal_type(
        code,
        "approved target content requires operator disposition",
        evidence={"repo_id": REPO, "rfilename": FILE, "drive_label": "d0"},
        actions=actions,
    )
    other = "org/continues"
    calls = []

    def fake_fetch_model(_ctx, repo_id, *_args, **_kwargs):
        calls.append(repo_id)
        if repo_id == REPO:
            raise refusal
        return {"files": 1, "skipped": 0, "bytes": 100}

    @contextmanager
    def mutation_writer(*_args, **_kwargs):
        yield SimpleNamespace(child_fence_fds=(), record_touched=lambda *_a, **_k: None)

    con = f.mem_con()
    with mock.patch.object(fetch, "fetch_model", side_effect=fake_fetch_model), \
            mock.patch.object(fetch.drive_mutation, "drive_mutation", mutation_writer):
        out = fetch.run(
            dest=tmp_path, drive_label="d0", repos=[REPO, other],
            max_24h_gb=0, ctx=fetch.RunCtx(con=con),
        )

    assert calls == [REPO, other], "one unprovable repo must not stop unrelated fetch work"
    assert out["stored_repos"] == [other]
    assert out.get("terminal_failure") is None
    assert out.get("requirement_refusals") == [{
        "repo_id": REPO,
        "code": code,
        "message": "approved target content requires operator disposition",
        "evidence": {"repo_id": REPO, "rfilename": FILE, "drive_label": "d0"},
        "actions": list(actions),
        "gate": "C",
    }]


@pytest.mark.parametrize("code", [
    "APPROVED_TARGET_DIGEST_MISMATCH",
    "APPROVED_TARGET_DIGEST_UNPROVABLE",
])
def test_c06c_fill_parks_conflicted_requirement_after_other_work_completes(code):
    other = "org/continues"
    con = f.mem_con()
    f.seed_plan_selection(con, repos=(REPO, other))
    for repo in (REPO, other):
        con.execute(
            "UPDATE files SET rfilename=?,size_bytes=100,format='safetensors',"
            "quant='bf16',sha256=? WHERE repo_id=?",
            [FILE, APPROVED, repo],
        )
    projection = SimpleNamespace(tasks=(
        _projection(repo=REPO, rid=RID).tasks[0],
        _projection(repo=other, rid=f"primary:{other}").tasks[0],
    ))
    pfiles = _proposal_files() + _proposal_files(repo=other, rid=f"primary:{other}")
    refusal = {
        "repo_id": REPO,
        "code": code,
        "message": "approved target content requires operator disposition",
        "evidence": {"repo_id": REPO, "rfilename": FILE, "drive_label": "d0"},
        "actions": (["run_repair_drive", "resume_same_approval"]
                    if code.endswith("UNPROVABLE")
                    else ["inspect_target", "disposition_required"]),
        "gate": "C",
    }
    calls = []

    def fake_run(**_kwargs):
        calls.append(tuple(_kwargs["repos"]))
        if len(calls) > 1:
            raise AssertionError("conflicted requirement was not parked")
        # The unrelated repository really did become durably satisfied; do not
        # let stored_repos stand in for the post-run derivation pinned by c08.
        _seed_archived(con, repo=other, orig=APPROVED)
        out = _fetch_outcome(stored=(other,))
        out["requirement_refusals"] = [refusal]
        return out

    with mock.patch.object(fill_mod, "_mounted", return_value=(True, True)), \
            mock.patch.object(fill_mod.fetch, "run", side_effect=fake_run), \
            mock.patch.object(fill_mod, "_refresh_projection", return_value=projection), \
            mock.patch("modelark.execution_session.heartbeat"):
        result = fill_mod._drain_projection(
            fetch.RunCtx(con=con), _session_start(projection, pfiles),
            plan_id="ark", max_24h_gb=0, repo_scope=None,
            guided=False, poll_secs=0.01, child_fds=(),
        )

    assert len(calls) == 1
    assert result.get("state") == "done", result
    assert result.get("code") == "PLAN_COMPLETE_WITH_FOLLOWUPS", result
    assert result.get("evidence") == {"content_refusals": [refusal]}, result
    assert result.get("code") not in {"FETCH_TASK_FAILED", "PLAN_SATISFIED"}


def test_c06d_gated_and_content_followups_coexist_without_evidence_loss():
    other = "org/continues"
    gated = "org/gated"
    con = f.mem_con()
    f.seed_plan_selection(con, repos=(REPO, other, gated))
    for repo in (REPO, other, gated):
        con.execute(
            "UPDATE files SET rfilename=?,size_bytes=100,format='safetensors',"
            "quant='bf16',sha256=? WHERE repo_id=?",
            [FILE, APPROVED, repo],
        )
    projection = SimpleNamespace(tasks=(
        _projection(repo=REPO, rid=RID).tasks[0],
        _projection(repo=other, rid=f"primary:{other}").tasks[0],
        _projection(repo=gated, rid=f"primary:{gated}").tasks[0],
    ))
    pfiles = (
        _proposal_files()
        + _proposal_files(repo=other, rid=f"primary:{other}")
        + _proposal_files(repo=gated, rid=f"primary:{gated}")
    )
    refusal = {
        "repo_id": REPO,
        "code": "APPROVED_TARGET_DIGEST_UNPROVABLE",
        "message": "approved target content requires operator disposition",
        "evidence": {"repo_id": REPO, "rfilename": FILE, "drive_label": "d0"},
        "actions": ["run_repair_drive", "resume_same_approval"],
        "gate": "C",
    }
    calls = []

    def fake_run(**_kwargs):
        calls.append(tuple(_kwargs["repos"]))
        if len(calls) > 1:
            raise AssertionError("one follow-up category was not parked")
        _seed_archived(con, repo=other, orig=APPROVED)
        out = _fetch_outcome(stored=(other,))
        out["gated_repos"] = [{"repo": gated, "resolution": "skip"}]
        out["requirement_refusals"] = [refusal]
        return out

    with mock.patch.object(fill_mod, "_mounted", return_value=(True, True)), \
            mock.patch.object(fill_mod.fetch, "run", side_effect=fake_run), \
            mock.patch.object(fill_mod, "_refresh_projection", return_value=projection), \
            mock.patch("modelark.execution_session.heartbeat"):
        result = fill_mod._drain_projection(
            fetch.RunCtx(con=con), _session_start(projection, pfiles),
            plan_id="ark", max_24h_gb=0, repo_scope=None,
            guided=False, poll_secs=0.01, child_fds=(),
        )

    assert len(calls) == 1
    assert result.get("state") == "done", result
    assert result.get("code") == "PLAN_COMPLETE_WITH_FOLLOWUPS", result
    assert result.get("evidence") == {
        "access_gated": [gated],
        "content_refusals": [refusal],
    }, result


def test_c07_explicit_fetch_matching_presence_calls_shared_rule_and_skips(tmp_path):
    con = f.mem_con()
    _seed_repo(con)
    _seed_archived(con, orig=APPROVED)
    trace = mock.Mock(return_value=True)

    with mock.patch.object(archive_hash, "content_satisfies", trace, create=True), \
            mock.patch.object(fetch, "_download_shard") as download:
        out = fetch.fetch_model(
            fetch.RunCtx(con=con), REPO, tmp_path, "d0", False,
            {"max_compress_ram_gb": 4.0, "threads": 1},
            manifest=_manifest(),
        )

    assert trace.call_count == 1, "safe resume skip must be content-derived"
    assert download.call_count == 0
    assert out["files"] == 0 and out["skipped"] == 1, out


def test_c08_stored_repo_without_durable_change_cannot_complete_and_is_bounded():
    con = f.mem_con()
    f.seed_plan_selection(con, repos=(REPO,))
    con.execute(
        "UPDATE files SET rfilename=?,size_bytes=100,format='safetensors',"
        "quant='bf16',sha256=? WHERE repo_id=?",
        [FILE, APPROVED, REPO],
    )
    # Present but content-unsatisfied: both initial derivation and post-run
    # completion must consult the shared predicate.  A wholly absent row would
    # legitimately fail before a predicate call and would not pin the wiring.
    _seed_archived(con, orig=OTHER)
    projection = _projection()
    pfiles = _proposal_files()
    trace = mock.Mock(return_value=False)
    events: list[str] = []

    def fake_run(**_kwargs):
        events.append("fetch.run")
        return _fetch_outcome(stored=(REPO,))

    def traced_content(**kwargs):
        events.append("content_satisfies")
        return trace(**kwargs)

    with mock.patch.object(archive_hash, "content_satisfies", traced_content, create=True), \
            mock.patch.object(fill_mod, "_mounted", return_value=(True, True)), \
            mock.patch.object(fill_mod.fetch, "run", side_effect=fake_run):
        result = fill_mod._drain_projection(
            fetch.RunCtx(con=con), _session_start(projection, pfiles),
            plan_id="ark", max_24h_gb=0, repo_scope=None,
            guided=False, poll_secs=0.01, child_fds=(),
        )

    assert events.count("fetch.run") == fill_mod._MAX_TASK_ATTEMPTS, events
    assert "content_satisfies" in events, "completion must re-derive durable satisfaction"
    fetch_positions = [i for i, event in enumerate(events) if event == "fetch.run"]
    for n, position in enumerate(fetch_positions):
        end = fetch_positions[n + 1] if n + 1 < len(fetch_positions) else len(events)
        assert "content_satisfies" in events[position + 1:end], events
    assert result.get("code") == "FETCH_TASK_FAILED", result
    assert result.get("code") != "PLAN_SATISFIED", result


# ---------------------------------------------------------------------------
# Replica: per-requirement outcomes, heal wiring, mismatch halt, bounded drain.
# ---------------------------------------------------------------------------


@contextmanager
def _fake_mutation_writer():
    yield SimpleNamespace(child_fence_fds=(), record_touched=lambda *_a, **_k: None)


def _replica_task(*, repo=REPO, rid=f"replica:{REPO}"):
    return SimpleNamespace(
        task_id=rid,
        requirement_id=rid,
        repo_id=repo,
        source_drive="d0",
        target_drive="d1",
        budget=SimpleNamespace(missing_files=(FILE,)),
    )


def _run_replica_with_present_target(tmp_path, *, source_digest=APPROVED,
                                     target_digest=None, target_provenance=None,
                                     target_annex_key=None):
    con = f.mem_con()
    f.seed_plan_selection(con, repos=(REPO,))
    con.execute(
        "UPDATE files SET rfilename=?,size_bytes=100,sha256=? WHERE repo_id=?",
        [FILE, APPROVED, REPO],
    )
    con.execute("UPDATE drives SET annex_uuid='uuid-d1' WHERE drive_label='d1'")
    _seed_archived(
        con, drive="d0", orig=source_digest, annex_key=f"SHA256E-s100--{source_digest}.bin",
        provenance="hub_confirmed",
    )
    _seed_archived(
        con, drive="d1", orig=target_digest, annex_key=target_annex_key,
        provenance=target_provenance,
    )
    target_before = con.execute(
        "SELECT * FROM archived WHERE repo_id=? AND rfilename=? AND drive_label='d1'",
        [REPO, FILE],
    ).fetchone()
    source = tmp_path / "source"
    target = tmp_path / "target"
    library = tmp_path / "library"
    source.mkdir()
    target.mkdir()
    library.mkdir()
    paths = {"d0": source, "d1": target}
    real_heal = fetch.heal_replica_archived_from_source
    heal_spy = mock.Mock(wraps=real_heal)
    content_spy = mock.Mock(wraps=archive_hash.content_satisfies)
    completed = mock.Mock(returncode=0, stdout="", stderr="")

    patches = (
        mock.patch.object(fetch.register, "archive_path", side_effect=lambda _c, lab: paths[lab]),
        mock.patch.object(fetch.register, "library_root", return_value=library),
        mock.patch.object(fetch.drive_mutation, "drive_mutation", return_value=_fake_mutation_writer()),
        mock.patch.object(fetch, "_dest_writable", return_value=True),
        mock.patch.object(fetch.subprocess, "run", return_value=completed),
        mock.patch.object(fetch, "heal_replica_archived_from_source", heal_spy),
        mock.patch.object(archive_hash, "content_satisfies", content_spy, create=True),
    )
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
        out = fetch.run_replica_tasks([_replica_task()], ctx=fetch.RunCtx(con=con))
    return con, out, heal_spy, content_spy, completed, target_before


def _annex_copy_calls(proc):
    return [
        call for call in proc.call_args_list
        if "annex" in call.args[0] and "copy" in call.args[0]
    ]


def test_c09_replica_null_target_wires_heal_and_reports_exact_completion(tmp_path):
    con, out, heal, content, proc, _before = _run_replica_with_present_target(
        tmp_path, target_digest=None,
    )
    assert content.call_count >= 1, "replica presence decision must call shared predicate"
    assert heal.call_count == 1, "unproven target must enter DEC-060 heal"
    row = con.execute(
        "SELECT orig_sha256,orig_sha256_provenance FROM archived "
        "WHERE repo_id=? AND rfilename=? AND drive_label='d1'",
        [REPO, FILE],
    ).fetchone()
    assert row == (APPROVED, "hub_confirmed"), row
    assert out.get("completed_requirements") == [f"replica:{REPO}"], out
    assert _annex_copy_calls(proc) == [], "metadata heal must not copy bytes"


def test_c10_replica_digest_mismatch_halts_before_copy_or_upsert(tmp_path):
    con, out, heal, content, proc, before = _run_replica_with_present_target(
        tmp_path, target_digest=OTHER, target_provenance="legacy_unknown",
    )
    assert content.call_count >= 1
    assert heal.call_count == 1, "mismatch must be classified by DEC-060 heal"
    after = con.execute(
        "SELECT * FROM archived WHERE repo_id=? AND rfilename=? AND drive_label='d1'",
        [REPO, FILE],
    ).fetchone()
    assert after == before
    assert _annex_copy_calls(proc) == []
    assert out.get("completed_requirements") == [], out
    assert con.in_transaction is False, "mismatch refusal must roll back and release its lock"
    failures = out.get("failed") or []
    assert any(item.get("requirement_id") == f"replica:{REPO}"
               and "MISMATCH" in str(item.get("code", "")) for item in failures), failures


def test_c11_replica_outcomes_are_per_requirement_even_when_batch_progresses():
    con = f.mem_con()
    outcome = fetch.run_replica_tasks([], ctx=fetch.RunCtx(con=con))
    assert set(outcome) >= {
        "completed_requirements", "progressed_requirements", "failed",
    }, outcome
    assert outcome["completed_requirements"] == []
    assert outcome["progressed_requirements"] == []


def test_c12_replica_aggregate_progress_cannot_blanket_complete_and_is_bounded():
    con = f.mem_con()
    f.seed_plan_selection(con, repos=(REPO,))
    con.execute(
        "UPDATE files SET rfilename=?,size_bytes=100,sha256=? WHERE repo_id=?",
        [FILE, APPROVED, REPO],
    )
    _seed_archived(con, drive="d0", orig=APPROVED)
    rid = f"replica:{REPO}"
    projection = _projection(source="d0", rid=rid, target="d1")
    pfiles = _proposal_files(rid=rid)
    trace = mock.Mock(return_value=True)  # source ready; target remains wholly absent
    events = []

    def fake_replica(_tasks, ctx=None):
        events.append("replica")
        return {
            "deferred": False,
            "source_offline": False,
            "deferred_targets": [],
            "copied_targets": ["d1"],
            "copied_files": 1,
            "completed_requirements": [],
            "progressed_requirements": [],
            "failed": [],
        }

    def traced_content(**kwargs):
        events.append("content_satisfies")
        return trace(**kwargs)

    with mock.patch.object(archive_hash, "content_satisfies", traced_content, create=True), \
            mock.patch.object(fill_mod, "_mounted", return_value=(True, True)), \
            mock.patch.object(fill_mod.fetch, "run_replica_tasks", side_effect=fake_replica):
        result = fill_mod._drain_projection(
            fetch.RunCtx(con=con), _session_start(projection, pfiles),
            plan_id="ark", max_24h_gb=0, repo_scope=None,
            guided=False, poll_secs=0.01, child_fds=(),
        )

    assert events.count("replica") == fill_mod._MAX_TASK_ATTEMPTS, events
    assert trace.call_count >= 1, "replica completion must re-derive content satisfaction"
    last_replica = max(i for i, event in enumerate(events) if event == "replica")
    assert any(event == "content_satisfies" for event in events[last_replica + 1:]), events
    assert result.get("code") == "REPLICA_TASK_FAILED", result
    assert result.get("code") != "PLAN_SATISFIED", result


class _HealRaceConnection:
    def __init__(self, con, writer):
        self._con = con
        self.writer = writer
        self.statements = []
        self.injected = False
        self.writer_blocked = False

    def execute(self, sql, params=()):
        self.statements.append(sql)
        cursor = self._con.execute(sql, params)
        normalized = " ".join(sql.upper().split())
        if normalized.startswith("BEGIN IMMEDIATE"):
            self.injected = True
            try:
                self.writer.execute(
                    "UPDATE archived SET orig_sha256=?,orig_sha256_provenance=? "
                    "WHERE drive_label='d1' AND repo_id=? AND rfilename=?",
                    [OTHER, "legacy_unknown", REPO, FILE],
                )
            except sqlite3.OperationalError as exc:
                assert "locked" in str(exc).lower(), exc
                self.writer_blocked = True
        return cursor

    def __getattr__(self, name):
        return getattr(self._con, name)


def test_c13_replica_heal_serializes_and_compare_swaps_null_target(tmp_path):
    seed = f.mem_con()
    _seed_repo(seed)
    _seed_archived(
        seed, drive="d0", orig=APPROVED, provenance="hub_confirmed",
    )
    _seed_archived(seed, drive="d1", orig=None, provenance=None)
    db_path = tmp_path / "heal-race.sqlite"
    con = sqlite3.connect(db_path, isolation_level=None, timeout=0.05)
    seed.backup(con)
    writer = sqlite3.connect(db_path, isolation_level=None, timeout=0.0)
    race = _HealRaceConnection(con, writer)
    committed = None
    transaction_open = None
    try:
        out = fetch.heal_replica_archived_from_source(
            race,
            source_drive="d0", target_drive="d1",
            repo_id=REPO, rfilename=FILE,
        )
        row = con.execute(
            "SELECT orig_sha256,orig_sha256_provenance FROM archived "
            "WHERE drive_label='d1' AND repo_id=? AND rfilename=?",
            [REPO, FILE],
        ).fetchone()
        transaction_open = con.in_transaction
        observer = sqlite3.connect(db_path, isolation_level=None, timeout=0.05)
        try:
            committed = observer.execute(
                "SELECT orig_sha256,orig_sha256_provenance FROM archived "
                "WHERE drive_label='d1' AND repo_id=? AND rfilename=?",
                [REPO, FILE],
            ).fetchone()
        finally:
            observer.close()
    finally:
        if con.in_transaction:
            con.rollback()
        writer.close()
        con.close()

    normalized = [" ".join(sql.upper().split()) for sql in race.statements]
    begin = next((i for i, sql in enumerate(normalized)
                  if sql.startswith("BEGIN IMMEDIATE")), None)
    assert begin is not None, normalized
    assert race.injected, "race harness must fail loudly if injection stops executing"
    assert race.writer_blocked, "BEGIN IMMEDIATE must exclude the injected writer"
    assert transaction_open is False, "successful heal must commit and release its write lock"
    guarded_updates = [
        sql for sql in normalized
        if sql.startswith("UPDATE ARCHIVED SET ORIG_SHA256=")
    ]
    assert guarded_updates and all("ORIG_SHA256 IS NULL" in sql for sql in guarded_updates), \
        guarded_updates
    assert out == {
        "status": "filled", "orig_sha256": APPROVED,
        "orig_sha256_provenance": "hub_confirmed",
    }
    assert row == (APPROVED, "hub_confirmed")
    assert committed == row, "heal must be visible from a fresh connection before it returns"


def test_c14_replica_divergent_raw_annex_target_halts_without_stamp(tmp_path):
    con = f.mem_con()
    f.seed_plan_selection(con, repos=(REPO,))
    con.execute(
        "UPDATE files SET rfilename=?,size_bytes=100,sha256=? WHERE repo_id=?",
        [FILE, APPROVED, REPO],
    )
    con.execute("UPDATE drives SET annex_uuid='uuid-d1' WHERE drive_label='d1'")
    _seed_archived(
        con, drive="d0", orig=APPROVED,
        annex_key=f"SHA256E-s100--{APPROVED}.bin", provenance="hub_confirmed",
    )
    _seed_archived(
        con, drive="d1", orig=None,
        annex_key=f"SHA256E-s100--{OTHER}.bin", provenance=None,
    )
    before = con.execute(
        "SELECT * FROM archived WHERE repo_id=? AND rfilename=? AND drive_label='d1'",
        [REPO, FILE],
    ).fetchone()
    source = tmp_path / "source"
    target = tmp_path / "target"
    library = tmp_path / "library"
    source.mkdir()
    target.mkdir()
    library.mkdir()
    paths = {"d0": source, "d1": target}
    content_spy = mock.Mock(wraps=archive_hash.content_satisfies)
    heal_spy = mock.Mock(wraps=fetch.heal_replica_archived_from_source)
    completed = mock.Mock(returncode=0, stdout="", stderr="")

    with mock.patch.object(fetch.register, "archive_path",
                           side_effect=lambda _c, label: paths[label]), \
            mock.patch.object(fetch.register, "library_root", return_value=library), \
            mock.patch.object(fetch.drive_mutation, "drive_mutation",
                              return_value=_fake_mutation_writer()), \
            mock.patch.object(fetch, "_dest_writable", return_value=True), \
            mock.patch.object(fetch.subprocess, "run", return_value=completed), \
            mock.patch.object(fetch, "heal_replica_archived_from_source", heal_spy), \
            mock.patch.object(archive_hash, "content_satisfies", content_spy):
        out = fetch.run_replica_tasks([_replica_task()], ctx=fetch.RunCtx(con=con))

    after = con.execute(
        "SELECT * FROM archived WHERE repo_id=? AND rfilename=? AND drive_label='d1'",
        [REPO, FILE],
    ).fetchone()
    assert content_spy.call_count >= 1
    assert heal_spy.call_count == 1
    assert after == before, "annex-derived contradiction must halt without metadata stamping"
    assert con.in_transaction is False
    assert out.get("completed_requirements") == [], out
    assert _annex_copy_calls(completed) == []
    failures = out.get("failed") or []
    assert any(item.get("requirement_id") == f"replica:{REPO}"
               and "MISMATCH" in str(item.get("code", "")) for item in failures), failures


def test_c15_numcopies_two_refusal_survives_waiting_replica_dependency():
    con = f.mem_con()
    f.seed_plan_selection(con, repos=(REPO,))
    con.execute("UPDATE models SET numcopies=2 WHERE repo_id=?", [REPO])
    con.execute(
        "UPDATE files SET rfilename=?,size_bytes=100,format='safetensors',"
        "quant='bf16',sha256=? WHERE repo_id=?",
        [FILE, APPROVED, REPO],
    )
    replica_rid = f"replica:{REPO}"
    projection = SimpleNamespace(tasks=(
        _projection(rid=RID, target="d0").tasks[0],
        _projection(rid=replica_rid, source="d0", target="d1").tasks[0],
    ))
    pfiles = _proposal_files(rid=RID) + _proposal_files(rid=replica_rid)
    refusal = {
        "repo_id": REPO,
        "code": "APPROVED_TARGET_DIGEST_UNPROVABLE",
        "message": "approved target content requires operator disposition",
        "evidence": {"repo_id": REPO, "rfilename": FILE, "drive_label": "d0"},
        "actions": ["run_repair_drive", "resume_same_approval"],
        "gate": "C",
    }
    calls = []

    def fake_run(**kwargs):
        calls.append(tuple(kwargs["repos"]))
        if len(calls) > 1:
            raise AssertionError("refused primary was not parked")
        out = _fetch_outcome()
        out["requirement_refusals"] = [refusal]
        return out

    with mock.patch.object(fill_mod, "_mounted", return_value=(True, True)), \
            mock.patch.object(fill_mod.fetch, "run", side_effect=fake_run), \
            mock.patch.object(fill_mod, "_refresh_projection", return_value=projection), \
            mock.patch("modelark.execution_session.heartbeat"):
        result = fill_mod._drain_projection(
            fetch.RunCtx(con=con), _session_start(projection, pfiles),
            plan_id="ark", max_24h_gb=0, repo_scope=None,
            guided=False, poll_secs=0.01, child_fds=(),
        )

    assert calls == [(REPO,)]
    assert result.get("state") == "done", result
    assert result.get("code") == "PLAN_COMPLETE_WITH_FOLLOWUPS", result
    assert result.get("evidence") == {"content_refusals": [refusal]}, result
    assert result.get("code") != "WAITING_DEPENDENCY"


def test_c16_matching_raw_annex_target_fills_digest_and_provenance_together(tmp_path):
    key = f"SHA256E-s100--{APPROVED}.bin"
    con, out, heal, _content, proc, _before = _run_replica_with_present_target(
        tmp_path,
        target_digest=None,
        target_provenance=None,
        target_annex_key=key,
    )

    row = con.execute(
        "SELECT orig_sha256,orig_sha256_provenance,annex_key FROM archived "
        "WHERE repo_id=? AND rfilename=? AND drive_label='d1'",
        [REPO, FILE],
    ).fetchone()
    assert heal.call_count == 1
    assert row == (APPROVED, "hub_confirmed", key), (
        "matching annex evidence may enrich the digest column, but provenance must never be "
        "written while orig_sha256 remains NULL",
        row,
    )
    assert out.get("completed_requirements") == [f"replica:{REPO}"], out
    assert _annex_copy_calls(proc) == [], "safe metadata enrichment must not copy bytes"


def test_c17_replica_heal_classifier_matrix_names_fill_kind():
    key_a = f"SHA256E-s100--{APPROVED}.bin"
    key_b = f"SHA256E-s100--{OTHER}.bin"
    cases = [
        # Matching key plus a source column digest safely enriches both target fields.
        (APPROVED, key_a, "hub_confirmed", None, key_a, None, "fill", "digest"),
        # A populated matching target digest may receive provenance alone.
        (APPROVED, key_a, "hub_confirmed", APPROVED, key_a, None,
         "fill", "provenance"),
        # Complete matching evidence needs no mutation.
        (APPROVED, key_a, "hub_confirmed", APPROVED, key_a, "hub_confirmed",
         "satisfied", None),
        # Key-only source evidence cannot certify otherwise-unproven target bytes.
        (None, key_a, None, None, None, None, "noop", None),
        # Independently resolvable disagreement always halts.
        (APPROVED, key_a, "hub_confirmed", None, key_b, None,
         "halt_contradiction", None),
        # No target evidence can be filled from a real source column digest.
        (APPROVED, key_a, "hub_confirmed", None, None, None, "fill", "digest"),
    ]
    observed = []
    for (source_orig, source_key, source_prov, target_orig, target_key, target_prov,
         _action, _fill_kind) in cases:
        decision = fetch._classify_replica_heal(
            source_orig_sha256=source_orig,
            source_compressed=False,
            source_annex_key=source_key,
            source_provenance=source_prov,
            target_orig_sha256=target_orig,
            target_compressed=False,
            target_annex_key=target_key,
            target_provenance=target_prov,
        )
        observed.append((decision.action, getattr(decision, "fill_kind", None)))

    assert observed == [(case[-2], case[-1]) for case in cases], observed
