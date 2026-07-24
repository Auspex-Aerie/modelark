"""PR-05 / logical #36a canonical requirements & candidates (tests-first, RFC-002 / DEC-049).

Gate 1 pins the PURE candidate contract BEFORE production. #36a replaces ``reconcile._choose_partial``/
``WorkIntent.pinned_target`` authority with a functional core that, for every unsatisfied required copy,
emits *all* verified finish-in-place partial candidates and *all* policy-permitted fresh targets with
exact reuse/provenance/costs — and chooses nothing. #38 later consumes this canonical ``CandidateSet``,
ranks reuse, and assigns targets; #36a supplies the no-pin input only.

It encodes the operator's Gate-0/Gate-1 rulings:

  1. ``reconcile_plan()`` becomes a compatibility façade over a one-transaction fact reader plus the pure
     ``requirements``/``candidates`` core. Canonical requirements/candidates carry NO pin. The legacy
     ``tiered_v1`` placement adapter may temporarily reproduce proven-partial behaviour by *consuming*
     the candidates, but it is not canonical authority; the operator-visible pinning pathology is not
     fully removed until #38 chooses the better fresh target.
  2. Reuse is hash-bound. A required file on a target is REUSED only when a proven archived row exists
     (``orig_bytes == manifest.size_bytes`` in both branches; if the manifest carries an upstream
     SHA-256 the archived ``orig_sha256`` must equal it; otherwise a non-null archived ``orig_sha256``
     binds the baseline, carrying the accepted same-path/same-size provider residual). A ReusableFile
     records path, size, bound hash, and proof source.
  3. A target that holds ANY required-but-unproven/mismatched archived row is neither a verified
     finish-in-place candidate nor a fresh target: that target is OMITTED and its rows are preserved as
     policy drift. #36a never emits "missing" work that would silently overwrite/delete such a row.
  4. Missing work carries exact file identity (path, size, hash, storage action), not bare filenames.
  5. Replica source is SINGULAR per candidate — one source-target candidate per exact ``SourceIdentity``
     (differing stored sizes / annex keys / raw-fallback outcomes cannot share one exact budget); a
     ``PendingHome`` reference is used only while the home requirement is unresolved (#38 resolves it).
  6. ``FileBudget``/``CandidateBudget`` live in ``modelark.budgets`` and are shared by ``candidates`` and
     the ``capacity`` compatibility path so the two cannot drift; budgets carry both guaranteed and
     expected durable bytes plus both workspace peaks. ``MovementCost.transfer_bytes`` is a small
     separate fact (fetch = raw acquisition bytes; replica = exact/estimated stored transfer from the
     singular source); reused/missing membership is read from the file sets, never duplicated.
  7. The no-RAID single-largest-primary protected-home fallback is preserved for this slice.
  8. Records are deeply immutable canonical tuples/frozen records. The pure API accepts no DB
     connection and imports no SQLite/transport.

RED until ``modelark.candidates`` and ``modelark.budgets`` exist; the lazy import + ``_require_pure()``
guard makes each contract test fail for the reviewed missing behaviour, not a fixture cascade. GREEN
characterization tests freeze the executor-facing ``tiered_v1`` placement outcomes that the façade
refactor must preserve — deliberately over BENIGN scenarios so the pin-can't-fit pathology is not frozen.

Self-running: CI executes ``python tests/test_candidates.py`` directly.
"""
from __future__ import annotations

import ast
import dataclasses
import inspect
import sqlite3
from unittest import mock

import _admission_compat
from modelark import archive_manifest, capacity, reconcile
from modelark.core import db

try:
    import modelark.candidates as candidates
    _HAS_CANDIDATES = True
except ModuleNotFoundError as exc:               # ONLY the exact absent submodule — a real defect surfaces
    if exc.name != "modelark.candidates":
        raise
    candidates = None
    _HAS_CANDIDATES = False

try:
    import modelark.budgets as budgets
    _HAS_BUDGETS = True
except ModuleNotFoundError as exc:
    if exc.name != "modelark.budgets":
        raise
    budgets = None
    _HAS_BUDGETS = False


# Distinct 64-hex upstream SHA-256 fixtures so provenance branches never collide by accident.
HW = "1" * 64        # a weight shard's canonical upstream hash
HW2 = "3" * 64       # a second weight shard
HC = "2" * 64        # a config/aux file with an upstream hash
MARGIN = capacity.EXPECTED_MARGIN
RATIO = capacity.DEFAULT_FLOAT_RATIO

