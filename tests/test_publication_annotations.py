"""Advisory annotations must never masquerade as payload/location evidence."""
from decimal import Decimal

import pytest

from modelark.publication_annotations import (
    AnnotationCell, check_annotation_merge, check_annotation_write, parse_annotations,
)
from modelark.publication_policy import PublicationRefused


def test_native_compaction_and_encoding():
    history = b"1s model +org/old params +7\n2s model +!b3JnL25ldyBzcGFjZQ== -org/old params -7 +8\n"
    compacted = b"2s model +!b3JnL25ldyBzcGFjZQ== -org/old params -7 +8\n"
    assert parse_annotations(history) == parse_annotations(compacted)
    assert parse_annotations(compacted)["model", "org/new space"] == AnnotationCell(Decimal(2), True)
    check_annotation_write(before=b"1s model +org/old params +7\n", after=compacted,
                           assignments={"model": "org/new space", "params": "8"}, clock_ceiling=Decimal(3))


def test_native_repeated_assignment_is_not_necessarily_byte_noop():
    old, new = b"1s model +org/a\n", b"1s model +org/a\n2s model +org/a\n"
    check_annotation_write(before=old, after=new, assignments={"model": "org/a"}, clock_ceiling=Decimal(2))
    check_annotation_merge(baseline=old, admitted_source=new, candidate=b"2s model +org/a\n")


def test_native_merge_preserves_other_same_key_model_tag():
    old, source = b"1s model +org/a\n", b"2s model +org/b\n"
    check_annotation_merge(baseline=old, admitted_source=source, candidate=old + source)
    with pytest.raises(PublicationRefused, match="MERGE_MISMATCH"):
        check_annotation_merge(baseline=old, admitted_source=source, candidate=source)


@pytest.mark.parametrize("data", [
    b"1s model +a", b"1s model\n", b"1s model +\n", b"1s model +!bad==\n",
    b"1s model +!AA==\n", b"1s model +\xff\n", b"Nans model +a\n",
    b"1s model +a\tbad\n", b"1s model +a\r\n", b"1s model +a\x7f\n",
    b"1s model +a  format +b\n", b"1s model +a model +b\n", b"1s +value\n",
    b"1s model +a -a\n", b"1s model +a\n1s model -a\n",
    b"1s model +a\n2s model +a\n1s model -a\n",
])
def test_ambiguous_grammar_or_conflict_refuses(data):
    with pytest.raises(PublicationRefused):
        parse_annotations(data)


@pytest.mark.parametrize("after", [
    b"2s model +evil\n", b"2s model +good operator +evil\n",
    b"99s model +good\n", b"0s model +good\n", b"2s model +good -unknown\n",
    b"", b"2s model +good\n",  # drops the sealed old model tombstone
])
def test_write_must_be_exact_assignment_not_any_same_key_change(after):
    with pytest.raises(PublicationRefused):
        check_annotation_write(before=b"1s model +old\n", after=after,
                               assignments={"model": "good"}, clock_ceiling=Decimal(3))


def test_omitted_parameter_preserves_existing_value():
    old = b"1s model +old params +7\n"
    new = b"1s params +7\n2s model +new -old\n"
    check_annotation_write(before=old, after=new, assignments={"model": "new"}, clock_ceiling=Decimal(3))
    with pytest.raises(PublicationRefused):
        check_annotation_write(before=old, after=new.replace(b"params +7", b"params +8"),
                               assignments={"model": "new"}, clock_ceiling=Decimal(3))


def test_merge_equal_clock_conflict_refuses_before_candidate_interpretation():
    with pytest.raises(PublicationRefused, match="CLOCK_CONFLICT"):
        check_annotation_merge(baseline=b"1s model +old\n", admitted_source=b"1s model -old\n", candidate=b"")
