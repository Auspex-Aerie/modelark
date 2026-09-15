"""Real directory fences, synthetic catalog and explicitly simulated hardware.

Only mount resolution, live UUID probes and lsblk/mount inventories are mocked.
The shared retained-directory observer, whole-parent traversal, serial/bridge
policy, real statvfs, BoundTree and catalog/scope checks execute normally. These
tests are not a physical USB qualification or native gitdir/profile proof.
"""
from dataclasses import FrozenInstanceError, replace
import json
import os
from types import SimpleNamespace

import pytest

from modelark import block_identity, publication_attachment as attachment, publication_locks, register
from modelark.capacity_evidence import identity_fingerprint_v1
from modelark.publication_policy import PublicationRefused
from test_publication_lifecycle import MAP
from test_publication_lifecycle import con as publication_connection  # noqa: F401

SERIAL = "VR1KV4LK"


@pytest.fixture(name="con")
def _connection(request):
    return request.getfixturevalue("publication_connection")


@pytest.fixture
def hardware(con, tmp_path, monkeypatch):
    root = tmp_path / "mounted" / "modelark"
    root.mkdir(parents=True)
    (root / "sentinel").write_bytes(b"archive unchanged\n")
    fs, annex = con.execute("SELECT fs_uuid,annex_uuid FROM drives WHERE drive_label='d0'").fetchone()
    stat = os.statvfs(root)
    capacity = stat.f_blocks * stat.f_frsize
    fingerprint = identity_fingerprint_v1(fs_uuid=fs, annex_uuid=annex, serial=SERIAL,
                                           filesystem_capacity_bytes=capacity)
    con.execute("UPDATE drives SET serial=?,filesystem_capacity_bytes=?,identity_fingerprint=? "
                "WHERE drive_label='d0'", [SERIAL, capacity, fingerprint])
    state = SimpleNamespace(root=root, fs=fs, annex=annex, capacity=capacity, serial=SERIAL,
                            partition_serial="WRONG-PARTITION-SERIAL", mount="8:1")
    def inventory():
        return {"blockdevices": [{"name": "/dev/sda", "path": "/dev/sda", "type": "disk",
                                  "maj:min": "8:0", "serial": state.serial,
                                  "children": [{"name": "/dev/sda1", "path": "/dev/sda1", "type": "part",
                                                "maj:min": "8:1", "uuid": state.fs,
                                                "serial": state.partition_serial}]}]}
    monkeypatch.setattr(register, "archive_path", lambda *_: state.root)
    monkeypatch.setattr(register, "probe_fs_uuid", lambda *_: state.fs)
    monkeypatch.setattr(register, "probe_annex_uuid", lambda *_: state.annex)
    monkeypatch.setattr(block_identity, "_mounted_device", lambda *_: state.mount)
    monkeypatch.setattr(block_identity, "read_inventory", inventory)
    return state


def test_real_attachment_observer_uses_parent_serial_and_preserves_catalog(con, hardware):
    before = tuple(con.iterdump())
    with publication_locks.hold(con, ["d0"], map_uuid=MAP) as scope:
        proof = attachment.capture(scope, "d0")
        fresh = attachment.recheck(scope, proof)
        assert proof.root == str(hardware.root)
        assert proof.serial == proof.observed_serial == SERIAL
        assert proof.mount_id > 0 and len(proof.root_identity) == 3
        assert proof.generation == 2 and proof.identity_epoch == 1
        assert proof.filesystem_capacity_bytes == hardware.capacity
        assert fresh.root_identity == proof.root_identity
        assert json.loads(json.dumps(proof.record())) == proof.record()
        assert len(proof.digest) == 64
        with pytest.raises(FrozenInstanceError):
            proof.root = "/different"
    assert tuple(con.iterdump()) == before
    assert (hardware.root / "sentinel").read_bytes() == b"archive unchanged\n"
    assert sorted(path.name for path in hardware.root.iterdir()) == ["sentinel"]


@pytest.mark.parametrize("field,value,error", [
    ("serial", "OTHER-PARENT", "SERIAL_UNPROVEN"),
    ("serial", None, "SERIAL_UNPROVEN"),
    ("fs", "other-fs", "IDENTITY_MISMATCH"),
    ("fs", None, "IDENTITY_MISMATCH"),
    ("annex", "00000000-0000-4000-8000-000000000000", "IDENTITY_MISMATCH"),
    ("annex", None, "IDENTITY_MISMATCH"),
    ("mount", "99:99", "ATTACHMENT_UNPROVEN"),
])
def test_wrong_parent_uuid_or_unobservable_mount_refuses(con, hardware, field, value, error):
    setattr(hardware, field, value)
    # A matching partition serial must never rescue the wrong whole-parent disk.
    hardware.partition_serial = SERIAL
    before = tuple(con.iterdump())
    with publication_locks.hold(con, ["d0"], map_uuid=MAP) as scope:
        with pytest.raises(PublicationRefused, match=error):
            attachment.capture(scope, "d0")
    assert tuple(con.iterdump()) == before


