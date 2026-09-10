"""Public supplied-connection hash repair cannot bypass catalog compatibility."""
from unittest import mock
import sqlite3

import pytest

from modelark import hash_repair
from test_drive_bootstrap import _catalog, _proven_drive, _FP


def _run(con, resolver, fingerprint=_FP):
    return hash_repair.run_explicit_drive_repair(
        con, 'drive-00', identity_epoch=1, identity_fingerprint=fingerprint,
        archive_resolver=resolver,
    )


@pytest.mark.parametrize('version', [0, 2, 6, 9, 10])
@pytest.mark.parametrize('entry', ['explicit', 'audit', 'dry_run', 'apply'])
def test_unsupported_supplied_connection_refuses_before_any_archive_or_write(tmp_path, version, entry):
    with _catalog(tmp_path) as con:
        _proven_drive(con)
        con.execute(f'PRAGMA user_version={version}')
        before = tuple(con.iterdump())
        statements = []
        con.set_trace_callback(statements.append)
        resolver = mock.Mock(side_effect=AssertionError('must not observe archive'))
        with pytest.raises(hash_repair.HashRepairError, match='unsupported catalog version'):
            if entry == 'explicit':
                _run(con, resolver)
            elif entry == 'audit':
                hash_repair.audit_hashes(con, archive_resolver=resolver)
            else:
                hash_repair.repair_hashes(con, apply=entry == 'apply', archive_resolver=resolver)
        con.set_trace_callback(None)
        assert statements == ['PRAGMA user_version']
        resolver.assert_not_called()
        assert tuple(con.iterdump()) == before
        assert not con.in_transaction
        assert not list(tmp_path.glob('*.bak'))


@pytest.mark.parametrize('version', [7, 8])
@pytest.mark.parametrize('entry', ['audit', 'dry_run', 'apply'])
def test_legacy_supported_entry_preserves_catalog_version(tmp_path, version, entry):
    with _catalog(tmp_path) as con:
        con.execute(f'PRAGMA user_version={version}')
        before = tuple(con.iterdump())
        if entry == 'audit':
            report = hash_repair.audit_hashes(con)
        else:
            report = hash_repair.repair_hashes(con, apply=entry == 'apply')
        assert report['repairs'] == []
        assert report['errors'] == []
        assert tuple(con.iterdump()) == before
        assert con.execute('PRAGMA user_version').fetchone() == (version,)


@pytest.mark.parametrize('version', [7, 8])
def test_supported_supplied_connection_repairs_without_changing_version(tmp_path, version):
    with _catalog(tmp_path) as con:
        _proven_drive(con)
        con.execute(f'PRAGMA user_version={version}')
        resolver = mock.Mock(return_value=tmp_path / 'archive')
        result = _run(con, resolver)
        assert result['status'] == 'complete'
        assert result['applied'] == 0
        resolver.assert_called_once_with(con, 'drive-00')
        assert con.execute('PRAGMA user_version').fetchone() == (version,)
        assert not con.in_transaction


@pytest.mark.parametrize('version', [7, 8])
def test_version_gate_keeps_stale_fingerprint_refusal_before_archive_observation(tmp_path, version):
    with _catalog(tmp_path) as con:
        _proven_drive(con)
        con.execute(f'PRAGMA user_version={version}')
        resolver = mock.Mock(side_effect=AssertionError('must not observe archive'))
        result = _run(con, resolver, fingerprint='0' * 64)
        assert result['status'] == 'halted'
        assert result['detail'] == 'identity_fingerprint mismatch'
        resolver.assert_not_called()
        assert con.execute('PRAGMA user_version').fetchone() == (version,)
        assert con.execute('SELECT identity_fingerprint FROM drives').fetchone() == (_FP,)
        assert not con.in_transaction


def test_version_change_before_write_lock_refuses_without_repair_state(tmp_path):
    with _catalog(tmp_path) as con:
        _proven_drive(con)

        class ChangedBeforeBegin:
            def __getattr__(self, name):
                return getattr(con, name)

            def execute(self, sql, *args):
                if sql == 'BEGIN IMMEDIATE':
                    con.execute('PRAGMA user_version=9')
                return con.execute(sql, *args)

        before = con.execute('SELECT * FROM drives').fetchall()
        resolver = mock.Mock(side_effect=AssertionError('must not observe archive'))
        with pytest.raises(hash_repair.HashRepairError, match='unsupported catalog version 9'):
            _run(ChangedBeforeBegin(), resolver)
        resolver.assert_not_called()
        assert con.execute('SELECT * FROM drives').fetchall() == before
        assert con.execute('SELECT * FROM drive_hash_repair_state').fetchall() == []
        assert con.execute('PRAGMA user_version').fetchone() == (9,)
        assert not con.in_transaction


@pytest.mark.parametrize('entry', ['audit', 'dry_run'])
@pytest.mark.parametrize('race_at', ['rows', 'resolver'])
def test_audit_pins_version_and_rows_across_concurrent_commit(tmp_path, monkeypatch, entry, race_at):
    with _catalog(tmp_path) as con:
        _proven_drive(con)
        con.execute('PRAGMA journal_mode=WAL')
        con.execute("INSERT INTO models(repo_id) VALUES('old/model')")
        con.execute("INSERT INTO files(repo_id,rfilename) VALUES('old/model','config.json')")
        con.execute("INSERT INTO archived(repo_id,rfilename,drive_label,stored_name,compressed) "
                    "VALUES('old/model','config.json','drive-00','config.json',0)")
        path = con.execute('PRAGMA database_list').fetchone()[2]
        writer = sqlite3.connect(path, isolation_level=None)
        seen = []

        def commit_future():
            writer.execute('BEGIN IMMEDIATE')
            writer.execute('PRAGMA user_version=9')
            writer.execute("UPDATE drives SET serial='future-serial'")
            writer.execute('COMMIT')

        original_rows = hash_repair._rows

        def rows(connection, scope):
            if race_at == 'rows':
                commit_future()
            seen.append(connection.execute('PRAGMA user_version').fetchone()[0])
            return original_rows(connection, scope)

        def resolver(connection, label):
            if race_at == 'resolver':
                commit_future()
            seen.append(connection.execute('PRAGMA user_version').fetchone()[0])
            assert connection.execute('SELECT serial FROM drives').fetchone()[0] != 'future-serial'
            return None

        monkeypatch.setattr(hash_repair, '_rows', rows)
        try:
            call = hash_repair.audit_hashes if entry == 'audit' else hash_repair.repair_hashes
            report = call(con, archive_resolver=resolver)
            assert report['archived_rows'] == 1
            assert seen == [7, 7]
            assert not con.in_transaction
            assert con.execute('PRAGMA user_version').fetchone() == (9,)
        finally:
            writer.close()


@pytest.mark.parametrize('caller_transaction', [False, True])
def test_audit_preserves_transaction_ownership_on_error(tmp_path, monkeypatch, caller_transaction):
    with _catalog(tmp_path) as con:
        if caller_transaction:
            con.execute('BEGIN IMMEDIATE')
            con.execute("INSERT INTO models(repo_id) VALUES('caller/pending')")
        monkeypatch.setattr(hash_repair, '_rows', mock.Mock(side_effect=ValueError('audit failed')))
        with pytest.raises(ValueError, match='audit failed'):
            hash_repair.audit_hashes(con)
        assert con.in_transaction is caller_transaction
        if caller_transaction:
            assert con.execute('SELECT repo_id FROM models').fetchall() == [('caller/pending',)]
            con.execute('ROLLBACK')
