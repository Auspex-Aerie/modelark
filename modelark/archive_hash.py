"""One restore-evidence rule shared by restore, verification, and legacy repair."""
from __future__ import annotations

import re

_ANNEX_SHA256 = re.compile(r"^SHA256E?-s\d+--([0-9a-f]{64})(?:\.|$)")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class DigestEvidenceError(ValueError):
    """Fail-closed digest evidence (INC-033)."""


class DigestEvidenceConflict(DigestEvidenceError):
    """Two independently resolvable original-byte sources disagree."""

    def __init__(self, sources: dict[str, str]):
        self.sources = dict(sources)
        super().__init__(
            "digest evidence conflict: "
            + " ".join(f"{k}={v}" for k, v in self.sources.items())
        )


class DigestEvidenceInvalid(DigestEvidenceError):
    """Present evidence is malformed or compression is indeterminate."""


def annex_sha256(key: str | None) -> str | None:
    """Extract an original-byte digest only from a SHA256 git-annex key."""
    match = _ANNEX_SHA256.match(key or "")
    return match.group(1) if match else None


def _hex64(value: str | None, *, field: str) -> str | None:
    """Return a 64-hex digest, or None if absent. Does not raise."""
    if value is None:
        return None
    text = str(value).strip().lower()
    if text == "":
        return None
    if _HEX64.match(text) is None:
        return None
    return text


def _present_nonempty(value: str | None) -> bool:
    return value is not None and str(value).strip() != ""


def _canonical_compressed(compressed) -> bool:
    if type(compressed) is bool:
        return compressed
    if type(compressed) is int and compressed in (0, 1):
        return bool(compressed)
    raise DigestEvidenceInvalid(f"indeterminate compressed: {compressed!r}")


def evidence_conflict(
    *,
    catalog_sha: str | None,
    orig_sha256: str | None,
    compressed: bool,
    annex_key: str | None,
) -> dict[str, str] | None:
    """Return disagreeing original-byte sources, or None if they agree / only one exists."""
    try:
        expected_sha256(
            catalog_sha=catalog_sha,
            orig_sha256=orig_sha256,
            compressed=compressed,
            annex_key=annex_key,
        )
    except DigestEvidenceConflict as exc:
        return dict(exc.sources)
    return None


def expected_sha256(
    *,
    catalog_sha: str | None,
    orig_sha256: str | None,
    compressed: bool,
    annex_key: str | None,
) -> str | None:
    """Return the strongest expected original-byte digest available for one stored copy.

    Independently resolvable 64-hex sources that disagree raise
    ``DigestEvidenceConflict`` (INC-033). Malformed present ``orig_sha256`` /
    ``catalog_sha`` together with another resolvable original-byte source raise
    ``DigestEvidenceInvalid``. Malformed orig/catalog alone returns the legacy
    string (non-hex test hashes). Indeterminate ``compressed`` raises Invalid.
    """
    compressed_flag = _canonical_compressed(compressed)
    annex = None
    if not compressed_flag:
        extracted = annex_sha256(annex_key)
        if extracted is not None:
            annex = extracted.lower()
    catalog = _hex64(catalog_sha, field="catalog_sha")
    orig = _hex64(orig_sha256, field="orig_sha256")
    orig_malformed = _present_nonempty(orig_sha256) and orig is None
    catalog_malformed = _present_nonempty(catalog_sha) and catalog is None
    if orig_malformed and annex is not None:
        raise DigestEvidenceInvalid(
            f"malformed orig_sha256 with resolvable annex: {orig_sha256!r} {annex}"
        )
    if catalog_malformed and (orig is not None or annex is not None):
        raise DigestEvidenceInvalid(
            f"malformed catalog_sha with other original-byte evidence: {catalog_sha!r}"
        )
    sources: dict[str, str] = {}
    if catalog is not None:
        sources["catalog_sha"] = catalog
    if orig is not None:
        sources["orig_sha256"] = orig
    if annex is not None:
        sources["annex_key"] = annex
    distinct = set(sources.values())
    if len(distinct) > 1:
        raise DigestEvidenceConflict(sources)
    if len(distinct) == 1:
        return next(iter(distinct))
    if orig_malformed:
        return str(orig_sha256).strip().lower()
    if catalog_malformed:
        return str(catalog_sha).strip().lower()
    return None


def content_satisfies(
    *,
    approved_sha256: str | None,
    orig_sha256: str | None,
    compressed: bool,
    annex_key: str | None,
) -> bool:
    """Return whether supplied archive evidence satisfies supplied approved content.

    This is deliberately a scalar-only approval predicate.  Resolver callers that need to know
    which digest evidence exists continue to call :func:`expected_sha256` directly.
    """
    try:
        resolved = expected_sha256(
            catalog_sha=None,
            orig_sha256=orig_sha256,
            compressed=compressed,
            annex_key=annex_key,
        )
    except DigestEvidenceError:
        return False
    if approved_sha256:
        return resolved is not None and str(resolved).lower() == str(approved_sha256).lower()
    return resolved is not None and str(resolved) != ""
