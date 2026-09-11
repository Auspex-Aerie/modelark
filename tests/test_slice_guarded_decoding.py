"""C2: explicit policy, bounded container hooks and guarded source lifetime."""
# ruff: noqa: F811 -- imported pytest fixtures deliberately name test parameters
from contextlib import contextmanager
from dataclasses import replace
import hashlib
import io
import json
import struct

import pytest

from modelark import artifact_io, artifact_preflight, codec_supervisor, streamznn
from modelark.artifact_policy import DecodePolicy, qualified_policy
from modelark.slice import state
from modelark.slice.decoding import original_stream
from modelark.slice.transaction import TransferRefusal
from test_slice_local_source import attachment  # noqa: F401
from test_slice_fat32_transactions import fat  # noqa: F401
from test_slice_fence_integration import archive  # noqa: F401
from test_slice_operator import store  # noqa: F401


def header(original=128, stored=36):
    result = bytearray(32)
    result[:4] = b"ZN\x00\x05"
    result[5], result[8], result[15] = 10, 1, 1
    result[16:24], result[24:32] = original.to_bytes(8, "little"), stored.to_bytes(8, "little")
    return bytes(result)


def container(*frames):
    return streamznn.MAGIC + b"".join(struct.pack("<I", len(frame)) + frame for frame in frames)


def inspect(blob, expected, *, policy=None, check=lambda: None):
    return artifact_preflight.inspect_original(
        io.BytesIO(blob), compressed=True, stored_bytes=len(blob), expected_bytes=expected,
        policy=policy or qualified_policy(), check=check)


@pytest.mark.parametrize("kind", ["whole", "stream"])
def test_later_headers_checked_without_native_decode(monkeypatch, kind):
    monkeypatch.setattr(codec_supervisor, "_launch", lambda *a: pytest.fail("preflight launched worker"))
    monkeypatch.setattr(artifact_preflight, "available_memory", lambda: {"available_bytes": 16 << 30})
    frame = header() + b"abcd"
    blob = frame if kind == "whole" else container(frame, frame)
    inspect(blob, 128 if kind == "whole" else 256)
    if kind == "stream":
        with pytest.raises(artifact_io.DecodeError, match="LIMIT"):
            inspect(container(frame, header(original=9 << 30) + b"abcd"), 16 << 30)


@pytest.mark.parametrize("blob,expected,error", [
    (header() + b"abc", 128, "INVALID"),
    (header() + b"abcdextra", 128, "INVALID"),
    (header() + b"abcd", 129, "INVALID"),
    (container(header() + b"abcd", header() + b"abc"), 256, "INVALID"),
    (b"something else", 14, "UNSUPPORTED"),
])
def test_preflight_framing_refusals(blob, expected, error):
    with pytest.raises(artifact_io.DecodeError, match=error):
        inspect(blob, expected)


def test_preflight_checks_and_discards_payload_in_small_reads(monkeypatch):
    monkeypatch.setattr(artifact_preflight, "available_memory", lambda: {"available_bytes": 16 << 30})
    calls = []
    class Source(io.BytesIO):
        def read(self, size):
            assert 0 < size <= 64 << 10
            calls.append(size)
            return super().read(min(size, 127))
    blob = header(stored=100032) + bytes(100000)
    checks = []
    artifact_preflight.inspect_original(Source(blob), compressed=True, stored_bytes=len(blob),
                                        expected_bytes=128, policy=qualified_policy(),
                                        check=lambda: checks.append(1))
    assert len(checks) >= 2 * len(calls)


@pytest.mark.parametrize("field", ["version", "limits", "memory", "zstd_window_unit"])
def test_policy_closed_world_and_missing_fields(field):
    record = qualified_policy().to_record()
    record.pop(field)
    with pytest.raises(ValueError):
        DecodePolicy.from_record(record)


@pytest.mark.parametrize("location", [None, "limits", "memory"])
def test_policy_unknown_fields_refused(location):
    record = qualified_policy().to_record()
    (record if location is None else record[location])["future"] = True
    with pytest.raises(ValueError):
        DecodePolicy.from_record(record)


