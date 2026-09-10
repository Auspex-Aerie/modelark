"""Pure recognition of the narrowly repairable v1 null-serial anchor (DEC-133).

Recognition is only one prerequisite for repair. The workflow must independently
bind the anchor to the exact epoch/generation, prove live media and cleanliness,
hold all compatible fences, and revalidate before committing. No hash-only check
in this module authorizes a repair, and no observation overwrites saved facts.
"""
from dataclasses import dataclass
import json

from modelark.capacity_evidence import identity_fingerprint_v1


class SerialIdentityUnproven(ValueError):
    """The supplied evidence does not prove the supported legacy correction."""


@dataclass(frozen=True)
class SerialCorrection:
    old_fingerprint: str
    new_fingerprint: str


def _normalized_string(value, name, *, optional=False):
    if value is None and optional:
        return
    if not isinstance(value, str) or not value or value != value.strip():
        raise SerialIdentityUnproven(f'{name} must be a nonempty normalized string')
    try:
        value.encode('utf-8')
    except UnicodeError as exc:
        raise SerialIdentityUnproven(f'{name} must be valid UTF-8 text') from exc


def _capacity(value, name):
    if type(value) is not int or value <= 0:
        raise SerialIdentityUnproven(f'{name} must be a positive integer')


def serial_for_identity(canonical_serial, observed_serial):
    """Select the existing catalog's serial binding without enriching it.

    A successful physical observation is required before calling: None is an
    observed absence, never a probe failure. Catalog NULL/empty deliberately
    leaves serial out of the fingerprint; discovering a serial does not register
    it. Known serials retain the actual observed input, and the workflow must
    additionally require equality with the canonical serial before admission.
    """
    if canonical_serial != "":
        _normalized_string(canonical_serial, 'canonical serial', optional=True)
    _normalized_string(observed_serial, 'observed serial', optional=True)
    return observed_serial if canonical_serial else None


def _correction(*, fs_uuid, annex_uuid, serial, fingerprint, filesystem_capacity_bytes):
    _normalized_string(fs_uuid, 'fs_uuid', optional=True)
    _normalized_string(annex_uuid, 'annex_uuid', optional=True)
    if fs_uuid is None and annex_uuid is None:
        raise SerialIdentityUnproven('filesystem or annex UUID required')
    _normalized_string(serial, 'serial')
    _capacity(filesystem_capacity_bytes, 'filesystem_capacity_bytes')
    old = identity_fingerprint_v1(
        fs_uuid=fs_uuid, annex_uuid=annex_uuid, serial=None,
        filesystem_capacity_bytes=filesystem_capacity_bytes,
    )
    if not isinstance(fingerprint, str) or fingerprint != old:
        raise SerialIdentityUnproven('saved fingerprint is not the exact null-serial identity')
    new = identity_fingerprint_v1(
        fs_uuid=fs_uuid, annex_uuid=annex_uuid, serial=serial,
        filesystem_capacity_bytes=filesystem_capacity_bytes,
    )
    return SerialCorrection(old, new)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise SerialIdentityUnproven(f'duplicate identity proof key: {key}')
        result[key] = value
    return result


def _invalid_constant(value):
    raise SerialIdentityUnproven(f'non-JSON identity proof value: {value}')


def recognize_legacy_anchor(
    *, fs_uuid, annex_uuid, serial, fingerprint, filesystem_capacity_bytes,
    anchor_identity_proof, anchor_fingerprint, anchor_capacity_bytes, anchor_authority,
) -> SerialCorrection:
    """Validate existing v1 anchor proof, preserving UUID and serial spelling.

The caller supplies a current clean anchor or an independently selected historical
same-epoch anchor for the explicit dirty bridge. This function cannot select an
anchor, prove a drive is mounted, or grant authority to publish either identity.
"""
    correction = _correction(
        fs_uuid=fs_uuid, annex_uuid=annex_uuid, serial=serial, fingerprint=fingerprint,
        filesystem_capacity_bytes=filesystem_capacity_bytes,
    )
    _capacity(anchor_capacity_bytes, 'anchor_capacity_bytes')
    if anchor_capacity_bytes != filesystem_capacity_bytes:
        raise SerialIdentityUnproven('anchor capacity disagrees with saved capacity')
    if not isinstance(anchor_fingerprint, str) or anchor_fingerprint != correction.old_fingerprint:
        raise SerialIdentityUnproven('anchor fingerprint disagrees with saved null-serial identity')
    if anchor_authority != 'dedicated_local':
        raise SerialIdentityUnproven('anchor dedicated_local authority required')
    if not isinstance(anchor_identity_proof, str):
        raise SerialIdentityUnproven('identity proof must be JSON text')
    try:
        proof = json.loads(anchor_identity_proof, object_pairs_hook=_unique_object,
                           parse_constant=_invalid_constant)
    except SerialIdentityUnproven:
        raise
    except (ValueError, RecursionError) as exc:
        raise SerialIdentityUnproven('malformed identity proof JSON') from exc
    if not isinstance(proof, dict) or set(proof) != {'v', 'fs_uuid', 'annex_uuid', 'serial'}:
        raise SerialIdentityUnproven('identity proof must contain exactly the v1 identity fields')
    if type(proof['v']) is not int or proof['v'] != 1:
        raise SerialIdentityUnproven('identity proof version must be integer 1')
    _normalized_string(proof['fs_uuid'], 'proof fs_uuid', optional=True)
    _normalized_string(proof['annex_uuid'], 'proof annex_uuid', optional=True)
    if proof['serial'] is not None:
        raise SerialIdentityUnproven('identity proof serial must be null')
    if proof['fs_uuid'] != fs_uuid or proof['annex_uuid'] != annex_uuid:
        raise SerialIdentityUnproven('identity proof UUIDs disagree with saved UUIDs')
    # The proof contains precisely the saved UUIDs and null serial; _correction
    # already reproduced its exact v1 hash with the validated anchor capacity.
    return correction


def is_legacy_serial_mismatch(
    *, fs_uuid, annex_uuid, serial, fingerprint, filesystem_capacity_bytes, live_fingerprint,
) -> bool:
    """Diagnostic only: does a null-serial saved hash differ only by saved serial?

There is deliberately no anchor input. True may select a repair-required message,
never admit a read/write or authorize a repair. Invalid facts return False so the
normal unproven-identity refusal remains available to the workflow.
"""
    try:
        correction = _correction(
            fs_uuid=fs_uuid, annex_uuid=annex_uuid, serial=serial, fingerprint=fingerprint,
            filesystem_capacity_bytes=filesystem_capacity_bytes,
        )
    except SerialIdentityUnproven:
        return False
    return isinstance(live_fingerprint, str) and live_fingerprint == correction.new_fingerprint