# Explicit graph-affecting compression config copied into the immutable input. The pure budget path
# consumes exactly these values via compress.plan_codec (which does direct cfg[...] access) — it must
# never invent defaults or fall back to wishlist.compression(). max_compress_ram_gb=64 keeps a small
# shard in CODEC_WHOLE deterministically (no dependence on host zstd availability).
_CFG = (("max_compress_ram_gb", 64), ("stream_compress", True), ("threads", 4))


def _cfg_dict():
    return dict(_CFG)


def _require_pure():
    if not (_HAS_CANDIDATES and _HAS_BUDGETS):
        raise AssertionError(
            "#36a must add the pure core modelark/candidates.py (requirements(inp)->RequirementGraph, "
            "candidates(inp, graph)->CandidateSet with PlannerInput/DriveFact/ArchivedFileFact/Candidate/"
            "ReusableFile/SourceIdentity/PendingHome/MovementCost/Satisfaction, no `con` params, no pin) "
            "and the shared modelark/budgets.py (FileBudget/CandidateBudget + file_budget/aggregate), "
            "and make reconcile_plan() a façade over the fact reader + pure core.")


# --------------------------------------------------------------------------------------------------
# Pure synthetic-input builders (no DB). Only constructed AFTER _require_pure() passes.
# --------------------------------------------------------------------------------------------------
def _mf(name, size, sha256, *, fmt="safetensors", quant="bf16"):
    if fmt == "safetensors" and quant in archive_manifest.FLOAT_QUANTS:
        action = "compress"
    else:
        action = "raw"
    return archive_manifest.ManifestFile(
        rfilename=name, size_bytes=size, sha256=sha256, format=fmt, quant=quant, storage_action=action)


def _drive(label, *, role="primary", raid=False, cap=10**12, fscap=None, epoch=1,
           fs_uuid=None, annex_uuid=None, serial=None):
    return candidates.DriveFact(
        drive_label=label, role=role, raid_backed=raid, capacity_bytes=cap,
        filesystem_capacity_bytes=(cap if fscap is None else fscap), identity_epoch=epoch,
        fs_uuid=fs_uuid, annex_uuid=annex_uuid, serial=serial)


def _arch(repo, drive, name, *, sha=None, obytes=None, sbytes=None, key=None):
    return candidates.ArchivedFileFact(
        repo_id=repo, drive_label=drive, rfilename=name,
        orig_sha256=sha, orig_bytes=obytes, stored_bytes=sbytes, annex_key=key)


def _input(*, selection, manifests, numcopies, drives, archived, cfg=_CFG, ratio=RATIO):
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


def _cands(cset, requirement_id):
    for req_id, cand_tuple in cset.by_requirement:
        if req_id == requirement_id:
            return cand_tuple
    return ()


def _targets(cands):
    return {c.target_drive for c in cands}


def _missing_names(cand):
    return {f.rfilename for f in cand.missing_files}


def _reused_names(cand):
    return {f.rfilename for f in cand.reused_files}


# --------------------------------------------------------------------------------------------------
# Change-contract matrix — RED until #36a lands. One compact table over the binding cases.
# --------------------------------------------------------------------------------------------------
def _scenario_metadata_only_stub():
    """Aux proven, weights absent → one finish-in-place candidate; weights are exact missing identity."""
    inp = _input(
        selection=["org/m"],
        manifests=[("org/m", [_mf("model.safetensors", 100, HW), _mf("config.json", 10, HC, fmt="aux", quant=None)])],
        numcopies=[("org/m", 1)],
        drives=[_drive("drive-01")],
        archived=[_arch("org/m", "drive-01", "config.json", sha=HC, obytes=10, sbytes=10)],
    )
    graph, cset = _run(inp)
    cands = _cands(cset, "primary:org/m")
    assert _targets(cands) == {"drive-01"}, "expected one finish-in-place candidate on the partial drive"
    c = cands[0]
    assert _reused_names(c) == {"config.json"} and _missing_names(c) == {"model.safetensors"}
    weight = next(f for f in c.missing_files if f.rfilename == "model.safetensors")
    assert weight.size_bytes == 100 and weight.sha256 == HW and weight.storage_action == "compress"
    assert c.movement_cost.transfer_bytes == 100      # raw acquisition bytes of the missing weight
    assert c.budget.guaranteed_durable == 100


