"""Strict existing-anchor proof recognition, without database or disk observation."""
from dataclasses import FrozenInstanceError
import json

import pytest

from modelark.capacity_evidence import identity_fingerprint_v1
from modelark.serial_identity import (
    SerialIdentityUnproven, is_legacy_serial_mismatch, recognize_legacy_anchor,
)


def _facts(fs_uuid='20A0-2FDD', annex_uuid='annex-uuid', serial='ZR16L100'):
    proof = {'v': 1, 'fs_uuid': fs_uuid, 'annex_uuid': annex_uuid, 'serial': None}
    fingerprint = identity_fingerprint_v1(
        fs_uuid=fs_uuid, annex_uuid=annex_uuid, serial=None, filesystem_capacity_bytes=1000,
    )
    return dict(fs_uuid=fs_uuid, annex_uuid=annex_uuid, serial=serial,
                fingerprint=fingerprint, filesystem_capacity_bytes=1000,
                anchor_identity_proof=json.dumps(proof), anchor_fingerprint=fingerprint,
                anchor_capacity_bytes=1000, anchor_authority='dedicated_local')


def _diagnostic(facts, live_fingerprint):
    return is_legacy_serial_mismatch(
        **{key: facts[key] for key in (
            'fs_uuid', 'annex_uuid', 'serial', 'fingerprint', 'filesystem_capacity_bytes',
        )}, live_fingerprint=live_fingerprint,
    )


@pytest.mark.parametrize('fs_uuid,annex_uuid', [('20A0-2FDD', 'annex-uuid'),
                                               ('fs-uuid', None), (None, 'annex-uuid')])
def test_valid_existing_proof_yields_exact_immutable_correction(fs_uuid, annex_uuid):
    facts = _facts(fs_uuid, annex_uuid)
    original = dict(facts)
    result = recognize_legacy_anchor(**facts)
    assert result.old_fingerprint == facts['fingerprint']
    assert result.new_fingerprint == identity_fingerprint_v1(
        fs_uuid=fs_uuid, annex_uuid=annex_uuid, serial='ZR16L100',
        filesystem_capacity_bytes=1000,
    )
    assert result.old_fingerprint != result.new_fingerprint
    assert facts == original
    with pytest.raises(FrozenInstanceError):
        result.new_fingerprint = 'replacement'


@pytest.mark.parametrize('proof', [
    '', '{', 'null', '[]', 'true', '1', '"text"',
    '{"v":1,"fs_uuid":"20A0-2FDD","annex_uuid":"annex-uuid","serial":null,"v":1}',
    '{"v":1,"fs_uuid":"20A0-2FDD","annex_uuid":"annex-uuid","serial":null,"serial":null}',
    '{"v":1,"fs_uuid":"20A0-2FDD","annex_uuid":"annex-uuid","serial":NaN}',
    '{"v":Infinity,"fs_uuid":"20A0-2FDD","annex_uuid":"annex-uuid","serial":null}',
    b'{"v":1}', None, {'v': 1},
])
def test_invalid_json_duplicate_keys_and_nonobject_proofs_refuse(proof):
    facts = _facts()
    facts['anchor_identity_proof'] = proof
    with pytest.raises(SerialIdentityUnproven):
        recognize_legacy_anchor(**facts)


@pytest.mark.parametrize('key,value', [
    ('v', True), ('v', 1.0), ('v', '1'), ('v', 2), ('v', None),
    ('serial', ''), ('serial', 'ZR16L100'), ('serial', False), ('serial', 0),
    ('fs_uuid', None), ('fs_uuid', ''), ('fs_uuid', True), ('fs_uuid', 1),
    ('fs_uuid', ['20A0-2FDD']), ('fs_uuid', ' 20A0-2FDD'), ('fs_uuid', '20a0-2fdd'),
    ('annex_uuid', None), ('annex_uuid', ''), ('annex_uuid', {}),
    ('extra', 'not-in-v1'), ('filesystem_capacity_bytes', 1000),
])
def test_proof_requires_exact_keys_types_null_serial_and_uuid_agreement(key, value):
    facts = _facts()
    proof = json.loads(facts['anchor_identity_proof'])
    proof[key] = value
    facts['anchor_identity_proof'] = json.dumps(proof)
    with pytest.raises(SerialIdentityUnproven):
        recognize_legacy_anchor(**facts)


