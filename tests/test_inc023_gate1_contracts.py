"""INC-023 Gate-1 contracts — multi-file projection without fabricated defaults.

Expected-red until production remediates execution_projection.project_pure to:
  - group every frozen proposal.files row by requirement_id;
  - evaluate every approved file (no last-row overwrite);
  - remove fabricated model.safetensors / \"1\"*64 / 100-byte durable defaults;
  - route digests through archive_hash.expected_sha256 (DEC-055 parity with Fill);
  - require _catalog_projection_bundle to capture compressed + annex_key;
  - requirement-level stored-overrun before shrink;
  - refuse missing/null guaranteed_durable; retain explicit zero.

Baseline certificate self-copy is INC-027 (out of these contracts).
No production in this gate.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

import _pr09_gate1_fixtures as f
from modelark import archive_hash, fill as fill_mod
from modelark.execution_session import _catalog_projection_bundle
from modelark.proposal import Refusal


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MISSING = object()


def _proj():
    return f.project_pure_fn()


def _task(
    rid="primary:org/m",
    *,
    repo="org/m",
    target="d0",
    source=None,
    row_kind="executable",
    order_key=1,
    guaranteed_durable=1000,
    full_manifest_hash=None,
    identity_epoch=1,
):
    return {
        "requirement_id": rid,
        "row_kind": row_kind,
        "repo_id": repo,
        "target_drive": target,
        "source_drive": source,
        "satisfying_drive": target if row_kind == "baseline_satisfied" else None,
        "full_manifest_hash": full_manifest_hash or ("a" * 64),
        "order_key": order_key,
        "guaranteed_durable": guaranteed_durable,
        "expected_durable": guaranteed_durable,
        "identity_epoch": identity_epoch,
        "baseline_certificate": None,
    }


def _file(rid, rfilename, *, size=100, sha=_MISSING, role="missing"):
    # Preserve explicit None (null approved digest); default only when omitted.
    return {
        "requirement_id": rid,
        "rfilename": rfilename,
        "role": role,
        "size_bytes": size,
        "orig_sha256": ("b" * 64) if sha is _MISSING else sha,
        "format": "safetensors",
        "quant": "bf16",
        "storage_action": "compress",
    }


def _approved_proposal(*, tasks, files, **extra):
    p = {
        "lifecycle": "approved",
        "proposal_id": "inc023-g1",
        "plan_id": "ark",
        "tasks": list(tasks),
        "files": list(files),
        "requirement_set_hash": "r" * 64,
        "semantic_input_hash": "s" * 64,
        "capacity_mode": "guaranteed",
        "policy_version": "1",
        "solver_version": "1",
    }
    p.update(extra)
    return p


def _arch(repo, rfilename, drive, *, orig_sha256=None, stored_bytes=100,
          compressed=0, annex_key=None):
    return {
        "orig_sha256": orig_sha256,
        "stored_bytes": stored_bytes,
        "orig_bytes": stored_bytes,
        "compressed": compressed,
        "annex_key": annex_key,
    }


def _drive(label, *, epoch=1, fingerprint=None):
    return SimpleNamespace(
        lifecycle="active",
        eligibility="enabled",
        identity_epoch=epoch,
        identity_fingerprint=fingerprint or (label.encode().hex().ljust(64, "0")[:64]),
        offline=False,
    )


def _inputs(proposal, *, archived=None, drives=None, manifests=None):
    return f.complete_projection_inputs(
        proposal, archived=archived, drives=drives, manifests=manifests)


def _task_ids(out):
    tasks = list(f.get_field(out, "tasks") or ())
    ids = []
    for t in tasks:
        if isinstance(t, dict):
            ids.append(t.get("requirement_id"))
        else:
            ids.append(getattr(t, "requirement_id", None) or f.get_field(t, "requirement_id"))
    return ids


def _refusal_evidence(exc_or_out):
    if isinstance(exc_or_out, Refusal):
        return exc_or_out.evidence or {}
    if isinstance(exc_or_out, dict):
        return exc_or_out.get("evidence") or {}
    ev = getattr(exc_or_out, "evidence", None)
    return ev if isinstance(ev, dict) else {}


# ===========================================================================
# Multi-file grouping & shrink
# ===========================================================================


def test_c01_groups_all_proposal_files_by_requirement_id():
    """Every frozen proposal.files row for a requirement is evaluated (order-independent).

    This ordering (first present, last missing) currently PASSES under last-file
    evaluation because the last name is absent. c02 is the reverse order that fails.
    """
    _mod, project_pure = _proj()
    rid = "primary:org/m"
    # Only the first file is archived; last (model.safetensors) is missing.
    tasks = [_task(rid, guaranteed_durable=200)]
    files = [
        _file(rid, "config.json", size=10, sha="c" * 64),
        _file(rid, "model.safetensors", size=190, sha="m" * 64),
    ]
    proposal = _approved_proposal(tasks=tasks, files=files)
    archived = {
        ("org/m", "config.json", "d0"): _arch(
            "org/m", "config.json", "d0", orig_sha256="c" * 64, stored_bytes=10),
        # model.safetensors absent
    }
    inp, graph = _inputs(proposal, archived=archived)
    out = project_pure(proposal, inp, graph, f.EMPTY_OVERLAY)
    if f.is_refusal(out):
        raise AssertionError(
            f"c01: partial multi-file must not refuse, got {f.refusal_code(out)}: {out!r}")
    ids = _task_ids(out)
    assert rid in ids, (
        f"c01: partial satisfaction must retain the requirement; remaining={ids}")


def test_c02_partial_never_shrinks_regardless_of_file_order():
    """Expected-red: only the last name present must not shrink (order-independent)."""
    _mod, project_pure = _proj()
    rid = "primary:org/m"
    tasks = [_task(rid, guaranteed_durable=200)]
    files = [
        _file(rid, "model.safetensors", size=190, sha="m" * 64),
        _file(rid, "config.json", size=10, sha="c" * 64),
    ]
    proposal = _approved_proposal(tasks=tasks, files=files)
    archived = {
        ("org/m", "config.json", "d0"): _arch(
            "org/m", "config.json", "d0", orig_sha256="c" * 64, stored_bytes=10),
    }
    inp, graph = _inputs(proposal, archived=archived)
    out = project_pure(proposal, inp, graph, f.EMPTY_OVERLAY)
    if f.is_refusal(out):
        raise AssertionError(f"c02: unexpected refuse {f.refusal_code(out)}: {out!r}")
    assert rid in _task_ids(out), (
        f"c02: partial (only second/last file present) must not shrink; got {_task_ids(out)}")


def test_c03_all_approved_files_satisfied_shrinks():
    """Positive compatibility: all approved files content-ok → requirement shrinks out."""
    _mod, project_pure = _proj()
    rid = "primary:org/m"
    tasks = [_task(rid, guaranteed_durable=200)]
    files = [
        _file(rid, "config.json", size=10, sha="c" * 64),
        _file(rid, "model.safetensors", size=190, sha="m" * 64),
    ]
    proposal = _approved_proposal(tasks=tasks, files=files)
    archived = {
        ("org/m", "config.json", "d0"): _arch(
            "org/m", "config.json", "d0", orig_sha256="c" * 64, stored_bytes=10),
        ("org/m", "model.safetensors", "d0"): _arch(
            "org/m", "model.safetensors", "d0", orig_sha256="m" * 64, stored_bytes=190),
    }
    inp, graph = _inputs(proposal, archived=archived)
    out = f.require_success(
        project_pure(proposal, inp, graph, f.EMPTY_OVERLAY), label="c03 all satisfied")
    assert rid not in _task_ids(out), (
        f"c03: fully satisfied multi-file requirement must shrink; remaining={_task_ids(out)}")


# ===========================================================================
# Positive compatibility: no live catalog satisfaction authority / DEC-056 drift
# ===========================================================================


def test_c04_no_live_manifest_for_repo_or_raw_files_for_satisfaction():
    """Positive compatibility: satisfaction must not call live catalog helpers.

    DEC-056 _manifest_hash drift recomputation remains allowed elsewhere; this pin
    only forbids live catalog file authority as *satisfaction* input.
    """
    _mod, project_pure = _proj()
    rid = "primary:org/m"
    proposal = _approved_proposal(
        tasks=[_task(rid, guaranteed_durable=100)],
        files=[_file(rid, "model.safetensors", size=100, sha="m" * 64)],
    )
    archived = {
        ("org/m", "model.safetensors", "d0"): _arch(
            "org/m", "model.safetensors", "d0", orig_sha256="m" * 64),
    }
    inp, graph = _inputs(proposal, archived=archived)
    with mock.patch("modelark.archive_manifest.manifest_for_repo") as mfr, \
            mock.patch("modelark.archive_manifest.inspect_manifests_for_repos") as ins:
        out = project_pure(proposal, inp, graph, f.EMPTY_OVERLAY)
        mfr.assert_not_called()
        ins.assert_not_called()
    f.require_success(out, label="c04 no live manifest")


def test_c05_dec056_manifest_hash_drift_still_refuses():
    """Positive compatibility pin: DEC-056 full_manifest_hash drift still refuses."""
    _mod, project_pure = _proj()
    rid = "primary:org/m"
    proposal = _approved_proposal(
        tasks=[_task(rid, guaranteed_durable=100, full_manifest_hash="a" * 64)],
        files=[_file(rid, "model.safetensors", size=100, sha="m" * 64)],
    )
    inp, graph = _inputs(
        proposal, archived={},
        manifests={"org/m": "f" * 64},
    )
    f.assert_refuses(
        lambda: project_pure(proposal, inp, graph, f.EMPTY_OVERLAY),
        code="APPROVED_INPUT_CHANGED",
        label="c05 DEC-056 drift",
    )


# ===========================================================================
# Expected-red: empty / missing proposal file authority (Fill parity)
# ===========================================================================


def test_c06_missing_file_group_is_approved_input_changed():
    """Expected-red: executable task with no proposal.files group."""
    _mod, project_pure = _proj()
    rid = "primary:org/m"
    proposal = _approved_proposal(
        tasks=[_task(rid, guaranteed_durable=100)],
        files=[],  # no rows for rid
    )
    inp, graph = _inputs(proposal, archived={})
    out = project_pure(proposal, inp, graph, f.EMPTY_OVERLAY)
    assert f.is_refusal(out) and f.refusal_code(out) == "APPROVED_INPUT_CHANGED", (
        f"c06: expected APPROVED_INPUT_CHANGED, got {out!r}")
    ev = _refusal_evidence(out)
    assert ev.get("reason") == "missing_proposal_file_authority", (
        f"c06: reason must match Fill missing_proposal_file_authority, got {ev!r}")


def test_c07_rows_without_usable_filenames_is_empty_proposal_file_authority():
    """Expected-red: rows present but no usable rfilename (Fill parity)."""
    _mod, project_pure = _proj()
    rid = "primary:org/m"
    proposal = _approved_proposal(
        tasks=[_task(rid, guaranteed_durable=100)],
        files=[{
            "requirement_id": rid,
            "rfilename": None,
            "role": "missing",
            "size_bytes": 100,
            "orig_sha256": "m" * 64,
            "format": "safetensors",
            "quant": "bf16",
            "storage_action": "compress",
        }, {
            "requirement_id": rid,
            "rfilename": "",
            "role": "missing",
            "size_bytes": 50,
            "orig_sha256": "n" * 64,
            "format": "aux",
            "quant": None,
            "storage_action": "raw",
        }],
    )
    inp, graph = _inputs(proposal, archived={})
    out = project_pure(proposal, inp, graph, f.EMPTY_OVERLAY)
    assert f.is_refusal(out) and f.refusal_code(out) == "APPROVED_INPUT_CHANGED", (
        f"c07: expected APPROVED_INPUT_CHANGED, got {out!r}")
    ev = _refusal_evidence(out)
    assert ev.get("reason") == "empty_proposal_file_authority", (
        f"c07: reason must match Fill empty_proposal_file_authority, got {ev!r}")


# ===========================================================================
# Expected-red: catalog projection envelope
# ===========================================================================


def test_c08_catalog_projection_bundle_captures_compressed_and_annex_key():
    """Expected-red: _catalog_projection_bundle must include compressed + annex_key."""
    con = f.mem_con()
    f.seed_plan_selection(con, repos=("org/m",))
    con.execute(
        "INSERT OR REPLACE INTO archived("
        "repo_id,rfilename,drive_label,orig_sha256,stored_bytes,orig_bytes,"
        "compressed,annex_key) VALUES(?,?,?,?,?,?,?,?)",
        ["org/m", "model.safetensors", "d0", None, 100, 100, 0,
         "SHA256E-s100--" + ("a" * 64)],
    )
    proposal = _approved_proposal(
        tasks=[_task("primary:org/m", guaranteed_durable=100)],
        files=[_file("primary:org/m", "model.safetensors", size=100, sha=None)],
    )
    services = SimpleNamespace(
        observe_exact_capacity=lambda *a, **k: {
            "d0": SimpleNamespace(kind="offline", executable=True, admissible_free=10**12),
            "d1": SimpleNamespace(kind="offline", executable=True, admissible_free=10**12),
        },
    )
    current_input, _graph = _catalog_projection_bundle(
        con, proposal, ["d0", "d1"], services, {"capacity_mode": "guaranteed"})
    archived = getattr(current_input, "archived", None) or {}
    row = archived.get(("org/m", "model.safetensors", "d0"))
    assert isinstance(row, dict), f"c08: archived row missing, keys={list(archived)[:5]}"
    assert "compressed" in row, f"c08: compressed missing from envelope {row!r}"
    assert "annex_key" in row, f"c08: annex_key missing from envelope {row!r}"
    assert row.get("annex_key") and "SHA256" in str(row.get("annex_key"))


# ===========================================================================
# Expected-red: digest parity + production routing through expected_sha256
# ===========================================================================


def _parity_cases():
    """(label, approved_sha, arch_kwargs, expect_satisfied_by_fill)."""
    digest = "d" * 64
    other = "e" * 64
    raw_key = f"SHA256E-s100--{digest}"
    return [
        ("match", digest, {"orig_sha256": digest, "compressed": 0}, True),
        ("mismatch", digest, {"orig_sha256": other, "compressed": 0}, False),
        ("approved_null_resolved", digest, {"orig_sha256": None, "compressed": 0}, False),
        ("null_approved_with_digest", None, {"orig_sha256": digest, "compressed": 0}, True),
        ("raw_annex_key", None, {
            "orig_sha256": None, "compressed": 0, "annex_key": raw_key}, True),
        ("compressed_annex_no_orig", None, {
            "orig_sha256": None, "compressed": 1,
            "annex_key": f"SHA256E-s50--{digest}"}, False),
    ]


def test_c09_satisfaction_matches_fill_expected_sha256_matrix():
    """Expected-red: outcome parity with Fill and routing via expected_sha256."""
    import modelark.execution_projection as ep

    _mod, project_pure = _proj()
    rid = "primary:org/m"
    failures = []
    for label, approved_sha, arch_kw, fill_ok in _parity_cases():
        # Fill oracle first — before installing any spy.
        fill_sat = fill_mod._archive_content_satisfies(
            approved_sha,
            orig_sha256=arch_kw.get("orig_sha256"),
            compressed=bool(arch_kw.get("compressed")),
            annex_key=arch_kw.get("annex_key"),
        )
        assert fill_sat is fill_ok, f"oracle drift for {label}: fill={fill_sat} want={fill_ok}"

        tasks = [_task(rid, guaranteed_durable=100)]
        files = [_file(rid, "weights.bin", size=100, sha=approved_sha)]
        proposal = _approved_proposal(tasks=tasks, files=files)
        orig = arch_kw.get("orig_sha256")
        compressed = arch_kw.get("compressed", 0)
        annex_key = arch_kw.get("annex_key")
        archived = {
            ("org/m", "weights.bin", "d0"): _arch(
                "org/m", "weights.bin", "d0",
                orig_sha256=orig,
                stored_bytes=100,
                compressed=compressed,
                annex_key=annex_key,
            ),
        }
        inp, graph = _inputs(proposal, archived=archived)

        # Production must route through execution_projection.archive_hash.expected_sha256.
        # Bind the module name when absent so the spy target matches Gate-2 import style
        # (`from modelark import archive_hash` in execution_projection).
        bound = False
        if getattr(ep, "archive_hash", None) is None:
            ep.archive_hash = archive_hash
            bound = True
        try:
            with mock.patch(
                "modelark.execution_projection.archive_hash.expected_sha256",
                wraps=archive_hash.expected_sha256,
            ) as spy:
                out = project_pure(proposal, inp, graph, f.EMPTY_OVERLAY)
                try:
                    spy.assert_called_once_with(
                        catalog_sha=None,
                        orig_sha256=orig,
                        compressed=bool(compressed),
                        annex_key=annex_key,
                    )
                except AssertionError as exc:
                    failures.append((label, f"routing: {exc}", fill_ok))
                    continue
        finally:
            if bound and getattr(ep, "archive_hash", None) is archive_hash:
                delattr(ep, "archive_hash")

        if f.is_refusal(out) and f.refusal_code(out) not in (
                None, "APPROVED_PLACEMENT_NO_LONGER_FEASIBLE"):
            if f.refusal_code(out) in ("APPROVED_INPUT_CHANGED", "APPROVAL_PROJECTION_VIOLATION"):
                failures.append((label, f"refuse {f.refusal_code(out)}", fill_ok))
                continue
        remaining = rid in _task_ids(out) if not f.is_refusal(out) else True
        proj_satisfied = not remaining
        if proj_satisfied is not fill_ok:
            failures.append((label, f"proj_satisfied={proj_satisfied}", fill_ok))
    assert not failures, (
        "c09: projection must match Fill/expected_sha256 matrix and route through "
        f"archive_hash.expected_sha256; mismatches={failures!r}")


# ===========================================================================
# Expected-red: replica source readiness (every approved file)
# ===========================================================================


def test_c10_replica_source_evaluates_every_approved_file_waiting_dependency():
    """Expected-red: only LAST approved file on source → still waiting_dependency.

    Archives only the last approved name on the primary/source drive. Under every-file
    evaluation both primary and replica source are incomplete → waiting_dependency.
    Last-file production incorrectly treats primary/source as satisfied.
    """
    _mod, project_pure = _proj()
    primary = "primary:org/m"
    replica = "replica:org/m"
    tasks = [
        _task(primary, target="d0", source=None, order_key=1, guaranteed_durable=200),
        _task(replica, target="d1", source="d0", order_key=2, guaranteed_durable=200),
    ]
    files = [
        _file(primary, "a.bin", size=100, sha="a" * 64),
        _file(primary, "b.bin", size=100, sha="b" * 64),
        _file(replica, "a.bin", size=100, sha="a" * 64),
        _file(replica, "b.bin", size=100, sha="b" * 64),
    ]
    proposal = _approved_proposal(tasks=tasks, files=files)
    # Only the LAST approved file on the shared primary/source drive.
    archived = {
        ("org/m", "b.bin", "d0"): _arch(
            "org/m", "b.bin", "d0", orig_sha256="b" * 64, stored_bytes=100),
        # a.bin absent on d0
    }
    inp, graph = _inputs(proposal, archived=archived)
    out = project_pure(proposal, inp, graph, f.EMPTY_OVERLAY)
    if f.is_refusal(out):
        raise AssertionError(
            f"c10: incomplete source with unfinished primary must wait, not refuse; "
            f"got {f.refusal_code(out)}: {out!r}")
    tasks_out = list(f.get_field(out, "tasks") or ())
    by_id = {
        (t.get("requirement_id") if isinstance(t, dict) else f.get_field(t, "requirement_id")): t
        for t in tasks_out
    }
    assert primary in by_id, (
        f"c10: primary must remain under every-file evaluation; tasks={list(by_id)}")
    assert replica in by_id, f"c10: replica must remain; tasks={list(by_id)}"
    rep = by_id[replica]
    state = rep.get("schedule_state") if isinstance(rep, dict) else f.get_field(rep, "schedule_state")
    assert state == "waiting_dependency", (
        f"c10: expected waiting_dependency, got {state!r} "
        f"(last-file-only source readiness is wrong)")


def test_c11_replica_completed_primary_missing_source_is_violation():
    """Expected-red: completed executable primary + incomplete multi-file source.

    Primary target holds every approved primary file. Replica source holds only the
    LAST approved replica file. Replica target is empty. Third drive separates source
    from primary target so completion and source readiness are independent.
    No baseline_satisfied tasks, no certificate injection, no INC-027 reliance.
    """
    _mod, project_pure = _proj()
    primary = "primary:org/m"
    replica = "replica:org/m"
    # primary target=d0 (complete), replica source=d1 (last only), replica target=d2 (absent)
    tasks = [
        _task(primary, target="d0", source=None, order_key=1, guaranteed_durable=200),
        _task(replica, target="d2", source="d1", order_key=2, guaranteed_durable=200),
    ]
    files = [
        _file(primary, "a.bin", size=100, sha="a" * 64),
        _file(primary, "b.bin", size=100, sha="b" * 64),
        _file(replica, "a.bin", size=100, sha="a" * 64),
        _file(replica, "b.bin", size=100, sha="b" * 64),
    ]
    proposal = _approved_proposal(tasks=tasks, files=files)
    archived = {
        # Primary complete on d0
        ("org/m", "a.bin", "d0"): _arch(
            "org/m", "a.bin", "d0", orig_sha256="a" * 64, stored_bytes=100),
        ("org/m", "b.bin", "d0"): _arch(
            "org/m", "b.bin", "d0", orig_sha256="b" * 64, stored_bytes=100),
        # Replica source d1: only LAST approved file
        ("org/m", "b.bin", "d1"): _arch(
            "org/m", "b.bin", "d1", orig_sha256="b" * 64, stored_bytes=100),
        # a.bin absent on d1; replica target d2 empty
    }
    drives = {
        "d0": _drive("d0", fingerprint="a" * 64),
        "d1": _drive("d1", fingerprint="b" * 64),
        "d2": _drive("d2", fingerprint="c" * 64),
    }
    inp, graph = _inputs(proposal, archived=archived, drives=drives)
    out = project_pure(proposal, inp, graph, f.EMPTY_OVERLAY)
    assert f.is_refusal(out) and f.refusal_code(out) == "APPROVAL_PROJECTION_VIOLATION", (
        f"c11: expected APPROVAL_PROJECTION_VIOLATION, got {out!r}")
    ev = _refusal_evidence(out)
    assert ev.get("reason") == "source_not_ready", (
        f"c11: reason=source_not_ready required, got {ev!r}")
    assert ev.get("source") == "d1", f"c11: evidence.source must be d1, got {ev!r}"
    assert ev.get("repo") == "org/m", f"c11: evidence.repo must be org/m, got {ev!r}"


# ===========================================================================
# Expected-red: guaranteed_durable & stored-overrun
# ===========================================================================


def test_c12_null_guaranteed_durable_is_missing_guaranteed_durable():
    """Expected-red: null guaranteed_durable refuses with missing_guaranteed_durable."""
    _mod, project_pure = _proj()
    rid = "primary:org/m"
    t = _task(rid, guaranteed_durable=None)
    t["guaranteed_durable"] = None
    proposal = _approved_proposal(
        tasks=[t],
        files=[_file(rid, "model.safetensors", size=100, sha="m" * 64)],
    )
    inp, graph = _inputs(proposal, archived={})
    out = project_pure(proposal, inp, graph, f.EMPTY_OVERLAY)
    assert f.is_refusal(out) and f.refusal_code(out) == "APPROVAL_PROJECTION_VIOLATION", (
        f"c12: expected APPROVAL_PROJECTION_VIOLATION, got {out!r}")
    ev = _refusal_evidence(out)
    assert ev.get("reason") == "missing_guaranteed_durable", (
        f"c12: reason=missing_guaranteed_durable required, got {ev!r}")


def test_c13_explicit_zero_guaranteed_durable_never_becomes_100():
    """Expected-red: exact durable=0 must not coerce via `or 100` into overrun.

    Content-mismatched archived row with stored_bytes above the overrun floor keeps
    the task from shrinking. With exact guaranteed_durable=0, production must not
    fabricate a positive budget and must not issue stored_bytes_overrun; the task
    remains and retains zero. Current `or 100` path treats durable as 100 and reds.
    """
    _mod, project_pure = _proj()
    rid = "primary:org/m"
    proposal = _approved_proposal(
        tasks=[_task(rid, guaranteed_durable=0)],
        files=[_file(rid, "model.safetensors", size=50, sha="m" * 64)],
    )
    # Mismatched digest → cannot shrink; stored above floor (> 10**12).
    archived = {
        ("org/m", "model.safetensors", "d0"): _arch(
            "org/m", "model.safetensors", "d0",
            orig_sha256="x" * 64,
            stored_bytes=10**12 + 1,
        ),
    }
    inp, graph = _inputs(proposal, archived=archived)
    out = project_pure(proposal, inp, graph, f.EMPTY_OVERLAY)
    if f.is_refusal(out):
        code = f.refusal_code(out)
        ev = _refusal_evidence(out)
        assert not (code == "APPROVAL_PROJECTION_VIOLATION"
                    and ev.get("reason") == "missing_guaranteed_durable"), (
            "c13: explicit 0 must not be treated as missing_guaranteed_durable")
        assert not (code == "APPROVED_PLACEMENT_NO_LONGER_FEASIBLE"
                    and ev.get("reason") == "stored_bytes_overrun"), (
            f"c13: exact durable=0 must not issue stored_bytes_overrun via coerced 100; "
            f"got {out!r}")
        raise AssertionError(f"c13: unexpected refusal {code}: {out!r}")
    assert rid in _task_ids(out), (
        f"c13: mismatched content must remain under durable=0; remaining={_task_ids(out)}")
    for t in f.get_field(out, "tasks") or ():
        trid = t.get("requirement_id") if isinstance(t, dict) else f.get_field(t, "requirement_id")
        if trid != rid:
            continue
        gd = t.get("guaranteed_durable") if isinstance(t, dict) else f.get_field(
            t, "guaranteed_durable")
        assert gd == 0, f"c13: guaranteed_durable must remain 0, got {gd!r}"


def test_c14_stored_overrun_sums_approved_filenames_only_before_shrink():
    """Expected-red: overrun uses exact task.guaranteed_durable and sum of approved stored.

    Proposal file size totals are materially larger than task.guaranteed_durable so a
    budget derived from proposal sizes would not trip. Aggregate approved stored_bytes
    exceeds the threshold for the exact task budget. Large stored sits on a non-last
    approved file so last-file production stays red. Unapproved rows are excluded.
    """
    _mod, project_pure = _proj()
    rid = "primary:org/m"
    # Task budget small → threshold max(100*1000, 10**12) = 10**12 bytes (1 decimal TB).
    durable = 100
    # Proposal sizes sum to 4e9; size-derived durable would yield threshold 4e12 (~4 TB).
    size_a = 2_000_000_000
    size_b = 2_000_000_000
    assert size_a + size_b != durable
    tasks = [_task(rid, guaranteed_durable=durable)]
    files = [
        _file(rid, "a.bin", size=size_a, sha="a" * 64),
        _file(rid, "b.bin", size=size_b, sha="b" * 64),
    ]
    proposal = _approved_proposal(tasks=tasks, files=files)
    # Sum of approved stored exceeds task threshold (1 decimal TB) but not size-derived (~4 TB).
    stored_a = 10**12 + 5  # non-last
    stored_b = 1           # last (tiny — last-file path will not refuse)
    expected_sum = stored_a + stored_b
    assert expected_sum > max(durable * 1000, 10**12)
    assert expected_sum <= max((size_a + size_b) * 1000, 10**12)
    archived = {
        ("org/m", "a.bin", "d0"): _arch(
            "org/m", "a.bin", "d0", orig_sha256="a" * 64, stored_bytes=stored_a),
        ("org/m", "b.bin", "d0"): _arch(
            "org/m", "b.bin", "d0", orig_sha256="b" * 64, stored_bytes=stored_b),
        # Unapproved archive row — must not contribute to the sum
        ("org/m", "junk.onnx", "d0"): _arch(
            "org/m", "junk.onnx", "d0", orig_sha256="j" * 64, stored_bytes=10**15),
    }
    inp, graph = _inputs(proposal, archived=archived)
    out = project_pure(proposal, inp, graph, f.EMPTY_OVERLAY)
    assert f.is_refusal(out) and f.refusal_code(out) == "APPROVED_PLACEMENT_NO_LONGER_FEASIBLE", (
        f"c14: expected APPROVED_PLACEMENT_NO_LONGER_FEASIBLE, got {out!r}")
    ev = _refusal_evidence(out)
    assert ev.get("reason") == "stored_bytes_overrun", (
        f"c14: reason=stored_bytes_overrun required, got {ev!r}")
    assert ev.get("stored") == expected_sum, (
        f"c14: evidence.stored must equal sum of approved stored_bytes "
        f"({expected_sum}), got {ev.get('stored')!r} (unapproved rows excluded)")


# ===========================================================================
# Positive compatibility: unapproved rows excluded from overrun alone
# ===========================================================================


def test_c15_unapproved_archive_rows_do_not_drive_overrun_alone():
    """Positive compatibility: only unapproved rows huge → no stored_bytes_overrun."""
    _mod, project_pure = _proj()
    rid = "primary:org/m"
    durable = 10**9
    tasks = [_task(rid, guaranteed_durable=durable)]
    files = [_file(rid, "a.bin", size=100, sha="a" * 64)]
    proposal = _approved_proposal(tasks=tasks, files=files)
    archived = {
        ("org/m", "a.bin", "d0"): _arch(
            "org/m", "a.bin", "d0", orig_sha256="a" * 64, stored_bytes=100),
        ("org/m", "junk.onnx", "d0"): _arch(
            "org/m", "junk.onnx", "d0", orig_sha256="j" * 64, stored_bytes=10**15),
    }
    inp, graph = _inputs(proposal, archived=archived)
    out = project_pure(proposal, inp, graph, f.EMPTY_OVERLAY)
    if f.is_refusal(out):
        code = f.refusal_code(out)
        assert code != "APPROVED_PLACEMENT_NO_LONGER_FEASIBLE", (
            f"c15: unapproved junk must not cause stored overrun; got {out!r}")


# ===========================================================================
# Boundary documentation: INC-027 baseline certificates (not under test)
# ===========================================================================


def test_c16_inc027_baseline_boundary_documented():
    """Document INC-027: baseline cert self-copy is out of INC-023 Gate-1 scope.

    This pin is green when the ledger entry exists; it does not assert production
    multi-file baseline evidence (that is INC-027 / DEC-053–054).
    """
    from pathlib import Path
    ledger = Path(__file__).resolve().parents[1] / "docs" / "decision_log.md"
    text = ledger.read_text()
    assert "### INC-027:" in text, "c16: INC-027 must be allocated in decision_log"
    assert "baseline certificates" in text.lower() or "baseline certificate" in text.lower()
    assert "DEC-057" in text or "scope freeze" in text.lower()