def _scenario_partial_cant_hold_but_fresh_exists():
    """#36a makes NO feasibility call: both the finish-in-place partial AND the fresh target are emitted."""
    inp = _input(
        selection=["org/m"],
        manifests=[("org/m", [_mf("a.safetensors", 100, HW), _mf("b.safetensors", 100, HW2)])],
        numcopies=[("org/m", 1)],
        drives=[_drive("small", cap=1), _drive("fresh", cap=10**12)],
        archived=[_arch("org/m", "small", "a.safetensors", sha=HW, obytes=100, sbytes=100)],
    )
    _, cset = _run(inp)
    cands = _cands(cset, "primary:org/m")
    assert _targets(cands) == {"small", "fresh"}, "tiny partial and roomy fresh target are BOTH alternatives"
    small = next(c for c in cands if c.target_drive == "small")
    fresh = next(c for c in cands if c.target_drive == "fresh")
    assert _reused_names(small) == {"a.safetensors"} and _missing_names(small) == {"b.safetensors"}
    assert _reused_names(fresh) == set() and _missing_names(fresh) == {"a.safetensors", "b.safetensors"}
    assert not any(hasattr(c, "pinned_target") for c in cands)


def _scenario_multiple_partial_drives():
    inp = _input(
        selection=["org/m"],
        manifests=[("org/m", [_mf("a.safetensors", 100, HW), _mf("b.safetensors", 100, HW2)])],
        numcopies=[("org/m", 1)],
        drives=[_drive("d1"), _drive("d2")],
        archived=[
            _arch("org/m", "d1", "a.safetensors", sha=HW, obytes=100, sbytes=100),
            _arch("org/m", "d2", "b.safetensors", sha=HW2, obytes=100, sbytes=100),
        ],
    )
    _, cset = _run(inp)
    cands = _cands(cset, "primary:org/m")
    assert _targets(cands) == {"d1", "d2"}
    assert _missing_names(next(c for c in cands if c.target_drive == "d1")) == {"b.safetensors"}
    assert _missing_names(next(c for c in cands if c.target_drive == "d2")) == {"a.safetensors"}


def _scenario_unproven_hash_blocks_target():
    """Null archived hash on a required file → target OMITTED + drift; clean drive stays a fresh target."""
    inp = _input(
        selection=["org/m"],
        manifests=[("org/m", [_mf("a.safetensors", 100, HW)])],
        numcopies=[("org/m", 1)],
        drives=[_drive("legacy"), _drive("clean")],
        archived=[_arch("org/m", "legacy", "a.safetensors", sha=None, obytes=100, sbytes=100)],
    )
    _, cset = _run(inp)
    cands = _cands(cset, "primary:org/m")
    assert _targets(cands) == {"clean"}, "unproven partial must not become a candidate (no silent overwrite)"
    assert any(d.drive_label == "legacy" and d.rfilename == "a.safetensors" for d in cset.drift)


def _scenario_mismatch_and_size_block_target():
    # (label, archived kwargs, manifest sha). The last case is the hashless branch: no upstream SHA plus a
    # non-null archived orig_sha256 but the WRONG orig_bytes → zero reuse credit (the size gate fails), so
    # the required row is unproven and blocks its target rather than being silently reused.
    cases = (
        ("hashbad", dict(sha="f" * 64, obytes=100), HW),
        ("sizebad", dict(sha=HW, obytes=999), HW),
        ("hashless_sizebad", dict(sha="a" * 64, obytes=999), None),
    )
    for label, kw, manifest_sha in cases:
        inp = _input(
            selection=["org/m"],
            manifests=[("org/m", [_mf("a.safetensors", 100, manifest_sha)])],
            numcopies=[("org/m", 1)],
            drives=[_drive("bad"), _drive("clean")],
            archived=[_arch("org/m", "bad", "a.safetensors", sbytes=100, **kw)],
        )
        _, cset = _run(inp)
        assert _targets(_cands(cset, "primary:org/m")) == {"clean"}, f"{label}: mismatched row must block its target"
        assert any(d.drive_label == "bad" for d in cset.drift), f"{label}: drift not recorded"


