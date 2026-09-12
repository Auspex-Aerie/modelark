"""Historical encoding recognition is proof-only, not runtime serial equality."""
import json

import pytest

from modelark.capacity_evidence import identity_fingerprint_v1
from modelark.serial_identity import (
    SerialIdentityUnproven, recognize_bridge_anchor, serial_for_identity,
)


SERIAL = 'VR1KV4LK'
RAW = '5652314B56344C4B'


def facts(raw=RAW):
    proof = json.dumps(dict(v=1, fs_uuid='fs-1', annex_uuid='annex-1', serial=raw))
    fingerprint = identity_fingerprint_v1(fs_uuid='fs-1', annex_uuid='annex-1',
                                          serial=raw, filesystem_capacity_bytes=1000)
    return dict(fs_uuid='fs-1', annex_uuid='annex-1', serial=SERIAL, fingerprint=fingerprint,
                filesystem_capacity_bytes=1000, anchor_identity_proof=proof,
                anchor_fence_proof=proof, anchor_fingerprint=fingerprint,
                anchor_capacity_bytes=1000, anchor_authority='dedicated_local')


@pytest.mark.parametrize('raw', [RAW, RAW.lower()])
def test_exact_pair_recognized_with_raw_spelling_preserved(raw):
    original = facts(raw)
    result = recognize_bridge_anchor(**original)
    assert result.observed_serial == raw
    assert result.old_fingerprint == original['fingerprint']
    assert result.new_fingerprint == identity_fingerprint_v1(
        fs_uuid='fs-1', annex_uuid='annex-1', serial=SERIAL, filesystem_capacity_bytes=1000)
    assert original == facts(raw)
    # No standing equality rule: an ordinary observation still carries raw E.
    assert serial_for_identity(SERIAL, raw) == raw


@pytest.mark.parametrize('key,value', [
    ('fs_uuid', None), ('annex_uuid', None), ('fs_uuid', 'other'), ('annex_uuid', 'other'),
    ('serial', None), ('serial', ''), ('serial', 'other'), ('serial', RAW),
    ('serial', 'é'), ('serial', 'a' * 129), ('serial', 'a b'), ('serial', '\ud800'),
    ('filesystem_capacity_bytes', True), ('filesystem_capacity_bytes', 1000.0),
    ('filesystem_capacity_bytes', 1001), ('anchor_capacity_bytes', 1001),
    ('anchor_authority', 'unknown'), ('fingerprint', 'a' * 64),
    ('anchor_fingerprint', 'b' * 64),
])
def test_saved_facts_cannot_be_inferred_or_weakened(key, value):
    with pytest.raises(SerialIdentityUnproven):
        recognize_bridge_anchor(**(facts() | {key: value}))


@pytest.mark.parametrize('key', ['anchor_identity_proof', 'anchor_fence_proof'])
@pytest.mark.parametrize('value', [None, '{}', 'null', '[]', '{',
    '{"v":1,"v":1,"fs_uuid":"fs-1","annex_uuid":"annex-1","serial":"5652314B56344C4B"}',
    '{"v":NaN,"fs_uuid":"fs-1","annex_uuid":"annex-1","serial":"5652314B56344C4B"}',
    '[' * 2000 + 'null' + ']' * 2000,
])
def test_invalid_proof_refuses(key, value):
    with pytest.raises(SerialIdentityUnproven):
        recognize_bridge_anchor(**(facts() | {key: value}))


@pytest.mark.parametrize('key', ['anchor_identity_proof', 'anchor_fence_proof'])
@pytest.mark.parametrize('field,value', [
    ('v', True), ('v', 1.0), ('v', 2), ('serial', None), ('serial', SERIAL),
    ('serial', RAW.lower()), ('serial', RAW + '00'), ('serial', ' ' + RAW),
    ('serial', RAW.encode().hex()), ('serial', 123), ('fs_uuid', 'other'),
    ('annex_uuid', None), ('extra', True),
])
def test_either_proof_mismatch_refuses(key, field, value):
    changed = facts()
    proof = json.loads(changed[key])
    proof[field] = value
    changed[key] = json.dumps(proof)
    with pytest.raises(SerialIdentityUnproven):
        recognize_bridge_anchor(**changed)


@pytest.mark.parametrize('raw', [SERIAL, RAW + '00', RAW.encode().hex(), '0x' + RAW])
def test_two_agreeing_proofs_and_a_matching_hash_do_not_authorize_other_encodings(raw):
    with pytest.raises(SerialIdentityUnproven):
        recognize_bridge_anchor(**facts(raw))
