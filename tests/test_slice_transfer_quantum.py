"""Source polling must not become destination I/O; bounded writes retain guards."""
# ruff: noqa: F811 -- pytest fixtures imported from the disposable transaction suite
from contextlib import contextmanager
import io
from types import SimpleNamespace

import pytest

from modelark.slice.transaction import Session, TransferRefusal
from test_slice_transaction import DATA, api, setup, start  # noqa: F401


def bare(poll=lambda: None):
    session = object.__new__(Session)
    session._poll = poll
    return session


@pytest.mark.parametrize('size', [0, 1, (1 << 20) - 1, 1 << 20, (1 << 20) + 17])
def test_gather_short_reads_bounded_and_owned(size):
    checks, reads = [], []

    class Short(io.BytesIO):
        def read(self, size):
            assert 0 < size <= 1 << 20
            reads.append(size)
            return super().read(min(size, 4093))

    stream = Short(b'x' * size)
    session = bare(lambda: checks.append(True))
    chunks = []
    while chunk := session._read_chunk(stream):
        chunks.append(chunk)
    assert b''.join(chunks) == b'x' * size
    assert all(len(chunk) == 1 << 20 for chunk in chunks[:-1])
    assert len(checks) == 2 * len(reads)
    assert len({id(chunk) for chunk in chunks}) == len(chunks)


@pytest.mark.parametrize('invalid', [None, 'text', memoryview(b'x'), b'x' * ((1 << 20) + 1)])
def test_bad_read_contract_refuses(invalid):
    with pytest.raises(TransferRefusal, match='SOURCE_READ_FAILED'):
        bare()._read_chunk(SimpleNamespace(read=lambda size: invalid))


@pytest.mark.parametrize('where', ['before', 'read', 'after', 'eof'])
def test_gather_preserves_exception_identity_and_discards_buffer(where):
    error = OSError('authority or source failure')
    calls = []

    def poll():
        calls.append('poll')
        if where == 'before' or where == 'after' and len(calls) == 3:
            raise error
        if where == 'eof' and len(calls) == 6:
            raise error

    def read(size):
        calls.append('read')
        if where == 'read':
            raise error
        return b'a' if len(calls) == 2 else b''

    with pytest.raises(OSError) as caught:
        bare(poll)._read_chunk(SimpleNamespace(read=read))
    assert caught.value is error


def test_poll_is_only_authority_and_exact_head_check():
    session = object.__new__(Session)
    calls = []
    session.head = (1, 'head')
    session.authority = SimpleNamespace(boundary=lambda: calls.append('authority') or (1, 'head'))
    session.destination = SimpleNamespace(check=lambda *args: pytest.fail('poll touched destination'))
    session._poll()
    assert calls == ['authority']
    session.head = (2, 'changed')
    with pytest.raises(TransferRefusal, match='JOURNAL_CORRUPT'):
        session._poll()


@pytest.mark.parametrize('condition', ['complete', 'stop', 'destination', 'journal'])
def test_polled_short_reads_keep_full_check_before_append(setup, condition):
    t, store, plan, tx, dest, sources = setup
    appends, polls, during_read = [], [], []
    append, check = dest.append, dest.check

    def checked(*args):
        assert not during_read, 'destination census inside a source read'
        polls.append('destination')
        return check(*args)

    def appended(path, token, data):
        if sources.fenced:
            assert polls and polls[-1] == 'destination'
            appends.append(bytes(data))
        return append(path, token, data)

    dest.check, dest.append = checked, appended

    @contextmanager
    def polled(candidate, poll):
        sources.fenced = True

        class Short(io.BytesIO):
            def read(self, size):
                during_read.append(True)
                try:
                    poll()
                    data = super().read(min(size, 1))
                    if data:
                        if condition == 'stop':
                            store.request_stop(tx)
                        elif condition == 'destination':
                            dest.changed = True
                        elif condition == 'journal':
                            with store._connection() as con:
                                con.execute('UPDATE transactions SET journal_seq=journal_seq+1 WHERE id=?', (tx,))
                    poll()
                    return data
                finally:
                    during_read.pop()

        try:
            yield sources.snapshot, Short(DATA)
        finally:
            sources.fenced = False

    sources.open_polled = polled
    sources.open_checked = lambda *args: pytest.fail('polled capability ignored')
    with start(setup) as session:
        if condition in {'destination', 'journal'}:
            with pytest.raises(TransferRefusal, match='DESTINATION_CHANGED|JOURNAL_CORRUPT'):
                session.run()
        else:
            assert session.run().state == ('complete' if condition == 'complete' else 'stopped')
    assert appends == ([DATA] if condition == 'complete' else [])
    if condition == 'journal':
        with pytest.raises(TransferRefusal, match='JOURNAL_CORRUPT'):
            store.receipt(tx)
        with store._connection(write=False) as con:
            assert con.execute("SELECT count(*) FROM journal WHERE tx=? AND event='receipt'", (tx,)).fetchone()[0] == 0
    else:
        assert (store.receipt(tx) is not None) == (condition == 'complete')


def test_checked_only_source_retains_full_destination_callback(setup):
    t, store, plan, tx, dest, sources = setup
    opening = sources.open
    called = []

    @contextmanager
    def checked(candidate, check):
        before = len(dest.trace)
        check()
        assert len(dest.trace) > before and dest.trace[-1][0] == 'check'
        called.append(True)
        with opening(candidate) as result:
            yield result

    sources.open_checked = checked
    with start(setup) as session:
        assert session.run().state == 'complete'
    assert called


@pytest.mark.parametrize('condition', ['complete', 'detached', 'changed'])
def test_final_eof_rechecks_destination_before_flush(setup, condition):
    t, store, plan, tx, dest, sources = setup
    appends, after_eof = [], []
    append, check, flush = dest.append, dest.check, dest.flush

    def appended(path, token, data):
        if sources.fenced:
            appends.append(bytes(data))
        return append(path, token, data)

    def checked(*args):
        if after_eof:
            after_eof.append('check')
        return check(*args)

    def flushed(path):
        if after_eof:
            after_eof.append('flush')
        return flush(path)

    dest.append, dest.check, dest.flush = appended, checked, flushed

    @contextmanager
    def polled(candidate, poll):
        sources.fenced = True

        class FinalEOF(io.BytesIO):
            def read(self, size):
                data = super().read(size)
                # The first gather includes a short tail and EOF. Change the
                # destination only at the next empty gather, after its append.
                if not data and appends:
                    assert not after_eof
                    after_eof.append('eof')
                    if condition == 'detached':
                        dest.available = False
                    elif condition == 'changed':
                        dest.changed = True
                return data

        try:
            yield sources.snapshot, FinalEOF(DATA)
        finally:
            sources.fenced = False

    sources.open_polled = polled
    with start(setup) as session:
        if condition == 'changed':
            with pytest.raises(TransferRefusal) as caught:
                session.run()
            assert caught.value.code == 'DESTINATION_CHANGED'
        else:
            expected = 'complete' if condition == 'complete' else 'waiting_destination'
            assert session.run().state == expected
    assert appends == [DATA]
    assert after_eof[:2] == ['eof', 'check']
    if condition == 'complete':
        assert after_eof[2] == 'flush'
    else:
        assert 'flush' not in after_eof
        assert not (dest.root / 'models/org/model/model.safetensors').exists()
    assert (store.receipt(tx) is not None) == (condition == 'complete')