def _scenario_reuse_via_archived_hash_only():
    """No upstream manifest SHA (git-tracked blob): a non-null archived orig_sha256 + size binds reuse."""
    inp = _input(
        selection=["org/m"],
        # A third un-archived file keeps the requirement INCOMPLETE, so both proven reused files are
        # inspected on a finish-in-place candidate (not swallowed into a fully-satisfied requirement).
        manifests=[("org/m", [_mf("weights.safetensors", 100, HW),
                              _mf("tokenizer.model", 20, None, fmt="aux", quant=None),
                              _mf("extra.safetensors", 50, HW2)])],
        numcopies=[("org/m", 1)],
        drives=[_drive("d1")],
        archived=[
            _arch("org/m", "d1", "weights.safetensors", sha=HW, obytes=100, sbytes=100),
            _arch("org/m", "d1", "tokenizer.model", sha="a" * 64, obytes=20, sbytes=20),
        ],
    )
    _, cset = _run(inp)
    c = _cands(cset, "primary:org/m")[0]
    assert _missing_names(c) == {"extra.safetensors"}, "the un-archived file keeps a finish-in-place candidate"
    tok = next(f for f in c.reused_files if f.rfilename == "tokenizer.model")
    assert tok.proof_source == candidates.ProofSource.ARCHIVED_ORIG_SHA256 and tok.bound_hash == "a" * 64
    w = next(f for f in c.reused_files if f.rfilename == "weights.safetensors")
    assert w.proof_source == candidates.ProofSource.MANIFEST_SHA256 and w.bound_hash == HW


def _scenario_format_and_byte_edges():
    """GGUF, pickle, aux-only, zero-byte, and one huge shard all yield valid budgets."""
    huge = 3 * 10**12
    inp = _input(
        selection=["g/gguf", "p/pt", "a/aux", "z/zero", "h/huge"],
        manifests=[
            ("g/gguf", [_mf("model.gguf", 50, HW, fmt="gguf", quant="Q4_K_M")]),
            ("p/pt", [_mf("pytorch_model.bin", 50, HW, fmt="pytorch", quant="fp16")]),
            ("a/aux", [_mf("readme.md", 5, None, fmt="aux", quant=None)]),
            ("z/zero", [_mf("empty.safetensors", 0, HW)]),
            ("h/huge", [_mf("shard.safetensors", huge, HW)]),
        ],
        numcopies=[(r, 1) for r in ("g/gguf", "p/pt", "a/aux", "z/zero", "h/huge")],
        drives=[_drive("d1")],
        archived=[],
    )
    _, cset = _run(inp)
    gguf = _cands(cset, "primary:g/gguf")[0]
    assert gguf.budget.guaranteed_durable == 50 and gguf.movement_cost.transfer_bytes == 50
    pt = _cands(cset, "primary:p/pt")           # pickle is stored raw (no compression shrink)
    # guaranteed == size proves no compression shrink for a raw file; expected still carries the shared
    # seam's estimate margin (capacity applies EXPECTED_MARGIN to raw and compress alike — test_capacity
    # pins the same margin-on-raw for a gguf replica estimate).
    assert len(pt) == 1 and pt[0].budget.guaranteed_durable == 50
    assert pt[0].budget.expected_durable == int(50 * MARGIN)
    aux = _cands(cset, "primary:a/aux")         # aux-only repo is stored raw
    assert len(aux) == 1 and aux[0].budget.guaranteed_durable == 5
    huge_c = _cands(cset, "primary:h/huge")[0]
    assert huge_c.budget.guaranteed_durable == huge
    assert huge_c.budget.expected_durable == int(huge * RATIO * MARGIN)   # compressible float shard
    zero_c = _cands(cset, "primary:z/zero")[0]
    assert zero_c.budget.guaranteed_durable == 0


def _scenario_complete_satisfaction_multi_copy():
    """Numcopies=1 complete on two eligible drives → satisfaction lists both copies; no candidates."""
    inp = _input(
        selection=["org/m"],
        manifests=[("org/m", [_mf("a.safetensors", 100, HW)])],
        numcopies=[("org/m", 1)],
        drives=[_drive("d1"), _drive("d2")],
        archived=[
            _arch("org/m", "d1", "a.safetensors", sha=HW, obytes=100, sbytes=100),
            _arch("org/m", "d2", "a.safetensors", sha=HW, obytes=100, sbytes=100),
        ],
    )
    _, cset = _run(inp)
    assert _cands(cset, "primary:org/m") == (), "a satisfied requirement emits no candidates"
    sat = next(s for s in cset.satisfied if s.requirement_id == "primary:org/m")
    assert {cp.drive_label for cp in sat.copies} == {"d1", "d2"}, "both complete copies are recorded, none chosen"


