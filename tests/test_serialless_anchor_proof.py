"""No guessed serial, permissive JSON, or contradictory historical proof."""
import json

import pytest

from modelark.serial_identity import serialless_anchor_serial, SerialIdentityUnproven
from test_drive_bootstrap import _FS, _ANX, _SER, _FP


def facts():
    proof = json.dumps(dict(v=1, fs_uuid=_FS, annex_uuid=_ANX, serial=_SER))
    return dict(fs_uuid=_FS, annex_uuid=_ANX, fingerprint=_FP, filesystem_capacity_bytes=1000,
                anchor_identity_proof=proof, anchor_fence_proof=proof, anchor_fingerprint=_FP,
                anchor_capacity_bytes=1000, anchor_authority='dedicated_local')


def test_exact_evidence_returns_serial_without_changing_facts():
    original = facts()
    assert serialless_anchor_serial(**original) == _SER
    assert original == facts()


@pytest.mark.parametrize('key', ['anchor_identity_proof', 'anchor_fence_proof'])
@pytest.mark.parametrize('proof', [None, '{}', 'null', '[]', '{',
    '{"v":1,"v":1,"fs_uuid":"fs-uuid-1","annex_uuid":"annex-uuid-1","serial":"serial-1"}',
    '{"v":NaN,"fs_uuid":"fs-uuid-1","annex_uuid":"annex-uuid-1","serial":"serial-1"}',
    '[' * 2000 + 'null' + ']' * 2000])
def test_invalid_json_refuses(key, proof):
    with pytest.raises(SerialIdentityUnproven):
        serialless_anchor_serial(**(facts() | {key: proof}))


@pytest.mark.parametrize('key', ['anchor_identity_proof', 'anchor_fence_proof'])
@pytest.mark.parametrize('field,value', [('v', True), ('v', 1.0), ('v', 2),
    ('serial', None), ('serial', ''), ('serial', 'different'), ('serial', ' serial-1'),
    ('serial', 123), ('serial', '\ud800'), ('fs_uuid', 'other'), ('annex_uuid', None),
    ('extra', True)])
def test_proof_mismatch_refuses(key, field, value):
    changed = facts()
    proof = json.loads(changed[key])
    proof[field] = value
    changed[key] = json.dumps(proof)
    with pytest.raises(SerialIdentityUnproven):
        serialless_anchor_serial(**changed)


@pytest.mark.parametrize('field,value', [('filesystem_capacity_bytes', True),
    ('filesystem_capacity_bytes', 2000), ('anchor_capacity_bytes', 1000.0),
    ('anchor_authority', 'unknown'), ('anchor_fingerprint', 'f' * 64),
    ('fingerprint', _FP.upper()), ('fs_uuid', None)])
def test_facts_cannot_be_inferred_from_proof(field, value):
    with pytest.raises(SerialIdentityUnproven):
        serialless_anchor_serial(**(facts() | {field: value}))