@pytest.mark.parametrize("kind", ["native", "fat32"])
def test_both_folder_versions_roundtrip_without_widening_old_seals(kind):
    if kind == "native":
        from test_slice_folder_plan import plan
        from modelark.slice.folder_plan import binding_for, POLICY_VERSION
    else:
        from test_slice_fat32_plan import plan
        from modelark.slice.fat32_plan import binding_for, POLICY_VERSION
    old = plan()
    old_json = old.to_json()
    assert "decode_policy" not in json.loads(old_json)
    assert state._load_plan(old_json).to_json() == old_json
    policy = qualified_policy()
    binding = binding_for(old.destination.target, old.catalog, old.parent_path, old.backing_ids, policy)
    new = replace(old, destination=binding, version=POLICY_VERSION, decode_policy=policy)
    assert new.seal != old.seal and new.destination.admission_id != old.destination.admission_id
    assert state._load_plan(new.to_json()) == new
    for change in ({"decode_policy": None}, {"version": old.version}):
        record = json.loads(new.to_json()) | change
        with pytest.raises((TransferRefusal, ValueError)):
            state._load_plan(json.dumps(record))
    record = json.loads(new.to_json())
    record["decode_policy"]["memory"]["reserve_bytes"] += 1
    with pytest.raises(TransferRefusal, match="ADMISSION_CORRUPT"):
        state._load_plan(json.dumps(record))
    assert old.to_json() == old_json


@pytest.mark.parametrize("kind", ["whole", "stream"])
def test_guarded_dispatch_never_joins_worker_output_and_closes_context(monkeypatch, kind):
    events = []
    @contextmanager
    def worker(source, **kwargs):
        events.append("open")
        try:
            yield iter((b"abc", b"def"))
        finally:
            events.append("reaped")
    monkeypatch.setattr(codec_supervisor, "guarded_zipnn_frame", worker)
    blob = b"ZN000" if kind == "whole" else container(b"unused")
    reader = original_stream(io.BytesIO(blob), compressed=True, expected_bytes=6, policy=qualified_policy())
    assert reader.read(2) == b"ab"
    assert events == ["open"]
    reader.close()
    assert events == ["open", "reaped"]


def test_local_source_reaps_before_descriptor_release_and_preserves_consumer_error(attachment, monkeypatch):
    from modelark.slice.local_source import LocalArchiveReader
    root, candidate, observer, _ = attachment
    events = []
    @contextmanager
    def worker(source, **kwargs):
        try:
            yield iter((b"originaldata",))
        finally:
            # A fresh attachment proof remains usable until helper cleanup ends.
            kwargs["check"]()
            events.append("reaped")
    monkeypatch.setattr(codec_supervisor, "guarded_zipnn_frame", worker)
    (root / "org/model/model.safetensors").write_bytes(b"ZN0000000000")
    candidate = replace(candidate, copy=replace(candidate.copy, compressed=True))
    error = OSError(28, "destination full")
    with pytest.raises(OSError) as caught:
        with LocalArchiveReader({"drive-a": root}, observer=observer,
                                policy=qualified_policy()).open(candidate) as reader:
            assert reader.read(1) == b"o"
            raise error
    assert caught.value is error
    assert events == ["reaped"]


@pytest.mark.parametrize("failure", ["STOPPED", "DESTINATION_CHANGED", "SOURCE_MISSING"])
def test_local_reader_idle_child_checks_and_reaps_before_context_exit(attachment, monkeypatch, failure):
    from modelark.slice.local_source import LocalArchiveReader
    from test_codec_supervisor import launch_fake, assert_reaped
    root, candidate, observer, _ = attachment
    frame = header(original=12) + b"abcd"
    (root / "org/model/model.safetensors").write_bytes(frame)
    candidate = replace(candidate, copy=replace(candidate.copy, compressed=True, stored_bytes=len(frame)))
    monkeypatch.setattr(codec_supervisor, "available_memory", lambda: {"available_bytes": 16 << 30})
    children = launch_fake(monkeypatch, "sys.stdin.buffer.read()\nwhile True: time.sleep(.02)")
    error = TransferRefusal(failure)
    def check():
        if children and children[0].stdin.closed:
            raise error
    reader = LocalArchiveReader({"drive-a": root}, observer=observer, policy=qualified_policy())
    with pytest.raises(TransferRefusal) as caught:
        with reader.open(candidate, check=check) as source:
            source.read(1)
    assert caught.value is error
    assert_reaped(children)


def test_initial_destination_boundary_error_is_not_reclassified_as_source(attachment):
    from modelark.slice.local_source import LocalArchiveReader
    root, candidate, observer, _ = attachment
    error = TransferRefusal("DESTINATION_CHANGED")
    def check():
        raise error
    with pytest.raises(TransferRefusal) as caught:
        with LocalArchiveReader({"drive-a": root}, observer=observer,
                                policy=qualified_policy()).open(candidate, check=check):
            pytest.fail("invalid destination boundary ignored")
    assert caught.value is error