def _scenario_protected_replica_pending_home():
    """No home copy yet → replica candidates reference the home requirement, not a guessed source."""
    inp = _input(
        selection=["org/m"],
        manifests=[("org/m", [_mf("a.safetensors", 100, HW)])],
        numcopies=[("org/m", 2)],
        drives=[_drive("raid", raid=True), _drive("rep", role="replica")],
        archived=[],
    )
    graph, cset = _run(inp)
    home = _cands(cset, "protected_home:org/m")
    rep = _cands(cset, "protected_replica:org/m")
    assert _targets(home) == {"raid"}
    assert _targets(rep) == {"rep"}
    assert isinstance(rep[0].source, candidates.PendingHome)
    assert rep[0].source.requirement_id == "protected_home:org/m"
    assert rep[0].depends_on_requirement == "protected_home:org/m"
    # With no exact source yet, the transfer cost is the deterministic ESTIMATE — a replica copies the
    # stored bytes, so transfer equals the estimated durable budget (never zero, never a guessed exact
    # size). Tied to the budget field rather than a hard-coded margin formula to stay robust.
    assert rep[0].movement_cost.transfer_bytes == rep[0].budget.expected_durable > 0


def _scenario_protected_replica_singular_exact_sources():
    """Two complete homes → ONE source-target candidate per exact SourceIdentity, each exact-costed."""
    inp = _input(
        selection=["org/m"],
        manifests=[("org/m", [_mf("a.safetensors", 100, HW)])],
        numcopies=[("org/m", 2)],
        drives=[_drive("raid1", raid=True), _drive("raid2", raid=True), _drive("rep", role="replica")],
        archived=[
            _arch("org/m", "raid1", "a.safetensors", sha=HW, obytes=100, sbytes=90, key="k1"),
            _arch("org/m", "raid2", "a.safetensors", sha=HW, obytes=100, sbytes=88, key="k2"),
        ],
    )
    _, cset = _run(inp)
    rep = _cands(cset, "protected_replica:org/m")
    sources = {c.source.drive_label for c in rep}
    assert sources == {"raid1", "raid2"}, "one candidate per exact complete home source (no ExactSources bundle)"
    assert all(isinstance(c.source, candidates.SourceIdentity) for c in rep)
    by_src = {c.source.drive_label: c for c in rep}
    assert by_src["raid1"].movement_cost.transfer_bytes == 90    # exact stored transfer from that source
    assert by_src["raid2"].movement_cost.transfer_bytes == 88


def _scenario_wrong_tier_complete_relocation_omitted():
    """A complete copy on a non-eligible drive is drift, never an executable relocation candidate."""
    inp = _input(
        selection=["org/m"],
        manifests=[("org/m", [_mf("a.safetensors", 100, HW)])],
        numcopies=[("org/m", 2)],
        drives=[_drive("raid", raid=True), _drive("rep", role="replica"), _drive("plain")],
        archived=[_arch("org/m", "plain", "a.safetensors", sha=HW, obytes=100, sbytes=100)],
    )
    _, cset = _run(inp)
    home = _cands(cset, "protected_home:org/m")
    assert "plain" not in _targets(home), "no annex-to-annex relocation candidate onto the eligible tier"
    assert _targets(home) == {"raid"}
    assert "protected_home:org/m" not in {s.requirement_id for s in cset.satisfied}
    assert any(d.drive_label == "plain" for d in cset.drift)


def _scenario_no_raid_single_largest_primary_fallback():
    """Confirmed for this slice: with no RAID, protected_home eligibility is the single largest primary."""
    inp = _input(
        selection=["org/m"],
        manifests=[("org/m", [_mf("a.safetensors", 100, HW)])],
        numcopies=[("org/m", 2)],
        drives=[_drive("big", cap=9 * 10**12), _drive("small", cap=10**12), _drive("rep", role="replica")],
        archived=[],
    )
    graph, cset = _run(inp)
    home_req = next(r for r in graph.desired if r.requirement_id == "protected_home:org/m")
    assert home_req.eligible_drives == ("big",), "no-RAID fallback is the single largest primary only"
    assert _targets(_cands(cset, "protected_home:org/m")) == {"big"}


