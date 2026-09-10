"""Ordinary reconciliation cannot publish across a lifecycle/eligibility change."""
from types import SimpleNamespace
from unittest import mock

import pytest

import _pr09_gate1_fixtures as f
from modelark import drive_bootstrap as bs, drive_fence, drive_mutation as dm
from test_bootstrap_fence_aliases import _fingerprint, _published_state
from test_drive_bootstrap import (
    _SER, _anchor, _catalog, _clean_inv, _dirty_gen, _drive_row, _ev, _proven_drive,
)


@pytest.fixture(params=["bootstrap", "generation_zero", "sessionless_dirty", "clean", "transition", "drift", "owned"])
def case(request, tmp_path, monkeypatch):
    monkeypatch.setattr(drive_fence, "_LOCK_DIR", tmp_path / "locks")
    kind = request.param
    with _catalog(tmp_path) as con:
        if kind == "owned":
            f.seed_plan_selection(con, repos=("org/a",))
            _, pid, _ = f.create_and_approve(con)
            con.execute(
                "INSERT INTO execution_sessions(session_id,plan_id,approved_proposal_id,controller_identity,"
                "worker_identity,state,bound_planner_revision,fencing_token) "
                "VALUES('ended','ark',?,'controller','worker','paused',0,7)", [pid])
        if kind == "bootstrap":
            _drive_row(con)
        else:
            generation = 0 if kind == "generation_zero" else 1
            _proven_drive(con, generation=generation)
            if generation:
                _dirty_gen(con, owner="ended" if kind == "owned" else None,
                           token=7 if kind == "owned" else None)
            if kind in {"clean", "transition", "drift"}:
                _anchor(con)
        capacity = 2000 if kind == "transition" else 1000
        observed = _ev(fp=_fingerprint(capacity, _SER), capacity=capacity,
                       free=899 if kind == "drift" else capacity - 100)
        if kind == "drift":
            monkeypatch.setattr(bs, "free_drift_tolerance_v1", lambda _: 0)
        inventory = _clean_inv()
        observe = mock.Mock(return_value=observed)
        inspect = mock.Mock(return_value=inventory)
        path = mock.Mock(return_value=tmp_path / "archive")
        monkeypatch.setattr(bs, "_live_evidence", observe)
        monkeypatch.setattr(bs, "_inventory", inspect)
        monkeypatch.setattr(bs.register, "archive_path", path)
        yield SimpleNamespace(con=con, kind=kind, observed=observed, inventory=inventory,
                              observe=observe, inspect=inspect, path=path)


def run(case):
    return bs.reconcile_drive(case.con, "drive-00", now="test", dedicated=True,
                              accept_drift=True, blocking=False)


@pytest.mark.parametrize("lifecycle", ["lost", "retired"])
def test_initial_nonactive_drive_refuses_before_observation_or_inventory(case, lifecycle):
    case.con.execute("UPDATE drives SET lifecycle=? WHERE drive_label='drive-00'", [lifecycle])
    before = tuple(case.con.iterdump())
    with pytest.raises(dm.DriveMutationRefused, match="DRIVE_IDENTITY_UNPROVEN"):
        run(case)
    case.observe.assert_not_called()
    case.inspect.assert_not_called()
    case.path.assert_not_called()
    assert tuple(case.con.iterdump()) == before


def test_active_excluded_drive_still_allows_maintenance(case):
    case.con.execute("UPDATE drives SET eligibility='excluded' WHERE drive_label='drive-00'")
    result = run(case)
    assert result.outcome in {"bootstrapped", "refreshed", "recovered", "epoch_advanced", "drift_accepted"}
    assert case.con.execute("SELECT lifecycle,eligibility FROM drives WHERE drive_label='drive-00'").fetchone() == (
        "active", "excluded")


def test_publication_rechecks_metadata_inside_writer_transaction_without_changing_identity_tuple(case, monkeypatch):
    original = bs._reconcile_metadata
    checks = []

    def checked(con, label, *, expected=None):
        checks.append((con.in_transaction, expected))
        return original(con, label, expected=expected)

    monkeypatch.setattr(bs, "_reconcile_metadata", checked)
    assert len(bs._persisted(case.con, "drive-00")) == 8
    run(case)
    assert (True, ("active", "enabled")) in checks
    assert not case.con.in_transaction


@pytest.mark.parametrize("column,value", [("lifecycle", "lost"), ("lifecycle", "retired"), ("eligibility", "excluded")])
@pytest.mark.parametrize("phase", ["first_observation", "inventory", "before_begin"])
def test_metadata_drift_cannot_publish_on_any_ordinary_reconcile_path(case, monkeypatch, column, value, phase):
    before = _published_state(case.con)
    changed = []

    def change():
        assert not case.con.in_transaction
        case.con.execute(f"UPDATE drives SET {column}=? WHERE drive_label='drive-00'", [value])
        changed.append(True)

    if phase == "first_observation":
        def observe(*args):
            if not changed:
                change()
            return case.observed
        case.observe.side_effect = observe
    elif phase == "inventory":
        def inspect(*args, **kwargs):
            change()
            return case.inventory
        case.inspect.side_effect = inspect
    else:
        original = dm._immediate

        def begin(con, body):
            change()
            return original(con, body)
        monkeypatch.setattr(dm, "_immediate", begin)

    with pytest.raises(dm.DriveMutationRefused, match="DRIVE_RECOVERY_OWNER_CHANGED"):
        run(case)
    assert changed == [True]
    assert _published_state(case.con) == before
    assert case.con.execute(f"SELECT {column} FROM drives WHERE drive_label='drive-00'").fetchone() == (value,)
    if phase == "first_observation":
        case.inspect.assert_not_called()
    assert not case.con.in_transaction
