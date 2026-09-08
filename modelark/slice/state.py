"""Private, durable single-host slice authority. Never opens a ModelArk catalog.

The host namespace is deliberately not a per-catalog/per-transaction option. Tests replace
HOST_STATE_DIR with one disposable directory shared by all their child processes. Hardware
adapters and a public entry point are not provided by Slice 2.
"""
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import sqlite3
import stat
import uuid
from typing import NamedTuple

from modelark.execution_authority import Attempt, AuthorityLost, require_current
from .authority import RESUMABLE_STATES, RUNNING_STATES, TERMINAL_STATES, TRANSITIONS
from .domain import _json, validate_approval


HOST_STATE_DIR = Path.home() / ".local" / "state" / "modelark" / "slice"


class Reservation(NamedTuple):
    state: str
    stop_serial: int
    acknowledged_stop_serial: int


def _private_directory(path):
    for ancestor in (path, *path.parents):
        if ancestor.is_symlink():
            raise ValueError("slice state path may not contain symlinks")
    missing = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    # The last existing directory may be the result of interrupted bootstrap. Make its
    # parent entry durable before adding descendants, even when this retry creates nothing.
    for target in (current, current.parent):
        fd = os.open(target, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    for directory in reversed(missing):
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            pass
        for target in (directory, directory.parent):
            fd = os.open(target, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    for ancestor in (path, *path.parents):
        if ancestor.is_symlink():
            raise ValueError("slice state path may not contain symlinks")
    info = path.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError("slice state directory must be private to its owner")


class Store:
    def __init__(self):
        self.root = HOST_STATE_DIR.absolute()
        _private_directory(self.root)
        self.path = self.root / "transactions.sqlite"
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            pass
        else:
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        info = self.path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o077 or info.st_nlink != 1):
            raise ValueError("unsafe slice state database")
        with self._connection() as con:
            version = con.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2, 3, 4, 5):
                raise ValueError("unsupported slice state version")
            con.execute("CREATE TABLE IF NOT EXISTS transactions ("
                        "id TEXT PRIMARY KEY, plan TEXT NOT NULL, seal TEXT NOT NULL,"
                        "state TEXT NOT NULL, stop INTEGER NOT NULL DEFAULT 0, reason TEXT NOT NULL DEFAULT '',"
                        "journal_seq INTEGER NOT NULL DEFAULT 0, journal_digest TEXT NOT NULL DEFAULT '',"
                        "stop_serial INTEGER NOT NULL DEFAULT 0)")
            # Both pre-review v1 and the corrected v1 appeared during Slice 2 development.
            # Preserve plans, reservations, stops and journal heads in the same durable commit.
            if version == 1:
                columns = {row[1] for row in con.execute("PRAGMA table_info(transactions)")}
                if "stop_serial" not in columns:
                    con.execute("ALTER TABLE transactions ADD COLUMN stop_serial INTEGER NOT NULL DEFAULT 0")
            con.execute("CREATE TABLE IF NOT EXISTS owners (device TEXT PRIMARY KEY,"
                        "tx TEXT UNIQUE NOT NULL REFERENCES transactions(id),"
                        "activation_serial INTEGER NOT NULL DEFAULT 0,"
                        "process_seen INTEGER NOT NULL DEFAULT 0)")
            columns = {row[1] for row in con.execute("PRAGMA table_info(owners)")}
            if "activation_serial" not in columns:
                con.execute("ALTER TABLE owners ADD COLUMN activation_serial INTEGER NOT NULL DEFAULT 0")
                # Old initializations did not retain the activation token. Preserve any
                # pending stop conservatively; an explicit stopped-state resume can clear it.
                con.execute("UPDATE owners SET activation_serial=(SELECT stop_serial-stop "
                            "FROM transactions WHERE id=owners.tx)")
            if "process_seen" not in columns:
                # An old reservation alone cannot prove that its process ever held exclusion.
                con.execute("ALTER TABLE owners ADD COLUMN process_seen INTEGER NOT NULL DEFAULT 0")
            if "attempt" not in columns:
                con.execute("ALTER TABLE owners ADD COLUMN attempt TEXT")
            transaction_columns = {row[1] for row in con.execute("PRAGMA table_info(transactions)")}
            if "acknowledged_stop_serial" not in transaction_columns:
                con.execute("ALTER TABLE transactions ADD COLUMN acknowledged_stop_serial INTEGER NOT NULL DEFAULT 0")
                # Legacy stopped rows can also contain a NEW, unacknowledged request.
                # No old state label proves which request was actually acknowledged.
                con.execute("UPDATE transactions SET acknowledged_stop_serial=stop_serial-stop")
            # Legacy fields are retained for development-database compatibility only.
            # Neither process_seen nor activation_serial is execution authority in v5.
            con.execute("CREATE TABLE IF NOT EXISTS journal (tx TEXT NOT NULL REFERENCES transactions(id),"
                        "seq INTEGER NOT NULL, event TEXT NOT NULL, payload TEXT NOT NULL, digest TEXT NOT NULL,"
                        "PRIMARY KEY(tx,seq))")
            con.execute("PRAGMA user_version=5")

    @contextmanager
    def _connection(self, *, write=True):
        # Reader operations use deferred read transactions, but the private DB handle retains
        # SQLite's ability to recover a hot rollback journal left by a dead writer.
        con = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True, timeout=5, isolation_level=None)
        try:
            con.execute("PRAGMA foreign_keys=ON")
            con.execute("PRAGMA synchronous=FULL")
            con.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield con
            con.execute("COMMIT")
        except sqlite3.OperationalError as exc:
            if con.in_transaction:
                con.execute("ROLLBACK")
            from .transaction import TransferRefusal
            if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                raise TransferRefusal("STATE_BUSY", "private state writer did not finish") from exc
            raise
        except BaseException:
            if con.in_transaction:
                con.execute("ROLLBACK")
            raise
        finally:
            con.close()

    def create(self, plan, approval):
        if plan.version != "modelark.slice.transaction.v3":
            from .transaction import TransferRefusal
            raise TransferRefusal("LEGACY_PLAN", "new transactions require protocol v3")
        validate_approval(plan.proposal, approval)
        plan.required_bytes(self.root)
        tx = uuid.uuid4().hex
        with self._connection() as con:
            con.execute("INSERT INTO transactions(id,plan,seal,state) VALUES(?,?,?,'ready')",
                        (tx, plan.to_json(), plan.seal))
        return tx

    def load(self, tx):
        from .transaction import TransferPlan, TransferRefusal
        with self._connection(write=False) as con:
            row = con.execute("SELECT plan,seal FROM transactions WHERE id=?", (tx,)).fetchone()
        if row is None:
            raise TransferRefusal("TRANSACTION_MISSING", tx)
        plan = TransferPlan.from_json(row[0])
        if plan.seal != row[1]:
            raise TransferRefusal("STATE_CORRUPT", "plan seal differs")
        return plan

    def approve(self, tx, *, expected_seal):
        from .transaction import TransferRefusal
        self.load(tx)
        with self._connection() as con:
            seal, state = con.execute("SELECT seal,state FROM transactions WHERE id=?", (tx,)).fetchone()
            if seal != expected_seal:
                raise TransferRefusal("PREVIEW_STALE")
            if state not in {"ready", "approved"}:
                raise TransferRefusal("APPROVAL_STATE", state)
            con.execute("UPDATE transactions SET state='approved' WHERE id=? AND state='ready'", (tx,))

    def status(self, tx):
        from .transaction import Status, TransferRefusal
        with self._connection(write=False) as con:
            row = con.execute("SELECT state,reason FROM transactions WHERE id=?", (tx,)).fetchone()
        if row is None:
            raise TransferRefusal("TRANSACTION_MISSING", tx)
        return Status(tx, *row)

    def owner(self, device):
        with self._connection(write=False) as con:
            row = con.execute("SELECT tx FROM owners WHERE device=?", (device,)).fetchone()
        return row[0] if row else None

    def reserve(self, tx, device):
        from .transaction import TransferRefusal, Status
        def inspect(con):
            state, serial, acknowledged = con.execute(
                "SELECT state,stop_serial,acknowledged_stop_serial FROM transactions WHERE id=?", (tx,)).fetchone()
            if state == "complete":
                return None, Status(tx, state)
            owner = con.execute("SELECT tx FROM owners WHERE device=?", (device,)).fetchone()
            if owner and owner[0] != tx:
                raise TransferRefusal("DESTINATION_BUSY", owner[0])
            if state not in RESUMABLE_STATES:
                raise TransferRefusal("APPROVAL_MISSING" if state == "ready" else "NOT_RESUMABLE", state)
            return owner, Reservation(state, serial, acknowledged)
        # Repeated Start is a reader while an owner is actively journaling. Only the first
        # reservation needs the writer transaction; recheck inside it to close the CAS race.
        with self._connection(write=False) as con:
            owner, reservation = inspect(con)
            if owner or isinstance(reservation, Status):
                return reservation
        with self._connection() as con:
            owner, current = inspect(con)
            if isinstance(current, Status):
                return current
            con.execute("INSERT OR IGNORE INTO owners(device,tx) VALUES(?,?)", (device, tx))
            if not owner:
                con.execute("UPDATE transactions SET state='starting',reason='' WHERE id=?", (tx,))
            # Revalidate ownership/state, but never upgrade this caller's original
            # resume permission using a stop acknowledgment that happened while waiting.
            return reservation

    def current_attempt(self, device):
        with self._connection(write=False) as con:
            row = con.execute("SELECT tx,attempt FROM owners WHERE device=?", (device,)).fetchone()
        return Attempt(*row) if row and row[1] is not None else None

    def claim(self, tx, device, attempt, reservation):
        """Publish an attempt only while its caller holds exclusion and its live marker."""
        from .transaction import TransferRefusal, Status
        with self._connection() as con:
            state, stop, serial, acknowledged = con.execute(
                "SELECT state,stop,stop_serial,acknowledged_stop_serial FROM transactions WHERE id=?", (tx,)).fetchone()
            if state == "complete":
                return Status(tx, state)
            if state not in RESUMABLE_STATES:
                raise TransferRefusal("NOT_RESUMABLE", state)
            owner = con.execute("SELECT tx FROM owners WHERE device=?", (device,)).fetchone()
            if not owner or owner[0] != tx or attempt.owner != tx:
                raise TransferRefusal("EXECUTION_FENCE_LOST")
            con.execute("UPDATE owners SET attempt=? WHERE device=? AND tx=?", (attempt.token, device, tx))
            # Only a Start issued AFTER this exact stop was acknowledged may clear it.
            resume = (reservation.state == state == "stopped"
                      and reservation.stop_serial == reservation.acknowledged_stop_serial == serial == acknowledged)
            if stop and not resume:
                con.execute("UPDATE transactions SET state='stopped',reason='STOPPED',"
                            "acknowledged_stop_serial=stop_serial WHERE id=?", (tx,))
                return Status(tx, "stopped", "STOPPED")
            con.execute("UPDATE transactions SET state='starting',reason='',stop=0 WHERE id=?", (tx,))
            return Status(tx, "starting")

    def _require_attempt(self, con, attempt, device, *, allow_stop=False):
        from .transaction import TransferRefusal
        row = con.execute("SELECT t.state,t.stop,o.tx,o.attempt FROM transactions t "
                          "LEFT JOIN owners o ON o.device=? WHERE t.id=?", (device, attempt.owner)).fetchone()
        if row is None:
            raise TransferRefusal("EXECUTION_FENCE_LOST")
        try:
            require_current(attempt, Attempt(row[2], row[3]), row[0], RUNNING_STATES)
        except AuthorityLost as exc:
            code = "NOT_RESUMABLE" if row[0] in TERMINAL_STATES else "EXECUTION_FENCE_LOST"
            raise TransferRefusal(code, str(exc)) from exc
        if row[1] and not allow_stop:
            raise TransferRefusal("STOPPED")
        return row[0], bool(row[1])

    def transition(self, attempt, device, state, reason=""):
        from .transaction import Status, TransferRefusal
        if state not in RUNNING_STATES | {"failed", "invalidated"}:
            raise TransferRefusal("STATE_TRANSITION_INVALID", state)
        with self._connection() as con:
            current, stopped = self._require_attempt(con, attempt, device, allow_stop=True)
            if stopped and state not in {"failed", "invalidated"}:
                state, reason = "stopped", "STOPPED"
            if state not in TRANSITIONS[current]:
                raise TransferRefusal("STATE_TRANSITION_INVALID", f"{current} -> {state}")
            con.execute("UPDATE transactions SET state=?,reason=?,acknowledged_stop_serial="
                        "CASE WHEN ?='stopped' THEN stop_serial ELSE acknowledged_stop_serial END WHERE id=?",
                        (state, reason, state, attempt.owner))
            return Status(attempt.owner, state, reason)

    def request_stop(self, tx):
        with self._connection() as con:
            con.execute("UPDATE transactions SET stop=1,stop_serial=stop_serial+1 WHERE id=?", (tx,))

    def stop_requested(self, tx):
        with self._connection(write=False) as con:
            return bool(con.execute("SELECT stop FROM transactions WHERE id=?", (tx,)).fetchone()[0])

    def guard(self, attempt, device):
        with self._connection(write=False) as con:
            _, stopped = self._require_attempt(con, attempt, device, allow_stop=True)
            head = con.execute("SELECT journal_seq,journal_digest FROM transactions WHERE id=?", (attempt.owner,)).fetchone()
        return stopped, head

    def head(self, tx):
        with self._connection(write=False) as con:
            return con.execute("SELECT journal_seq,journal_digest FROM transactions WHERE id=?", (tx,)).fetchone()

    def append(self, tx, event, payload, *, expected_head=None, attempt, device):
        from .transaction import TransferRefusal
        encoded = _json(payload).decode()
        with self._connection() as con:
            state, _ = self._require_attempt(con, attempt, device)
            if state not in {"transferring", "verifying"}:
                raise TransferRefusal("STATE_TRANSITION_INVALID", f"journal append in {state}")
            if attempt.owner != tx:
                raise TransferRefusal("EXECUTION_FENCE_LOST")
            last = con.execute("SELECT journal_seq,journal_digest FROM transactions WHERE id=?", (tx,)).fetchone()
            if expected_head is not None and last != expected_head:
                from .transaction import TransferRefusal
                raise TransferRefusal("JOURNAL_CORRUPT", "journal changed outside its fenced writer")
            seq, previous = last[0] + 1, last[1]
            digest = hashlib.sha256(_json([tx, seq, previous, event, encoded])).hexdigest()
            con.execute("INSERT INTO journal VALUES(?,?,?,?,?)", (tx, seq, event, encoded, digest))
            con.execute("UPDATE transactions SET journal_seq=?,journal_digest=? WHERE id=?", (seq, digest, tx))
        return seq, digest

    def events(self, tx):
        import json
        from .transaction import TransferRefusal
        with self._connection(write=False) as con:
            rows = con.execute("SELECT seq,event,payload,digest FROM journal WHERE tx=? ORDER BY seq", (tx,)).fetchall()
            head = con.execute("SELECT journal_seq,journal_digest FROM transactions WHERE id=?", (tx,)).fetchone()
        previous, events = "", []
        for expected, (seq, event, payload, digest) in enumerate(rows, 1):
            if seq != expected or digest != hashlib.sha256(_json([tx, seq, previous, event, payload])).hexdigest():
                raise TransferRefusal("JOURNAL_CORRUPT")
            previous = digest
            events.append((event, json.loads(payload)))
        if head != (len(rows), previous):
            raise TransferRefusal("JOURNAL_CORRUPT", "journal tail differs from durable head")
        return events

    def receipt(self, tx):
        return next((payload for event, payload in reversed(self.events(tx)) if event == "receipt"), None)

    def complete(self, attempt, device, release_process):
        from .transaction import TransferRefusal
        tx = attempt.owner
        if self.receipt(tx) is None:
            raise TransferRefusal("RECEIPT_MISSING")
        with self._connection() as con:
            state, _ = self._require_attempt(con, attempt, device)
            if state != "verifying":
                raise TransferRefusal("STATE_TRANSITION_INVALID", f"completion in {state}")
            con.execute("UPDATE transactions SET state='complete',reason='' WHERE id=?", (tx,))
            release_process()
            con.execute("DELETE FROM owners WHERE device=? AND tx=?", (device, tx))
