"""Real repair and consumers on disposable archives with synthetic host inventory."""
from dataclasses import replace
import json
import sqlite3

import pytest

from modelark import drive_bootstrap as bootstrap, fetch, plan, proposal, register
from modelark.serial_evidence import load_bridge_binding
from modelark.slice.local_source import LocalArchiveReader
from modelark.slice.transaction import TransferRefusal
from test_serial_repair_public_slice import (
    workflow as _workflow, hardware as _hardware, native as _native,
    _candidate, _preview, _complete, _sources, REPO, PAYLOAD,
)

workflow, hardware, native = _workflow, _hardware, _native
pytestmark = pytest.mark.parametrize('workflow', ['bridge'], indirect=True)


@pytest.fixture
def bridge(workflow, hardware):
    case = workflow
    case.raw = hardware[3]['serial']
    case.encoded_fp = case.con.execute('SELECT identity_fingerprint FROM drives').fetchone()[0]
    return case


def repair(case):
    intent = bootstrap.inspect_serial_identity(case.con, 'drive-00')
    assert intent['status'] == 'bridge_encoded_clean'
    return bootstrap.repair_serial_identity(case.con, 'drive-00', expected_binding=intent['binding'],
                                           now='2026-09-12', writers_stopped=True)


def corrupt(con, statement):
    # Deliberately damaged disposable evidence only. Normal fixtures/production
    # retain these guards; successful repair is tested with append-only triggers.
    con.execute('DROP TRIGGER drive_clean_anchors_no_update')
    con.execute('DROP TRIGGER drive_clean_anchors_no_delete')
    con.execute('DROP TRIGGER drive_dirty_generations_no_update')
    con.execute(statement)


def test_bridge_repair_preserves_history_then_public_slice_completes(bridge):
    case = bridge
    before = tuple(case.con.iterdump())
    old_anchor = case.con.execute('SELECT * FROM drive_clean_anchors').fetchone()
    assert not bootstrap._live_evidence(case.con, 'drive-00').proven
    assert not fetch._observe_drive(case.con, 'drive-00').identity_proven
    with pytest.raises(proposal.Refusal) as refused:
        proposal._fence_facts(case.con, ['drive-00'])
    assert refused.value.code == 'DRIVE_SERIAL_REPAIR_REQUIRED'
    assert refused.value.evidence['drive'] == 'drive-00'
    assert 'reconcile_drive' not in refused.value.actions
    with pytest.raises(bootstrap.DriveMutationRefused, match='DRIVE_SERIAL_REPAIR_REQUIRED'):
        bootstrap.reconcile_drive(case.con, 'drive-00', now='2026-09-12', dedicated=True)
    assert tuple(case.con.iterdump()) == before

    result = repair(case)

    assert result['canonical_fingerprint'] == case.canonical
    assert result['observed_serial'] == case.raw
    assert result['generation'] == 2
    assert result['legacy_recovered'] is False
    assert case.con.execute('SELECT serial FROM drives').fetchone() == (case.serial,)
    assert case.con.execute('SELECT * FROM drive_clean_anchors WHERE generation=1').fetchone() == old_anchor
    with sqlite3.connect(result['backup']) as backup:
        assert tuple(backup.iterdump()) == before
    identity, fence = case.con.execute(
        'SELECT identity_proof,fence_proof FROM drive_clean_anchors WHERE generation=2').fetchone()
    assert json.loads(identity)['serial'] == case.serial
    assert json.loads(fence)['serial'] == case.raw
    assert proposal._fence_facts(case.con, ['drive-00'])
    for observation in (bootstrap._live_evidence(case.con, 'drive-00').observation(),
                        fetch._observe_drive(case.con, 'drive-00')):
        assert observation.identity_proven
        assert observation.fingerprint == case.canonical
        assert json.loads(observation.identity_proof)['serial'] == case.serial
        assert json.loads(observation.fence_proof)['serial'] == case.raw

    # Standalone readers have no repair authority. The fenced gate must resolve
    # the unique transition against the exact source anchor in this new seal.
    candidate = _candidate(case)
    with pytest.raises(TransferRefusal, match='SOURCE_CHANGED'):
        with LocalArchiveReader({'drive-00': case.archive}, observer=case.observer).open(candidate):
            pytest.fail('unbound reader admitted an encoded serial')
    with _sources(case).open(candidate) as (_, stream):
        assert stream.read(len(PAYLOAD)) == PAYLOAD
    preview = _preview(case, 'bridge-delivery')
    _complete(case, preview, case.parent / 'bridge-delivery')
    assert {str(p.relative_to(case.archive)): p.read_bytes()
            for p in case.archive.rglob('*') if p.is_file()} == case.archive_bytes
    fresh = bootstrap.inspect_serial_identity(case.con, 'drive-00')
    assert bootstrap.repair_serial_identity(case.con, 'drive-00', expected_binding=fresh['binding'],
                                           now='2026-09-12', writers_stopped=True)['status'] == 'already_correct'


def test_same_bridge_reconciliation_and_public_fill_approval(bridge):
    case = bridge
    repair(case)
    result = bootstrap.reconcile_drive(case.con, 'drive-00', now='2026-09-12',
                                       dedicated=True, accept_drift=True)
    assert result.outcome in {'refreshed', 'drift_accepted'}
    plan.create(case.con, 'ark', name='Bridge qualification')
    plan.add_drive(case.con, 'ark', 'drive-00')
    plan.set_active(case.con, 'ark')
    case.con.execute('UPDATE models SET numcopies=1')
    case.con.execute("INSERT INTO selection(repo_id,finalized_at) VALUES(?,'2026-09-12')", [REPO])
    preview = proposal.preview_pure(case.con, 'ark')
    assert preview['header']['gate_b_code'] == 'FEASIBLE'
    draft = proposal.publish_draft(case.con, preview)
    proposal.approve(case.con, draft['proposal_id'], services=proposal._DefaultServices())
    assert proposal.load_proposal(case.con, draft['proposal_id'])['lifecycle'] == 'approved'


