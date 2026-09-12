"""Optional bridge evidence cannot become a new admission rule for ordinary reads."""
import pytest

from modelark import drive_bootstrap as bootstrap, drive_mutation as dm
from modelark.serial_evidence import load_bridge_binding
from modelark.slice import catalog, domain
from modelark.slice.transaction import _same_source
from test_serial_repair_public_slice import (
    workflow as _workflow, hardware as _hardware, native as _native,
    _candidate, _sources, PAYLOAD, REPO,
)

workflow, hardware, native = _workflow, _hardware, _native


@pytest.mark.parametrize('workflow', ['canonical', 'serialless'], indirect=True)
@pytest.mark.parametrize('clean', [False, True])
def test_ordinary_source_gate_unchanged_without_bridge_permission(workflow, clean):
    case = workflow
    candidate = _candidate(case)
    spec = domain.SliceSpec((REPO,), 'qualification', 'delivery')
    old = domain.preview(spec, catalog.read_catalog(case.path, spec))
    generation = dm.begin_generation(case.con, 'drive-00', 'ordinary-write')
    if clean:
        observed = bootstrap._live_evidence(case.con, 'drive-00').observation()
        dm._publish_anchor_locked(case.con, 'drive-00', 1, generation, observed, '2026-09-12')
    before = tuple(case.con.iterdump())
    assert load_bridge_binding(case.con, 'drive-00', sealed_source=candidate) is None
    with _sources(case).open(candidate) as (_, stream):
        assert stream.read(len(PAYLOAD)) == PAYLOAD
    # Preserve the separate transaction admission rule: an old plan is still
    # stale. Restoring ordinary read behavior is not permission to execute it.
    fresh = catalog.read_catalog(case.path, spec)
    assert not _same_source(old.closure[0], candidate, fresh)
    assert tuple(case.con.iterdump()) == before


@pytest.mark.parametrize('workflow', ['legacy'], indirect=True)
def test_other_serial_repair_is_not_bridge_permission_or_read_refusal(workflow):
    case = workflow
    intent = bootstrap.inspect_serial_identity(case.con, 'drive-00')
    bootstrap.repair_serial_identity(case.con, 'drive-00', expected_binding=intent['binding'],
                                    now='2026-09-12', writers_stopped=True)
    candidate = _candidate(case)
    generation = dm.begin_generation(case.con, 'drive-00', 'ordinary-write')
    observed = bootstrap._live_evidence(case.con, 'drive-00').observation()
    dm._publish_anchor_locked(case.con, 'drive-00', 1, generation, observed, '2026-09-12')
    assert load_bridge_binding(case.con, 'drive-00', sealed_source=candidate) is None
    with _sources(case).open(candidate) as (_, stream):
        assert stream.read(len(PAYLOAD)) == PAYLOAD