@pytest.mark.parametrize("kind", ["whole", "stream"])
def test_real_guarded_container_original_hash(monkeypatch, kind):
    # Controlled admission sample: real child AS guard and original hash tested.
    monkeypatch.setattr(codec_supervisor, "available_memory", lambda: {"available_bytes": 16 << 30})
    original = bytes(range(128)) * 1024
    frame = bytes(streamznn._zipnn(dtype="bfloat16", threads=1).compress(bytearray(original)))
    blob = frame if kind == "whole" else container(frame, frame)
    expected = original if kind == "whole" else original * 2
    reader = original_stream(io.BytesIO(blob), compressed=True, expected_bytes=len(expected), policy=qualified_policy())
    digest = hashlib.sha256()
    try:
        while data := reader.read(1 << 20):
            assert len(data) <= 64 << 10
            digest.update(data)
    finally:
        reader.close()
    assert digest.hexdigest() == hashlib.sha256(expected).hexdigest()


@pytest.mark.parametrize("known_size", [False, True])
def test_new_zstd_policy_uses_bytes_while_legacy_approval_is_unchanged(known_size):
    zstd = pytest.importorskip("zstandard")
    data = bytes(256 << 10)
    blob = zstd.ZstdCompressor(write_content_size=known_size).compress(data)
    inspect(blob, len(data))
    new = original_stream(io.BytesIO(blob), compressed=True, expected_bytes=len(data), policy=qualified_policy())
    assert b"".join(iter(lambda: new.read(4096), b"")) == data
    if not known_size:
        legacy = original_stream(io.BytesIO(blob), compressed=True, expected_bytes=len(data))
        with pytest.raises(TransferRefusal, match="SOURCE_DECODE_INVALID"):
            b"".join(iter(lambda: legacy.read(4096), b""))


def test_direct_policy_binding_and_closed_world_reader(store):
    from modelark.slice import domain as d, transaction as t
    from test_slice_transaction import proposal
    preview, approval, _ = proposal()
    admission = {"version": "modelark.slice.direct.v2", "catalog": "/catalog.sqlite", "capacity": {},
                 "decode_policy": qualified_policy().to_record()}
    binding = t.DestinationBinding("test-device", "fs", "direct-v2:" + hashlib.sha256(d._json(admission)).hexdigest(),
                                   1_000_000)
    plan = t.TransferPlan(preview, binding, metadata_reserve_bytes=100_000)
    tx = store.create(plan, approval, admission=admission)
    assert store.load_admission(tx) == admission
    for bad in ({k: v for k, v in admission.items() if k != "decode_policy"},
                admission | {"decode_policy": None}, admission | {"version": "modelark.slice.direct.v1"},
                admission | {"unknown": True}):
        with pytest.raises(TransferRefusal, match="ADMISSION_CORRUPT"):
            store._validate_admission(plan, bad)
    admission["decode_policy"]["limits"]["decoded_frame_bytes"] -= 1
    with pytest.raises(TransferRefusal, match="ADMISSION_CORRUPT"):
        store._validate_admission(plan, admission)


@pytest.mark.parametrize("refusal", ["SOURCE_BLOCKED", "WAITING_SOURCE", "STOPPED"])
def test_preflight_refusal_never_creates_output_or_consumes_fat_attempt(fat, monkeypatch, refusal):
    from modelark.slice import transaction as t
    def preflight(proposal, check):
        check()
        assert not fat.store.attempt_consumed(fat.case.tx)
        assert not (fat.parent / "delivery").exists()
        if refusal == "STOPPED":
            fat.store.request_stop(fat.case.tx)
            check()
            pytest.fail("stop was ignored")
        raise TransferRefusal(refusal)
    monkeypatch.setattr(fat.case.sources, "preflight", preflight, raising=False)
    with pytest.raises(TransferRefusal, match=refusal):
        t.start(fat.store, fat.case.tx, fat.case.adapter, fat.case.sources)
    assert not fat.store.attempt_consumed(fat.case.tx)
    assert fat.store.events(fat.case.tx) == []
    assert not (fat.parent / "delivery").exists()
    # New explicit Start can retry the same approval after the read-only refusal.
    monkeypatch.setattr(fat.case.sources, "preflight", lambda proposal, check: check())
    with t.start(fat.store, fat.case.tx, fat.case.adapter, fat.case.sources) as session:
        assert session.run().state == "complete"


