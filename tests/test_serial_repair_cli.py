"""Operator inspection/repair entry points against disposable on-disk catalogs."""
import json
from types import SimpleNamespace

import pytest

from modelark import cli
from test_drive_bootstrap import _catalog
from test_serial_repair_workflow import seed, live_setup


def args(**changes):
    return SimpleNamespace(**({'label': 'drive-00', 'dedicated': False, 'accept_drift': False,
                              'inspect_serial_identity': False, 'repair_serial_identity': False,
                              'expected_binding': None, 'writers_stopped': False} | changes))


def test_operator_inspection_then_explicit_repair(tmp_path, monkeypatch, capsys):
    with _catalog(tmp_path) as con:
        seed(con, dirty=True)
        live_setup(monkeypatch, tmp_path)
        before = tuple(con.iterdump())
        cli.cmd_drive_reconcile(args(inspect_serial_identity=True))
        inspection = json.loads(capsys.readouterr().out)
        assert inspection['status'] == 'legacy_dirty'
        assert tuple(con.iterdump()) == before
        cli.cmd_drive_reconcile(args(repair_serial_identity=True, writers_stopped=True,
                                     expected_binding=inspection['binding']))
        output = capsys.readouterr()
        assert json.loads(output.out)['status'] == 'repaired'
        assert 'old-identity recovery committed' in output.err
        assert con.execute('PRAGMA user_version').fetchone() == (8,)


@pytest.mark.parametrize('options,message', [
    ({'repair_serial_identity': True}, 'requires --expected-binding'),
    ({'expected_binding': 'a' * 64}, 'require --repair-serial-identity'),
    ({'inspect_serial_identity': True, 'dedicated': True}, 'does not adopt authority'),
])
def test_invalid_operator_intent_refuses_before_catalog_open(monkeypatch, options, message):
    def fail(*args, **kwargs):
        pytest.fail('invalid intent must not open catalog')
    monkeypatch.setattr(cli.db, 'connect', fail)
    with pytest.raises(SystemExit, match=message):
        cli.cmd_drive_reconcile(args(**options))


def test_stale_operator_binding_is_a_clear_refusal(tmp_path, monkeypatch):
    with _catalog(tmp_path) as con:
        seed(con)
        live_setup(monkeypatch, tmp_path)
        before = tuple(con.iterdump())
        with pytest.raises(SystemExit, match='DRIVE_SERIAL_REPAIR_STALE'):
            cli.cmd_drive_reconcile(args(repair_serial_identity=True, writers_stopped=True,
                                         expected_binding='a' * 64))
        assert tuple(con.iterdump()) == before
