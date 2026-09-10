"""Attachment replacement after the last UUID sample cannot publish repair evidence."""
from types import SimpleNamespace

import pytest

from modelark import drive_bootstrap as bootstrap, drive_fence, drive_mutation as dm, register
from test_drive_bootstrap import _catalog, _FS, _ANX, _SER
from test_serial_repair_adversarial import _paused_owner
from test_serial_repair_workflow import OLD, seed


@pytest.mark.parametrize('dirty,change_on', [(False, 1), (True, 1), (True, 2)])
@pytest.mark.parametrize('last_component', ['fs_uuid', 'annex_uuid', 'statvfs'])
def test_repair_rejects_directory_attachment_replaced_during_final_components(
    tmp_path, monkeypatch, dirty, change_on, last_component,
):
    with _catalog(tmp_path) as con:
        seed(con, dirty=dirty)
        if dirty:
            _paused_owner(con)
        archive = tmp_path / 'archive'
        archive.mkdir()
        (archive / 'sentinel').write_bytes(b'original archive')
        retained = tmp_path / 'original-attachment'
        monkeypatch.setattr(drive_fence, '_LOCK_DIR', tmp_path / 'locks')
        monkeypatch.setattr(register, 'archive_path', lambda *args: archive)
        monkeypatch.setattr(register, 'probe_serial', lambda path: _SER)
        transactions = []
        reads = {key: 0 for key in ('fs_uuid', 'annex_uuid', 'statvfs')}
        replacements = []

        def sample(component, value):
            if con.in_transaction and len(transactions) == change_on:
                reads[component] += 1
                if component == last_component and reads[component] == 2:
                    # Replace the path's directory while preserving every
                    # reported UUID/serial/capacity. A held directory FD remains
                    # attached to the old inode, not this replacement tree.
                    archive.rename(retained)
                    archive.mkdir()
                    (archive / 'sentinel').write_bytes(b'replacement archive')
                    replacements.append(component)
            return value

        monkeypatch.setattr(register, 'probe_fs_uuid', lambda path: sample('fs_uuid', _FS))
        monkeypatch.setattr(register, 'probe_annex_uuid', lambda path: sample('annex_uuid', _ANX))
        monkeypatch.setattr(register.os, 'fstatvfs', lambda fd: sample(
            'statvfs', SimpleNamespace(f_blocks=1000, f_frsize=1, f_bavail=900)))
        original = dm._immediate

        def immediate(connection, body):
            if connection is con:
                transactions.append(1)
            return original(connection, body)

        monkeypatch.setattr(dm, '_immediate', immediate)
        before = tuple(con.iterdump())
        sessions = con.execute('SELECT * FROM execution_sessions').fetchall()
        binding = bootstrap.inspect_serial_identity(con, 'drive-00')['binding']
        with pytest.raises(dm.DriveMutationRefused, match='DRIVE_SERIAL_REPAIR_LIVE_MISMATCH') as raised:
            bootstrap.repair_serial_identity(con, 'drive-00', expected_binding=binding,
                                             now='2026-09-10', writers_stopped=True)
        assert replacements == [last_component]
        assert raised.value.evidence['legacy_recovered'] is (change_on == 2)
        assert con.execute('PRAGMA user_version').fetchone() == (7,)
        assert con.execute('SELECT * FROM execution_sessions').fetchall() == sessions
        if change_on == 2:
            assert con.execute('SELECT identity_epoch,write_generation,identity_fingerprint FROM drives').fetchone() == (
                1, 2, OLD)
            assert con.execute('SELECT generation,identity_fingerprint FROM drive_clean_anchors '
                               'ORDER BY generation').fetchall() == [(1, OLD), (2, OLD)]
        else:
            assert tuple(con.iterdump()) == before
        assert not con.in_transaction
        assert (retained / 'sentinel').read_bytes() == b'original archive'
        assert (archive / 'sentinel').read_bytes() == b'replacement archive'