def test_stop_between_preflight_and_atomic_claim_does_not_spend_fat(fat, monkeypatch):
    from modelark.slice import transaction as t
    original = fat.store.claim
    def stopped_claim(*args):
        fat.store.request_stop(fat.case.tx)
        return original(*args)
    monkeypatch.setattr(fat.store, "claim", stopped_claim)
    monkeypatch.setattr(fat.case.sources, "preflight", lambda proposal, check: check(), raising=False)
    with pytest.raises(TransferRefusal, match="STOPPED"):
        t.start(fat.store, fat.case.tx, fat.case.adapter, fat.case.sources)
    assert not fat.store.attempt_consumed(fat.case.tx)
    assert not (fat.parent / "delivery").exists()
    monkeypatch.setattr(fat.store, "claim", original)
    with t.start(fat.store, fat.case.tx, fat.case.adapter, fat.case.sources) as session:
        assert session.run().state == "complete"


def test_changed_catalog_copy_is_refused_before_preflight_reads(archive, monkeypatch):
    from modelark.slice import domain as d
    from modelark.slice.catalog import read_catalog
    from test_slice_domain import spec
    path, con, _, reader, sources = archive
    proposal = d.preview(spec(d), read_catalog(path, spec(d)))
    con.execute("UPDATE archived SET stored_relpath='other.safetensors'")
    reader.policy = qualified_policy()
    monkeypatch.setattr(reader, "inspect", lambda *args, **kwargs: pytest.fail("stale candidate inspected"),
                        raising=False)
    with pytest.raises(TransferRefusal, match="SOURCE_BLOCKED"):
        sources.preflight(proposal)


def test_frame_bounds_come_from_shared_address_space_envelope():
    policy = qualified_policy()
    assert policy.limits.decoded_frame_bytes == policy.memory.address_space_bytes
    assert policy.limits.stored_frame_bytes == policy.memory.address_space_bytes


@pytest.mark.parametrize("outcome", ["complete", "stop", "unplug", "destination"])
def test_actual_fenced_archive_reader_reaps_child_before_fence_release(archive, attachment, monkeypatch, outcome):
    from modelark import drive_fence
    from modelark.drive_identity import FenceIdentity
    from modelark.slice import domain as d
    from modelark.slice.catalog import read_catalog
    from modelark.slice.local_source import LocalArchiveReader
    from modelark.slice.sources import FencedSources
    from test_slice_domain import spec
    path, con, _, _, _ = archive
    root, _, observer, _ = attachment
    original = b"originaldata"
    blob = bytes(streamznn._zipnn(dtype="bfloat16", threads=1).compress(bytearray(original)))
    (root / "org/model/model.safetensors").write_bytes(blob)
    digest = hashlib.sha256(original).hexdigest()
    key = f"SHA256E-s{len(blob)}--{hashlib.sha256(blob).hexdigest()}"
    con.execute("UPDATE files SET sha256=?", (digest,))
    con.execute("UPDATE archived SET compressed=1,stored_bytes=?,orig_sha256=?,annex_key=?",
                (len(blob), digest, key))
    proposal = d.preview(spec(d), read_catalog(path, spec(d)))
    candidate = proposal.closure[0].sources[0]
    drive = candidate.drive
    keys = FenceIdentity(drive.fs_uuid, drive.annex_uuid, drive.serial,
                         drive.filesystem_capacity_bytes, drive.identity_epoch,
                         drive.identity_fingerprint).lock_keys()
    def fence_held():
        with pytest.raises(drive_fence.FenceUnavailable):
            with drive_fence.hold_drives_sorted(keys, blocking=False):
                pytest.fail("source fence released before worker reaping")
    children, reaps = [], []
    real_launch = codec_supervisor._launch
    def launch(*args):
        child = real_launch(*args)
        children.append(child)
        wait = child.wait
        def reap(*args, **kwargs):
            fence_held()
            result = wait(*args, **kwargs)
            reaps.append(result)
            return result
        child.wait = reap
        return child
    monkeypatch.setattr(codec_supervisor, "_launch", launch)
    monkeypatch.setattr(codec_supervisor, "available_memory", lambda: {"available_bytes": 16 << 30})
    error = (OSError(28, "destination full") if outcome == "destination" else
             TransferRefusal("STOPPED" if outcome == "stop" else "SOURCE_MISSING"))
    def check():
        fence_held()
        if outcome in {"stop", "unplug"} and children and children[0].stdin.closed:
            raise error
    sources = FencedSources(path, LocalArchiveReader({"drive-a": root}, observer=observer,
                                                     policy=qualified_policy()))
    def consume():
        with sources.open_checked(candidate, check) as (_, source):
            data = source.read(1)
            if outcome == "destination":
                raise error
            data += b"".join(iter(lambda: source.read(4096), b""))
            assert hashlib.sha256(data).hexdigest() == proposal.closure[0].sha256
    if outcome == "complete":
        consume()
    else:
        with pytest.raises(type(error)) as caught:
            consume()
        assert caught.value is error
    assert children and reaps and all(child.poll() is not None for child in children)
    with drive_fence.hold_drives_sorted(keys, blocking=False):
        pass  # only now can an archive writer acquire the same real fence