@pytest.mark.parametrize('change', ['dirty', 'other-uuid', 'missing-anchor', 'prior-repair'])
def test_unsupported_historical_case_refuses_without_changes(bridge, change):
    case = bridge
    if change == 'dirty':
        case.con.execute("INSERT INTO drive_dirty_generations(drive_label,identity_epoch,generation,operation_code) "
                         "VALUES('drive-00',1,2,'test')")
        case.con.execute('UPDATE drives SET write_generation=2')
    elif change == 'other-uuid':
        case.con.execute("UPDATE drives SET fs_uuid='other'")
    elif change == 'missing-anchor':
        corrupt(case.con, 'DELETE FROM drive_clean_anchors')
    else:
        corrupt(case.con, "UPDATE drive_dirty_generations SET operation_code='serial_identity_repair'")
    before = tuple(case.con.iterdump())
    with pytest.raises(bootstrap.DriveMutationRefused, match='DRIVE_SERIAL_REPAIR_UNPROVEN'):
        bootstrap.inspect_serial_identity(case.con, 'drive-00')
    assert tuple(case.con.iterdump()) == before


@pytest.mark.parametrize('field,value', [('serial', 'different'), ('fs_uuid', 'other'),
                                      ('annex_uuid', 'other'), ('capacity_bytes', 1000)])
def test_changed_final_attachment_refuses_after_backup(bridge, monkeypatch, field, value):
    case = bridge
    original = register.observe_archive_volume
    calls = []

    def observe(path):
        calls.append(path)
        observed = original(path)
        return replace(observed, **{field: value}) if len(calls) >= 3 else observed

    monkeypatch.setattr(register, 'observe_archive_volume', observe)
    before = tuple(case.con.iterdump())
    with pytest.raises(bootstrap.DriveMutationRefused, match='DRIVE_SERIAL_REPAIR_LIVE_MISMATCH'):
        repair(case)
    assert len(calls) >= 3
    assert tuple(case.con.iterdump()) == before


@pytest.mark.parametrize('mutation', [
    "UPDATE drive_clean_anchors SET anchor_id=anchor_id+20 WHERE generation=2",
    "UPDATE drive_clean_anchors SET identity_proof='{}' WHERE generation=1",
    "UPDATE drive_clean_anchors SET fence_proof='{}' WHERE generation=2",
    "UPDATE drive_dirty_generations SET operation_code='not-a-repair' WHERE generation=2",
    "INSERT INTO drive_dirty_generations(drive_label,identity_epoch,generation,operation_code) "
    "VALUES('drive-00',1,3,'serial_identity_repair')",
])
def test_sealed_source_cannot_use_changed_or_ambiguous_authority(bridge, mutation):
    case = bridge
    repair(case)
    candidate = _candidate(case)
    corrupt(case.con, mutation)
    with pytest.raises(TransferRefusal):
        with _sources(case).open(candidate):
            pytest.fail('changed proof admitted source')


def test_later_generation_keeps_one_transition_but_old_seal_does_not_rebind(bridge):
    case = bridge
    repair(case)
    old_candidate = _candidate(case)
    from modelark import drive_mutation as dm
    generation = dm.begin_generation(case.con, 'drive-00', 'test-later-write')
    observation = bootstrap._live_evidence(case.con, 'drive-00').observation()
    assert observation.identity_proven
    dm._publish_anchor_locked(case.con, 'drive-00', 1, generation, observation, '2026-09-12')
    assert load_bridge_binding(case.con, 'drive-00', sealed_source=old_candidate) is None
    with pytest.raises(TransferRefusal):
        with _sources(case).open(old_candidate):
            pytest.fail('old sealed generation was rebound')
    with _sources(case).open(_candidate(case)) as (_, stream):
        assert stream.read(len(PAYLOAD)) == PAYLOAD


@pytest.mark.parametrize('key', ['encoded_fp', 'canonical', 'old'])
def test_repair_holds_old_canonical_and_null_exclusion_keys(bridge, key):
    from modelark import drive_fence
    before = tuple(bridge.con.iterdump())
    with drive_fence.hold_drives_sorted([(getattr(bridge, key), 1)], blocking=False):
        with pytest.raises(bootstrap.DriveMutationRefused, match='DRIVE_FENCE_UNAVAILABLE'):
            repair(bridge)
    assert tuple(bridge.con.iterdump()) == before


def test_literal_bridge_readout_is_recorded_literally_after_repair(bridge, hardware):
    repair(bridge)
    hardware[3]['serial'] = bridge.serial
    observed = bootstrap._live_evidence(bridge.con, 'drive-00').observation()
    assert observed.identity_proven
    assert json.loads(observed.fence_proof)['serial'] == bridge.serial
    assert json.loads(observed.identity_proof)['serial'] == bridge.serial
    hardware[3]['serial'] = bridge.raw.lower()
    # Hex-digit case is recognized only in the initial historical proof.
    # A different fresh spelling is not an automatically authorized alias.
    if bridge.raw.lower() != bridge.raw:
        assert not bootstrap._live_evidence(bridge.con, 'drive-00').proven


def test_hash_rewrite_without_repair_does_not_authorize_matching(bridge):
    bridge.con.execute('UPDATE drives SET identity_fingerprint=?', [bridge.canonical])
    assert not bootstrap._live_evidence(bridge.con, 'drive-00').proven
    assert not fetch._observe_drive(bridge.con, 'drive-00').identity_proven
