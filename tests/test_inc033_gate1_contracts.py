"""INC-033 Gate-1 contracts — digest column vs annex-key disagreement.

Contracts only. Production is unchanged, so c01/c02/c04 stay red until Gate 2
makes the resolver fail closed on conflict.
"""
from __future__ import annotations

from modelark import archive_hash as ah


_A = "a" * 64
_B = "b" * 64
_ANNEX_B = f"SHA256E-s100--{_B}"
_ANNEX_A = f"SHA256E-s100--{_A}"


def _expected(**kwargs):
    error = None
    got = None
    try:
        got = ah.expected_sha256(**kwargs)
    except Exception as exc:
        error = exc
    return got, error


def test_c01_conflict_does_not_satisfy_column_or_return_column_winner():
    """orig A + uncompressed annex B must not approve A or resolve to A."""
    got, err = _expected(
        catalog_sha=None, orig_sha256=_A, compressed=False, annex_key=_ANNEX_B
    )
    if err is None:
        assert got is None, (
            "expected_sha256 must not return a digest winner on conflict: "
            f"{got!r}"
        )
    for approved in (_A, _B):
        sat = None
        sat_err = None
        try:
            sat = ah.content_satisfies(
                approved_sha256=approved,
                orig_sha256=_A,
                compressed=False,
                annex_key=_ANNEX_B,
            )
        except Exception as exc:
            sat_err = exc
        assert sat is False or sat_err is not None, (
            f"content_satisfies must not accept {approved} on annex conflict; "
            f"got sat={sat!r} err={sat_err!r}"
        )


def test_c02_conflict_is_visible_with_both_digests():
    """Conflict must surface both independently resolvable digests."""
    got, err = _expected(
        catalog_sha=None, orig_sha256=_A, compressed=False, annex_key=_ANNEX_B
    )
    if err is None and isinstance(got, str) and got.lower() in {_A, _B}:
        raise AssertionError(
            f"expected_sha256 selected a winner on conflict: {got!r}"
        )
    helper = getattr(ah, "evidence_conflict", None)
    if callable(helper):
        result = helper(
            catalog_sha=None,
            orig_sha256=_A,
            compressed=False,
            annex_key=_ANNEX_B,
        )
        blob = f"{result!r}".lower()
        assert _A in blob and _B in blob, result
        return
    assert err is not None, (
        f"without evidence_conflict, expected_sha256 must raise on conflict; "
        f"got {got!r}"
    )
    blob = f"{err!r}".lower()
    assert _A in blob and _B in blob, err


def test_c03_agreeing_or_single_source_still_resolves():
    """Happy path: agree, only-column, only-annex still work."""
    assert ah.content_satisfies(
        approved_sha256=_A, orig_sha256=_A, compressed=False, annex_key=_ANNEX_A
    ) is True
    assert ah.expected_sha256(
        catalog_sha=None, orig_sha256=_A, compressed=False, annex_key=_ANNEX_A
    ) == _A
    assert ah.expected_sha256(
        catalog_sha=None, orig_sha256=_A, compressed=False, annex_key=None
    ) == _A
    assert ah.content_satisfies(
        approved_sha256=_A, orig_sha256=_A, compressed=False, annex_key=None
    ) is True
    assert ah.expected_sha256(
        catalog_sha=None, orig_sha256=None, compressed=False, annex_key=_ANNEX_B
    ) == _B
    assert ah.content_satisfies(
        approved_sha256=_B, orig_sha256=None, compressed=False, annex_key=_ANNEX_B
    ) is True
    assert ah.content_satisfies(
        approved_sha256=_A, orig_sha256=None, compressed=True, annex_key=_ANNEX_A
    ) is False


def test_c04_malformed_orig_does_not_fall_through_to_annex_or_approve():
    """Present-but-malformed orig_sha256 is error, not annex B and not a digest winner."""
    bad = "not-a-digest"
    got, err = _expected(
        catalog_sha=None, orig_sha256=bad, compressed=False, annex_key=_ANNEX_B
    )
    assert err is not None or got is None, (
        f"malformed orig must be error or None, not a digest winner; got {got!r}"
    )
    if err is None:
        assert got != _B
    sat_none = None
    sat_b = None
    try:
        sat_none = ah.content_satisfies(
            approved_sha256=None, orig_sha256=bad, compressed=False, annex_key=_ANNEX_B
        )
    except Exception:
        sat_none = False
    try:
        sat_b = ah.content_satisfies(
            approved_sha256=_B, orig_sha256=bad, compressed=False, annex_key=_ANNEX_B
        )
    except Exception:
        sat_b = False
    assert sat_none is False, sat_none
    assert sat_b is False, sat_b