def test_preflight_visits_all_alternatives_and_holds_source_fences(archive, monkeypatch):
    from modelark import drive_fence
    from modelark.drive_identity import FenceIdentity
    from modelark.slice import domain as d
    from modelark.slice.catalog import read_catalog
    from test_slice_domain import spec
    path, _, candidate, reader, sources = archive
    preview = d.preview(spec(d), read_catalog(path, spec(d)))
    # Keep the same sealed identity but exercise two admitted representations.
    artifact = preview.closure[0]
    alternative = replace(candidate, copy=replace(candidate.copy, compressed=not candidate.copy.compressed))
    preview = replace(preview, closure=(replace(artifact, sources=(candidate, alternative)),))
    visited = []
    reader.policy = qualified_policy()
    @contextmanager
    def inspect_candidate(source, check):
        check()
        drive = source.drive
        keys = FenceIdentity(drive.fs_uuid, drive.annex_uuid, drive.serial,
                             drive.filesystem_capacity_bytes, drive.identity_epoch,
                             drive.identity_fingerprint).lock_keys()
        with pytest.raises(drive_fence.FenceUnavailable):
            with drive_fence.hold_drives_sorted(keys, blocking=False):
                pytest.fail("preflight released source fence")
        visited.append(source)
        if source is candidate:
            raise TransferRefusal("SOURCE_DECODE_RESOURCE")
        yield None
    monkeypatch.setattr(reader, "inspect", inspect_candidate, raising=False)
    # Isolate this test's alternate representation from fresh-evidence matching.
    monkeypatch.setattr("modelark.slice.transaction._same_source", lambda *args: True)
    sources.preflight(preview)
    assert visited == [candidate, alternative]
    @contextmanager
    def absent(source, check):
        raise TransferRefusal("WAITING_SOURCE")
        yield
    monkeypatch.setattr(reader, "inspect", absent)
    with pytest.raises(TransferRefusal, match="WAITING_SOURCE"):
        sources.preflight(preview)


@pytest.mark.parametrize("backend,version", [("cffi", "0.25.0"), ("rust", "0.25.0"), ("cext", "0.23.0")])
def test_unqualified_zstd_runtime_refused_early(monkeypatch, backend, version):
    zstd = pytest.importorskip("zstandard")
    blob = zstd.ZstdCompressor().compress(bytes(256 << 10))
    monkeypatch.setattr(zstd, "backend", backend)
    monkeypatch.setattr(zstd, "__version__", version)
    with pytest.raises(artifact_io.DecodeError, match="UNSUPPORTED"):
        inspect(blob, 256 << 10)


@pytest.mark.parametrize("delta", [-1, 0, 1])
@pytest.mark.parametrize("known_size", [False, True])
def test_zstd_actual_window_below_at_above_bound(delta, known_size):
    zstd = pytest.importorskip("zstandard")
    original = bytes(256 << 10)
    blob = zstd.ZstdCompressor(write_content_size=known_size).compress(original)
    window = zstd.get_frame_parameters(blob).window_size
    base = qualified_policy()
    policy = replace(base, limits=replace(base.limits, zstd_window_bytes=window + delta))
    if delta < 0:
        with pytest.raises(artifact_io.DecodeError, match="LIMIT"):
            inspect(blob, len(original), policy=policy)
    else:
        inspect(blob, len(original), policy=policy)
        reader = original_stream(io.BytesIO(blob), compressed=True, expected_bytes=len(original), policy=policy)
        assert b"".join(iter(lambda: reader.read(4096), b"")) == original
