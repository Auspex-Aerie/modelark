"""One restore-evidence rule shared by restore, verification, and legacy repair."""
from __future__ import annotations

import re

_ANNEX_SHA256 = re.compile(r"^SHA256E?-s\d+--([0-9a-f]{64})(?:\.|$)")


def annex_sha256(key: str | None) -> str | None:
    """Extract an original-byte digest only from a SHA256 git-annex key."""
    match = _ANNEX_SHA256.match(key or "")
    return match.group(1) if match else None


def expected_sha256(
    *,
    catalog_sha: str | None,
    orig_sha256: str | None,
    compressed: bool,
    annex_key: str | None,
) -> str | None:
    """Return the strongest expected original-byte digest available for one stored copy."""
    digest = catalog_sha or orig_sha256
    if digest:
        return str(digest).lower()
    # A raw SHA256 annex key names the original bytes. A compressed blob's annex key names the
    # compressed representation and therefore cannot certify its decompressed original.
    if not compressed:
        return annex_sha256(annex_key)
    return None