_MATRIX = [
    ("metadata_only_stub", _scenario_metadata_only_stub),
    ("partial_cant_hold_but_fresh_exists", _scenario_partial_cant_hold_but_fresh_exists),
    ("multiple_partial_drives", _scenario_multiple_partial_drives),
    ("unproven_hash_blocks_target", _scenario_unproven_hash_blocks_target),
    ("mismatch_and_size_block_target", _scenario_mismatch_and_size_block_target),
    ("reuse_via_archived_hash_only", _scenario_reuse_via_archived_hash_only),
    ("format_and_byte_edges", _scenario_format_and_byte_edges),
    ("complete_satisfaction_multi_copy", _scenario_complete_satisfaction_multi_copy),
    ("protected_replica_pending_home", _scenario_protected_replica_pending_home),
    ("protected_replica_singular_exact_sources", _scenario_protected_replica_singular_exact_sources),
    ("wrong_tier_complete_relocation_omitted", _scenario_wrong_tier_complete_relocation_omitted),
    ("no_raid_single_largest_primary_fallback", _scenario_no_raid_single_largest_primary_fallback),
]


def test_contract_candidate_matrix():
    _require_pure()
    failures = []
    for name, fn in _MATRIX:
        try:
            fn()
        except Exception as exc:                 # noqa: BLE001 — keep the whole matrix map, not the first abort
            failures.append(f"{name}: {type(exc).__name__}: {exc}")
    assert not failures, "matrix scenarios failed:\n  " + "\n  ".join(failures)


def test_contract_no_pin_no_delete_deep_immutability():
    _require_pure()
    assert not any(f.name == "pinned_target" for f in dataclasses.fields(candidates.Candidate))
    inp = _input(
        selection=["org/m"],
        manifests=[("org/m", [_mf("a.safetensors", 100, HW)])],
        numcopies=[("org/m", 1)],
        drives=[_drive("d1")],
        archived=[_arch("org/m", "d1", "a.safetensors", sha=HW, obytes=100, sbytes=100)],
    )
    _, cset = _run(inp)
    # Deeply immutable: mutating any frozen record raises, and containers are tuples not lists/dicts.
    # (No-mutate/no-delete of archived facts is proven by the drift scenarios preserving unproven rows.)
    sat = cset.satisfied[0]
    caught = False
    try:
        sat.requirement_id = "x"                    # type: ignore[misc]
    except dataclasses.FrozenInstanceError:
        caught = True
    assert caught, "records must be frozen"
    assert isinstance(cset.satisfied, tuple) and isinstance(cset.by_requirement, tuple)


def test_contract_determinism_and_stop_restart_reconstruction():
    _require_pure()

    def build(order):
        drives = [_drive("d1"), _drive("d2"), _drive("rep", role="replica")]
        archived = [
            _arch("org/m", "d1", "a.safetensors", sha=HW, obytes=100, sbytes=100),
            _arch("org/m", "d2", "b.safetensors", sha=HW2, obytes=100, sbytes=100),
        ]
        if order == "shuffled":
            drives = list(reversed(drives))
            archived = list(reversed(archived))
        return _input(
            selection=["org/m"],
            manifests=[("org/m", [_mf("a.safetensors", 100, HW), _mf("b.safetensors", 100, HW2)])],
            numcopies=[("org/m", 2)],
            drives=drives,
            archived=archived,
        )

    a1 = candidates.candidates(build("natural"), candidates.requirements(build("natural")))
    a2 = candidates.candidates(build("shuffled"), candidates.requirements(build("shuffled")))
    # Byte-equivalent reconstruction from unchanged durable facts (stop/restart), order-independent.
    assert a1 == a2, "CandidateSet must be canonical and independent of input/query order"
    g1 = candidates.requirements(build("natural"))
    g2 = candidates.requirements(build("shuffled"))
    assert g1.requirement_set_hash == g2.requirement_set_hash


