"""Pure compatibility keys for the v1 null-serial evidence correction (DEC-133).

These facts describe exclusion, not admission. Every key must be held together;
neither an alias nor a successful lock makes an old fingerprint executable.
Workflow adapters capture facts, translate refusals and revalidate after locking.
"""
from dataclasses import dataclass

from modelark.capacity_evidence import identity_fingerprint_v1


class UnprovenFenceIdentity(ValueError):
    """No safe compatible physical-lock set can be derived from these facts."""


@dataclass(frozen=True)
class FenceIdentity:
    fs_uuid: str | None
    annex_uuid: str | None
    serial: str | None
    filesystem_capacity_bytes: int | None
    identity_epoch: int
    identity_fingerprint: str | None

    def lock_keys(self) -> tuple[tuple[str, int], ...]:
        for name in ("fs_uuid", "annex_uuid", "serial"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or value.strip() != value):
                raise UnprovenFenceIdentity("malformed " + name)
        if not self.fs_uuid and not self.annex_uuid:
            raise UnprovenFenceIdentity("stable filesystem or annex UUID required")
        if type(self.filesystem_capacity_bytes) is not int or self.filesystem_capacity_bytes <= 0:
            raise UnprovenFenceIdentity("proven filesystem capacity required")
        if type(self.identity_epoch) is not int or self.identity_epoch < 1:
            raise UnprovenFenceIdentity("proven identity epoch required")
        fingerprints = {
            identity_fingerprint_v1(
                fs_uuid=self.fs_uuid, annex_uuid=self.annex_uuid, serial=serial,
                filesystem_capacity_bytes=self.filesystem_capacity_bytes)
            for serial in (None, self.serial or None)
        }
        if not isinstance(self.identity_fingerprint, str) or self.identity_fingerprint not in fingerprints:
            raise UnprovenFenceIdentity("fingerprint does not bind the captured identity facts")
        return tuple(sorted((fingerprint, self.identity_epoch) for fingerprint in fingerprints))


def compatible_keys(identities) -> tuple[tuple[str, int], ...]:
    """A globally sorted, deduplicated AND set, including across drive labels."""
    return tuple(sorted({key for identity in identities for key in identity.lock_keys()}))