@pytest.mark.parametrize('key', ['v', 'fs_uuid', 'annex_uuid', 'serial'])
def test_no_missing_proof_fields_are_inferred(key):
    facts = _facts()
    proof = json.loads(facts['anchor_identity_proof'])
    del proof[key]
    facts['anchor_identity_proof'] = json.dumps(proof)
    with pytest.raises(SerialIdentityUnproven):
        recognize_legacy_anchor(**facts)


@pytest.mark.parametrize('key,value', [
    ('serial', None), ('serial', ''), ('serial', ' ZR16L100'), ('serial', 'ZR16L100\n'),
    ('serial', False), ('serial', 123), ('fs_uuid', ''), ('annex_uuid', []),
    ('serial', '\ud800'), ('fs_uuid', '\udfff'),
    ('filesystem_capacity_bytes', True), ('filesystem_capacity_bytes', 1000.0),
    ('filesystem_capacity_bytes', '1000'), ('filesystem_capacity_bytes', 0),
    ('filesystem_capacity_bytes', -1), ('filesystem_capacity_bytes', 1001),
    ('anchor_capacity_bytes', True), ('anchor_capacity_bytes', 1000.0),
    ('anchor_capacity_bytes', 0), ('anchor_capacity_bytes', -1), ('anchor_capacity_bytes', 1001),
    ('anchor_authority', None), ('anchor_authority', 'unknown'), ('anchor_authority', 'shared'),
    ('fingerprint', None), ('fingerprint', 'f' * 64), ('anchor_fingerprint', None),
    ('anchor_fingerprint', 'f' * 64),
])
def test_saved_or_anchor_fact_contradictions_refuse(key, value):
    facts = _facts()
    facts[key] = value
    with pytest.raises(SerialIdentityUnproven):
        recognize_legacy_anchor(**facts)


def test_both_saved_uuids_absent_refuses_even_if_anchor_claims_them():
    facts = _facts()
    facts.update(fs_uuid=None, annex_uuid=None)
    with pytest.raises(SerialIdentityUnproven):
        recognize_legacy_anchor(**facts)


def test_excessively_deep_proof_is_a_typed_refusal():
    facts = _facts()
    facts['anchor_identity_proof'] = '[' * 2000 + 'null' + ']' * 2000
    with pytest.raises(SerialIdentityUnproven):
        recognize_legacy_anchor(**facts)


def test_uppercase_hash_and_already_correct_hash_are_not_legacy_proof():
    facts = _facts()
    result = recognize_legacy_anchor(**facts)
    for fingerprint in (result.old_fingerprint.upper(), result.new_fingerprint):
        with pytest.raises(SerialIdentityUnproven):
            recognize_legacy_anchor(**{**facts, 'fingerprint': fingerprint,
                                       'anchor_fingerprint': fingerprint})


def test_serial_spelling_is_preserved_not_case_folded_or_substituted():
    upper = recognize_legacy_anchor(**_facts(serial='ZR16L100'))
    lower = recognize_legacy_anchor(**_facts(serial='zr16l100'))
    assert upper.old_fingerprint == lower.old_fingerprint
    assert upper.new_fingerprint != lower.new_fingerprint


def test_diagnostic_does_not_stand_in_for_anchor_proof():
    facts = _facts()
    result = recognize_legacy_anchor(**facts)
    facts['anchor_identity_proof'] = 'missing-proof'
    assert _diagnostic(facts, result.new_fingerprint)
    with pytest.raises(SerialIdentityUnproven):
        recognize_legacy_anchor(**facts)
    assert not _diagnostic(facts, result.old_fingerprint)
    assert not _diagnostic(facts, 'f' * 64)
    assert not _diagnostic({**facts, 'serial': None}, result.new_fingerprint)
    assert not _diagnostic({**facts, 'fingerprint': result.new_fingerprint}, result.new_fingerprint)
    assert not _diagnostic({**facts, 'filesystem_capacity_bytes': 2000}, result.new_fingerprint)