def test_contract_pure_no_io_boundary():
    _require_pure()
    for fn in (candidates.requirements, candidates.candidates):
        params = set(inspect.signature(fn).parameters)
        assert "con" not in params and "connection" not in params, f"{fn.__name__} must take no DB connection"
    # Import/dependency boundary, checked at the source: neither pure module may pull in SQLite/socket/
    # DB/transport, read global config (wishlist), or re-enter the impure reconcile/capacity layer (also
    # an import cycle). Banning wishlist proves the budget path takes config as data, never global config.
    # Match on dotted COMPONENTS so every import form is covered, including the relative/aliased ones a
    # fully-qualified prefix check misses: `import modelark.wishlist`, `from modelark import wishlist`
    # (records the name, not just `modelark`), `from modelark.wishlist import x`, `from . import wishlist`
    # (ImportFrom.module is None), and `from .wishlist import x` (module is the bare leaf `wishlist`).
    banned = {"sqlite3", "socket", "wishlist", "fetch", "reconcile", "capacity", "db"}
    for module in (candidates, budgets):
        names = set()
        for node in ast.walk(ast.parse(inspect.getsource(module))):
            if isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    names.add(node.module)
                names.update(alias.name for alias in node.names)   # `from pkg import submodule`
        offenders = sorted(n for n in names if banned & set(n.split(".")))
        assert not offenders, f"pure {module.__name__} must not import {offenders}"
    # Proportional runtime proof: a full run under a poisoned connection still succeeds.
    inp = _input(
        selection=["org/m"],
        manifests=[("org/m", [_mf("a.safetensors", 100, HW)])],
        numcopies=[("org/m", 1)],
        drives=[_drive("d1")],
        archived=[],
    )
    with mock.patch("sqlite3.connect", side_effect=AssertionError("pure core must not touch SQLite")):
        _run(inp)


def test_contract_shared_budget_seam_exposes_both_modes():
    _require_pure()
    assert hasattr(budgets, "FileBudget") and hasattr(budgets, "CandidateBudget")
    assert candidates.CandidateBudget is budgets.CandidateBudget, "one budget truth shared with candidates"
    # gap #3: capacity must consume the SAME shared type, not a duplicate class/calculation.
    assert capacity.FileBudget is budgets.FileBudget, "capacity must re-export budgets.FileBudget, not duplicate it"
    # gap #1: the injected config is consumed (CODEC_WHOLE at 64 GB budget → output_cap == raw size).
    fb = budgets.file_budget(_mf("a.safetensors", 100, HW), RATIO, _cfg_dict())
    assert fb.guaranteed_durable == 100 and fb.expected_durable == int(100 * RATIO * MARGIN)
    assert fb.workspace_peak_guaranteed == 100, "workspace must reflect the codec chosen from the injected cfg"
    # gap #1: no invented defaults — a config missing the compress-gate keys must raise, not silently default.
    raised = False
    try:
        budgets.file_budget(_mf("x.safetensors", 100, HW), RATIO, {})
    except KeyError:
        raised = True
    assert raised, "the pure budget path must not invent compression defaults from an empty cfg"


def test_contract_reconcile_plan_exposes_canonical_candidateset():
    """gap #2 integration: the façade actually wires the pure core. reconcile_plan() exposes the
    canonical CandidateSet (no pinned_target), so candidates.py cannot sit dormant while every current
    test still passes. Uses the in-memory DB helpers below (resolved at call time)."""
    _require_pure()
    con = _mem()
    _db_drive(con, "d1", cap=10**9)
    _db_repo(con, "org/m", files=(
        ("a.safetensors", 100, "safetensors", "bf16"),
        ("b.safetensors", 100, "safetensors", "bf16")))
    _db_archive(con, "org/m", "d1", ("a.safetensors", 100, 100, HW))   # proven partial → a candidate exists
    result = reconcile.reconcile_plan(con, "ark")
    assert isinstance(result.candidates, candidates.CandidateSet), \
        "reconcile_plan() must expose the canonical CandidateSet — the pure core cannot stay dormant"
    emitted = [c for _, cs in result.candidates.by_requirement for c in cs]
    assert emitted, "expected a finish-in-place candidate for the unsatisfied requirement"
    assert not any(hasattr(c, "pinned_target") for c in emitted), "façade candidates carry no pinned_target"
    con.close()