def test_observed_serial_does_not_enrich_registered_serialless_identity(con, hardware):
    fingerprint = identity_fingerprint_v1(fs_uuid=hardware.fs, annex_uuid=hardware.annex, serial=None,
                                           filesystem_capacity_bytes=hardware.capacity)
    con.execute("UPDATE drives SET serial=NULL,identity_fingerprint=? WHERE drive_label='d0'", [fingerprint])
    with publication_locks.hold(con, ["d0"], map_uuid=MAP) as scope:
        proof = attachment.capture(scope, "d0")
        assert proof.serial is None and proof.observed_serial == SERIAL
    assert con.execute("SELECT serial FROM drives WHERE drive_label='d0'").fetchone() == (None,)


def test_known_serial_legacy_null_fingerprint_is_not_silently_repaired(con, hardware):
    fingerprint = identity_fingerprint_v1(fs_uuid=hardware.fs, annex_uuid=hardware.annex, serial=None,
                                           filesystem_capacity_bytes=hardware.capacity)
    con.execute("UPDATE drives SET identity_fingerprint=? WHERE drive_label='d0'", [fingerprint])
    with publication_locks.hold(con, ["d0"], map_uuid=MAP) as scope:
        with pytest.raises(PublicationRefused, match="IDENTITY_MISMATCH"):
            attachment.capture(scope, "d0")


def test_capacity_change_refuses_without_rewriting_catalog(con, hardware, monkeypatch):
    original = register.observe_archive_volume
    monkeypatch.setattr(register, "observe_archive_volume",
                        lambda path: replace(original(path), capacity_bytes=hardware.capacity + 4096))
    with publication_locks.hold(con, ["d0"], map_uuid=MAP) as scope:
        with pytest.raises(PublicationRefused, match="IDENTITY_MISMATCH"):
            attachment.capture(scope, "d0")


