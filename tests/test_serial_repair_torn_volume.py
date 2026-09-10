"""Real repair orchestration must reject volume changes inside its component probe."""
from types import SimpleNamespace

import pytest

from modelark import drive_bootstrap as bootstrap, drive_fence, drive_mutation as dm, register
from test_drive_bootstrap import _catalog, _FS, _ANX, _SER
from test_serial_repair_adversarial import _paused_owner
from test_serial_repair_workflow import OLD, seed


@pytest.mark.parametrize('dirty,change_on', [(False, 1), (True, 1), (True, 2)])
@pytest.mark.parametrize('field', ['fs_uuid', 'annex_uuid'])
def test_component_volume_change_inside_publication_probe_refuses(
    tmp_path, monkeypatch, dirty, change_on, field,
):
    with _catalog(tmp_path) as con:
        seed(con, dirty=dirty)
        if dirty:
            _paused_owner(con)
        archive = tmp_path / 'archive'
        archive.mkdir()
        sentinel = archive / 'untouched.txt'
        sentinel.write_bytes(b'archive stays unchanged')
        monkeypatch.setattr(drive_fence, '_LOCK_DIR', tmp_path / 'locks')
        monkeypatch.setattr(register, 'archive_path', lambda *args: archive)
        volume = {'fs_uuid': _FS, 'annex_uuid': _ANX}
        monkeypatch.setattr(register, 'probe_fs_uuid', lambda path: volume['fs_uuid'])
        monkeypatch.setattr(register, 'probe_annex_uuid', lambda path: volume['annex_uuid'])
        monkeypatch.setattr(bootstrap.os, 'fstatvfs',
                            lambda path: SimpleNamespace(f_blocks=1000, f_frsize=1, f_bavail=900))
        transactions = []
        changed = []

        def serial(path):
            # A different equal-sized filesystem on the SAME disk: serial and
            # capacity cannot distinguish this remount from the original volume.
            if con.in_transaction and len(transactions) == change_on:
                volume[field] = 'replacement-volume'
                changed.append(change_on)
            return _SER

        monkeypatch.setattr(register, 'probe_serial', serial)
        original = dm._immediate

        def immediate(connection, body):
            if connection is con:
                transactions.append(1)
            return original(connection, body)

        monkeypatch.setattr(dm, '_immediate', immediate)
        before = tuple(con.iterdump())
        sessions = con.execute('SELECT * FROM execution_sessions').fetchall()
        generations = con.execute('SELECT * FROM drive_dirty_generations ORDER BY generation').fetchall()
        binding = bootstrap.inspect_serial_identity(con, 'drive-00')['binding']
        with pytest.raises(dm.DriveMutationRefused, match='DRIVE_SERIAL_REPAIR_LIVE_MISMATCH') as raised:
            bootstrap.repair_serial_identity(con, 'drive-00', expected_binding=binding,
                                             now='2026-09-10', writers_stopped=True)
        assert changed, 'must reach the component-read race inside the actual transaction'
        assert raised.value.evidence['legacy_recovered'] is (change_on == 2)
        assert con.execute('PRAGMA user_version').fetchone() == (7,)
        assert con.execute('SELECT * FROM execution_sessions').fetchall() == sessions
        assert con.execute('SELECT * FROM drive_dirty_generations ORDER BY generation').fetchall() == generations
        if change_on == 2:
            assert con.execute('SELECT identity_epoch,write_generation,identity_fingerprint FROM drives').fetchone() == (
                1, 2, OLD)
            assert con.execute('SELECT generation,identity_fingerprint FROM drive_clean_anchors '
                               'ORDER BY generation').fetchall() == [(1, OLD), (2, OLD)]
        else:
            assert tuple(con.iterdump()) == before
        assert not con.in_transaction
        assert sentinel.read_bytes() == b'archive stays unchanged'