# --------------------------------------------------------------------------------------------------
# Characterization — GREEN now. Freezes executor-facing tiered_v1 placement the façade must preserve.
# Deliberately BENIGN (finish-in-place fits; fresh spread) so the pin-can't-fit pathology is NOT frozen.
# --------------------------------------------------------------------------------------------------
def _mem():
    con = sqlite3.connect(":memory:", isolation_level=None)
    for statement in db._statements(db.SCHEMA_PATH.read_text()):
        con.execute(statement)
    con.execute("INSERT INTO plans(plan_id,name,is_active) VALUES('ark','Ark',1)")
    return con


def _db_drive(con, label, *, role="primary", raid=False, cap=10**9):
    con.execute(
        "INSERT INTO drives(drive_label,role,raid_backed,capacity_bytes,free_bytes) VALUES(?,?,?,?,?)",
        [label, role, int(raid), cap, cap])
    con.execute("INSERT INTO plan_drives(plan_id,drive_label) VALUES('ark',?)", [label])


def _db_repo(con, repo, *, copies=1, files=(("model.safetensors", 100, "safetensors", "bf16"),), sha=HW):
    con.execute("INSERT INTO models(repo_id,numcopies) VALUES(?,?)", [repo, copies])
    con.execute("INSERT INTO selection(repo_id,finalized_at) VALUES(?,'2026-01-01')", [repo])
    con.executemany(
        "INSERT INTO files(repo_id,rfilename,size_bytes,format,quant,sha256) VALUES(?,?,?,?,?,?)",
        [(repo, name, size, fmt, quant, sha) for name, size, fmt, quant in files])


def _db_archive(con, repo, drive, *rows):
    con.executemany(
        "INSERT INTO archived(repo_id,rfilename,drive_label,stored_bytes,orig_bytes,orig_sha256,compressed) "
        "VALUES(?,?,?,?,?,?,0)",
        [(repo, name, drive, sbytes, obytes, sha) for name, sbytes, obytes, sha in rows])


def test_char_finish_in_place_when_partial_fits_is_preserved():
    """A proven partial on an eligible primary that has room stays a finish-in-place target."""
    with _admission_compat.seam_patch():
        con = _mem()
        _db_drive(con, "d1", cap=10**9)
        _db_repo(con, "org/m", files=(
            ("a.safetensors", 100, "safetensors", "bf16"),
            ("b.safetensors", 100, "safetensors", "bf16")))
        _db_archive(con, "org/m", "d1", ("a.safetensors", 100, 100, HW))
        result = reconcile.reconcile_plan(con, "ark")
        plan = capacity.plan_capacity(con, result, evidence_by_drive=_admission_compat.evidence_for_plan(con))
        targets = {t.requirement_id: t.target_drive for t in plan.tasks}
        assert targets.get("primary:org/m") == "d1", "finish-in-place placement must survive the façade refactor"
        con.close()


def test_char_fresh_bulk_spread_is_deterministic():
    """Fresh placement across ample primaries is stable and repeatable — behavior the façade preserves."""
    with _admission_compat.seam_patch():
        con = _mem()
        _db_drive(con, "d1", cap=10**9)
        _db_drive(con, "d2", cap=10**9)
        _db_repo(con, "org/a")
        _db_repo(con, "org/b")
        first = capacity.plan_capacity(
            con, reconcile.reconcile_plan(con, "ark"),
            evidence_by_drive=_admission_compat.evidence_for_plan(con))
        again = capacity.plan_capacity(
            con, reconcile.reconcile_plan(con, "ark"),
            evidence_by_drive=_admission_compat.evidence_for_plan(con))
        assert first.to_dict()["tasks"] == again.to_dict()["tasks"]
        assert {t.requirement_id for t in first.tasks} == {"primary:org/a", "primary:org/b"}
        con.close()


def main():
    tests = sorted((n, f) for n, f in globals().items()
                   if n.startswith("test_") and callable(f))
    passed, failed = [], []
    for name, fn in tests:
        try:
            fn()
            passed.append(name)
            print(f"PASS  {name}")
        except Exception as exc:                 # noqa: BLE001 — Gate-1 wants the full red/green map
            failed.append(name)
            print(f"FAIL  {name}  -> {type(exc).__name__}: {exc}")
    print(f"\n{len(passed)} passed, {len(failed)} failed")
    print("(Gate-1 tests-only: the test_contract_* map is EXPECTED RED until #36a production lands;")
    print(" the test_char_* characterization is GREEN and must stay green through the façade refactor.)")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