def test_free_space_drift_is_fresh_observation_not_attachment_conflict(con, hardware, monkeypatch):
    with publication_locks.hold(con, ["d0"], map_uuid=MAP) as scope:
        proof = attachment.capture(scope, "d0")
        original = register.observe_archive_volume
        monkeypatch.setattr(register, "observe_archive_volume",
                            lambda path: replace(original(path), free_bytes=proof.free_bytes // 2))
        fresh = attachment.recheck(scope, proof)
        assert fresh.free_bytes == proof.free_bytes // 2
        assert replace(fresh, free_bytes=proof.free_bytes) == proof


def test_detachment_during_parent_serial_observation_refuses(con, hardware, monkeypatch):
    original = register.probe_serial
    def detach(path):
        serial = original(path)
        hardware.root.rename(hardware.root.with_name("detached"))
        hardware.root.mkdir()
        return serial
    monkeypatch.setattr(register, "probe_serial", detach)
    with publication_locks.hold(con, ["d0"], map_uuid=MAP) as scope:
        with pytest.raises(PublicationRefused, match="ATTACHMENT_UNPROVEN"):
            attachment.capture(scope, "d0")


def test_recheck_does_not_redirect_to_new_path_with_matching_uuid(con, hardware):
    with publication_locks.hold(con, ["d0"], map_uuid=MAP) as scope:
        proof = attachment.capture(scope, "d0")
        hardware.root = hardware.root.with_name("different-archive")
        hardware.root.mkdir()
        with pytest.raises(PublicationRefused, match="ATTACHMENT_CHANGED"):
            attachment.recheck(scope, proof)


def test_recheck_refuses_replaced_inode_at_same_path(con, hardware):
    with publication_locks.hold(con, ["d0"], map_uuid=MAP) as scope:
        proof = attachment.capture(scope, "d0")
        hardware.root.rename(hardware.root.with_name("old-archive"))
        hardware.root.mkdir()
        with pytest.raises(PublicationRefused, match="ATTACHMENT_CHANGED"):
            attachment.recheck(scope, proof)


def test_path_resolution_change_inside_observation_refuses(con, hardware, monkeypatch):
    other = hardware.root.with_name("other")
    other.mkdir()
    calls = 0
    def resolve(*_):
        nonlocal calls
        calls += 1
        return hardware.root if calls == 1 else other
    monkeypatch.setattr(register, "archive_path", resolve)
    with publication_locks.hold(con, ["d0"], map_uuid=MAP) as scope:
        with pytest.raises(PublicationRefused, match="PATH_CHANGED"):
            attachment.capture(scope, "d0")


@pytest.mark.parametrize("kind", ["unmounted", "missing", "relative", "symlink"])
def test_missing_or_unconfined_archive_refuses(con, hardware, kind):
    if kind == "unmounted":
        hardware.root = None
    elif kind == "missing":
        hardware.root = hardware.root.with_name("missing")
    elif kind == "relative":
        hardware.root = "relative/modelark"
    else:
        link = hardware.root.with_name("alias")
        link.symlink_to(hardware.root, target_is_directory=True)
        hardware.root = link
    with publication_locks.hold(con, ["d0"], map_uuid=MAP) as scope:
        with pytest.raises(PublicationRefused):
            attachment.capture(scope, "d0")


def test_scope_and_transaction_required_before_hardware_io(con, hardware, monkeypatch):
    def forbidden(*_):
        pytest.fail("hardware IO before authority check")
    monkeypatch.setattr(register, "archive_path", forbidden)
    with pytest.raises(PublicationRefused, match="AUTHORITY_MISSING"):
        attachment.capture(lambda: None, "d0")
    with publication_locks.hold(con, ["d0"], map_uuid=MAP) as scope:
        with pytest.raises(PublicationRefused, match="PARTICIPANT_UNSELECTED"):
            attachment.capture(scope, "d1")
        con.execute("BEGIN")
        try:
            with pytest.raises(PublicationRefused, match="IO_TRANSACTION_ACTIVE"):
                attachment.capture(scope, "d0")
        finally:
            con.rollback()
    with pytest.raises(PublicationRefused, match="AUTHORITY_MISSING"):
        attachment.capture(scope, "d0")


def _bridge_history(con, hardware, *, ambiguous=False):
    raw = SERIAL.encode().hex().upper()
    canonical = {"v": 1, "fs_uuid": hardware.fs, "annex_uuid": hardware.annex, "serial": SERIAL}
    encoded = dict(canonical, serial=raw)
    old = identity_fingerprint_v1(fs_uuid=hardware.fs, annex_uuid=hardware.annex, serial=raw,
                                   filesystem_capacity_bytes=hardware.capacity)
    new = identity_fingerprint_v1(fs_uuid=hardware.fs, annex_uuid=hardware.annex, serial=SERIAL,
                                   filesystem_capacity_bytes=hardware.capacity)
    for generation, operation in [(3, "old-bridge"), (4, "serial_identity_repair"), (5, "publication-fixture")]:
        con.execute("INSERT INTO drive_dirty_generations(drive_label,identity_epoch,generation,operation_code) "
                    "VALUES('d0',1,?,?)", [generation, operation])
    for generation, fingerprint, identity in [(3, old, encoded), (4, new, canonical)]:
        con.execute("INSERT INTO drive_clean_anchors(drive_label,identity_epoch,generation,anchor_free_bytes,"
                    "filesystem_capacity_bytes,identity_fingerprint,write_authority,identity_proof,fence_proof,"
                    "observed_at) VALUES('d0',1,?,100,?,?,'dedicated_local',?,?,'fixture')",
                    [generation, hardware.capacity, fingerprint, json.dumps(identity), json.dumps(encoded)])
    con.execute("UPDATE drives SET write_generation=5 WHERE drive_label='d0'")
    if ambiguous:
        con.execute("INSERT INTO drive_dirty_generations(drive_label,identity_epoch,generation,operation_code) "
                    "VALUES('d0',1,6,'serial_identity_repair')")
        con.execute("UPDATE drives SET write_generation=6 WHERE drive_label='d0'")
    hardware.serial = raw


def test_exact_append_only_bridge_transition_is_used_not_generic_hex_decoding(con, hardware):
    raw = SERIAL.encode().hex().upper()
    hardware.serial = raw
    with publication_locks.hold(con, ["d0"], map_uuid=MAP) as scope:
        with pytest.raises(PublicationRefused, match="SERIAL_UNPROVEN"):
            attachment.capture(scope, "d0")
    _bridge_history(con, hardware)
    before = tuple(con.iterdump())
    with publication_locks.hold(con, ["d0"], map_uuid=MAP) as scope:
        proof = attachment.capture(scope, "d0")
        assert proof.serial == SERIAL and proof.observed_serial == raw and proof.generation == 5
        attachment.recheck(scope, proof)
    assert tuple(con.iterdump()) == before


def test_ambiguous_bridge_history_never_grants_serial_alias(con, hardware):
    _bridge_history(con, hardware, ambiguous=True)
    with publication_locks.hold(con, ["d0"], map_uuid=MAP) as scope:
        with pytest.raises(PublicationRefused, match="SERIAL_UNPROVEN"):
            attachment.capture(scope, "d0")


def test_vanished_kernel_mount_record_refuses_without_unmounting_hardware(con, hardware, monkeypatch):
    from modelark.slice import linux
    with publication_locks.hold(con, ["d0"], map_uuid=MAP) as scope:
        proof = attachment.capture(scope, "d0")
        # Explicit simulation only; never unmount the actual host filesystem.
        monkeypatch.setattr(linux, "_mount_ids", lambda: frozenset())
        with pytest.raises(PublicationRefused, match="ATTACHMENT_UNPROVEN"):
            attachment.recheck(scope, proof)


def test_recheck_proof_bound_to_exact_mount_id(con, hardware):
    with publication_locks.hold(con, ["d0"], map_uuid=MAP) as scope:
        proof = attachment.capture(scope, "d0")
        with pytest.raises(PublicationRefused, match="ATTACHMENT_CHANGED"):
            attachment.recheck(scope, replace(proof, mount_id=proof.mount_id + 1))


def test_current_generation_change_requires_new_binding(con, hardware):
    with publication_locks.hold(con, ["d0"], map_uuid=MAP) as scope:
        proof = attachment.capture(scope, "d0")
        con.execute("UPDATE drives SET write_generation=3 WHERE drive_label='d0'")
        with pytest.raises(PublicationRefused, match="ATTACHMENT_CHANGED"):
            attachment.recheck(scope, proof)


def test_catalog_generation_changed_during_observation_refuses(con, hardware, monkeypatch):
    original = register.observe_archive_volume
    def changed(path):
        result = original(path)
        con.execute("UPDATE drives SET write_generation=3 WHERE drive_label='d0'")
        return result
    monkeypatch.setattr(register, "observe_archive_volume", changed)
    with publication_locks.hold(con, ["d0"], map_uuid=MAP) as scope:
        with pytest.raises(PublicationRefused, match="CATALOG_CHANGED"):
            attachment.capture(scope, "d0")


@pytest.mark.parametrize("column,value", [("write_authority", "unknown"), ("lifecycle", "lost")])
def test_nonmanaged_or_inactive_drive_refuses_before_physical_io(con, hardware, monkeypatch, column, value):
    con.execute(f"UPDATE drives SET {column}=? WHERE drive_label='d0'", [value])
    def forbidden(*_):
        pytest.fail("hardware IO for inadmissible lifecycle")
    monkeypatch.setattr(register, "archive_path", forbidden)
    with publication_locks.hold(con, ["d0"], map_uuid=MAP) as scope:
        with pytest.raises(PublicationRefused, match="AUTHORITY_UNPROVEN"):
            attachment.capture(scope, "d0")


def test_new_registered_generation_zero_is_observable_but_not_a_dirty_generation(con, hardware):
    hardware.fs = "fresh-filesystem"
    hardware.annex = "77777777-7777-4777-8777-777777777777"
    fingerprint = identity_fingerprint_v1(fs_uuid=hardware.fs, annex_uuid=hardware.annex, serial=SERIAL,
                                           filesystem_capacity_bytes=hardware.capacity)
    con.execute("INSERT INTO drives(drive_label,fs_uuid,annex_uuid,serial,filesystem_capacity_bytes,"
                "identity_epoch,identity_fingerprint,write_generation,write_authority,lifecycle) "
                "VALUES('fresh',?,?,?, ?,1,?,0,'dedicated_local','active')",
                [hardware.fs, hardware.annex, SERIAL, hardware.capacity, fingerprint])
    before = tuple(con.iterdump())
    with publication_locks.hold(con, ["fresh"], map_uuid=MAP) as scope:
        proof = attachment.capture(scope, "fresh")
        assert proof.generation == 0 and proof.identity_epoch == 1
        assert attachment.recheck(scope, proof).generation == 0
    assert tuple(con.iterdump()) == before
    assert con.execute("SELECT generation FROM drive_dirty_generations WHERE drive_label='fresh'").fetchall() == []
    assert con.execute("SELECT generation FROM drive_clean_anchors WHERE drive_label='fresh'").fetchall() == []


def test_generation_zero_proof_cannot_be_reused_after_lifecycle_advance(con, hardware):
    # Synthetic pre-operation state; this observer never advances any generation.
    con.execute("UPDATE drives SET write_generation=0 WHERE drive_label='d0'")
    with publication_locks.hold(con, ["d0"], map_uuid=MAP) as scope:
        before = attachment.capture(scope, "d0")
        con.execute("UPDATE drives SET write_generation=1 WHERE drive_label='d0'")
        with pytest.raises(PublicationRefused, match="ATTACHMENT_CHANGED"):
            attachment.recheck(scope, before)
        assert attachment.capture(scope, "d0").generation == 1
