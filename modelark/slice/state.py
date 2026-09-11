"""Private, durable single-host slice authority. Never opens a ModelArk catalog.

The host namespace is deliberately not a per-catalog/per-transaction option. Tests replace
HOST_STATE_DIR with one disposable directory shared by all their child processes. Hardware
adapters and a public entry point are not provided by Slice 2.
"""
from contextlib import contextmanager
import hashlib
import json
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


def _load_plan(payload):
    """Tagged dispatch; never reinterpret a folder record as legacy USB authority."""
    from .transaction import TransferPlan, TransferRefusal
    from .folder_contract import _decode
    from .folder_plan import NativePlan
    from .fat32_plan import Fat32Plan
    try:
        record = _decode(payload)
        if type(record) is not dict:
            raise ValueError("plan envelope required")
        if record.get("version") in {"modelark.slice.native-transaction.v1", "modelark.slice.native-transaction.v2"}:
            return NativePlan.from_json(payload)
        if record.get("version") in {"modelark.slice.fat32-transaction.v1", "modelark.slice.fat32-transaction.v2"}:
            return Fat32Plan.from_json(payload)
        return TransferPlan.from_json(payload)
    except (ValueError, TypeError) as exc:
        raise TransferRefusal("STATE_CORRUPT", str(exc)) from exc


def _claims_overlap(first, second):
    from .folder_contract import overlaps
    if first.is_folder and second.is_folder:
        if getattr(first, "session_only", False) or getattr(second, "session_only", False):
            a, b = first.destination.target, second.destination.target
            if a.filesystem_scope != b.filesystem_scope:
                return False
            if a.profile != b.profile:
                from .transaction import TransferRefusal
                raise TransferRefusal("DESTINATION_UNPROVEN", "mixed filesystem profiles")
            # FAT observation requires exact enumerated parent spelling, rejects
            # requested short aliases, and qualifies numbered short-name creation.
            left, right = tuple(p.lower() for p in a.parts), tuple(p.lower() for p in b.parts)
            common = min(len(left), len(right))
            return left[:common] == right[:common]
        return overlaps(first.destination.target, second.destination.target)
    if not first.is_folder and not second.is_folder:
        return first.destination.device_id == second.destination.device_id
    folder, legacy = (first, second) if first.is_folder else (second, first)
    keys = {key.casefold() for key in folder.backing_ids}
    return (legacy.destination.device_id.casefold() in keys
            or ("filesystem:" + legacy.destination.filesystem_id).casefold() in keys)


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
            if version not in (0, 1, 2, 3, 4, 5, 6, 7, 8):
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
            if "admission" not in transaction_columns:
                con.execute("ALTER TABLE transactions ADD COLUMN admission TEXT")
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
            if "consumed_attempt" not in {row[1] for row in con.execute("PRAGMA table_info(transactions)")}:
                con.execute("ALTER TABLE transactions ADD COLUMN consumed_attempt TEXT")
            # v8 fences binaries unaware of FAT's irrevocable single-attempt rule.
            con.execute("PRAGMA user_version=8")

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

    def create(self, plan, approval, *, admission=None):
        from .transaction import TransferRefusal, TransferPlan
        from .folder_plan import NativePlan
        from .fat32_plan import Fat32Plan
        native = isinstance(plan, (NativePlan, Fat32Plan))
        if not native and (not isinstance(plan, TransferPlan) or plan.version != "modelark.slice.transaction.v3"):
            raise TransferRefusal("LEGACY_PLAN", "new transactions require protocol v3")
        if native and admission is not None:
            raise TransferRefusal("ADMISSION_CORRUPT", "native admission is part of the plan")
        validate_approval(plan.proposal, approval)
        plan.required_bytes(self.root)
        encoded = None
        if admission is not None:
            self._validate_admission(plan, admission)
            encoded = _json(admission).decode()
        tx = uuid.uuid4().hex
        with self._connection() as con:
            con.execute("INSERT INTO transactions(id,plan,seal,state,admission) VALUES(?,?,?,'ready',?)",
                        (tx, plan.to_json(), plan.seal, encoded))
        return tx

    @staticmethod
    def _validate_admission(plan, admission):
        from .transaction import TransferRefusal
        try:
            from modelark.artifact_policy import DecodePolicy
            guarded = type(admission) is dict and admission.get("version") == "modelark.slice.direct.v2"
            keys = {"version", "catalog", "capacity"} | ({"decode_policy"} if guarded else set())
            valid = (type(admission) is dict and set(admission) == keys
                     and admission["version"] in {"modelark.slice.direct.v1", "modelark.slice.direct.v2"}
                     and isinstance(admission["catalog"], str) and Path(admission["catalog"]).is_absolute()
                     and "\0" not in admission["catalog"]
                     and os.path.normpath(admission["catalog"]) == admission["catalog"]
                     and type(admission["capacity"]) is dict)
            digest = hashlib.sha256(_json(admission)).hexdigest()
            if guarded:
                DecodePolicy.from_record(admission.get("decode_policy"))
        except (TypeError, ValueError) as exc:
            raise TransferRefusal("ADMISSION_CORRUPT", "invalid direct admission record") from exc
        prefix = "direct-v2:" if guarded else "direct-v1:"
        if not valid or plan.destination.mount_id != prefix + digest:
            raise TransferRefusal("ADMISSION_CORRUPT", "direct admission differs from sealed binding")

    def load_admission(self, tx):
        from .transaction import TransferRefusal
        plan = self.load(tx)
        if plan.is_folder:
            return {"version": plan.version, "catalog": plan.catalog}
        with self._connection(write=False) as con:
            row = con.execute("SELECT admission FROM transactions WHERE id=?", (tx,)).fetchone()
        if row is None:
            raise TransferRefusal("TRANSACTION_MISSING", tx)
        if row[0] is None:
            raise TransferRefusal("ADMISSION_MISSING", "legacy internal transaction has no direct admission")
        try:
            admission = json.loads(row[0])
        except (TypeError, ValueError) as exc:
            raise TransferRefusal("ADMISSION_CORRUPT", "invalid admission JSON") from exc
        self._validate_admission(plan, admission)
        return admission

    def load(self, tx):
        from .transaction import TransferRefusal
        with self._connection(write=False) as con:
            row = con.execute("SELECT plan,seal FROM transactions WHERE id=?", (tx,)).fetchone()
        if row is None:
            raise TransferRefusal("TRANSACTION_MISSING", tx)
        plan = _load_plan(row[0])
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
            encoded, seal = con.execute("SELECT plan,seal FROM transactions WHERE id=?", (tx,)).fetchone()
            plan = _load_plan(encoded)
            if plan.seal != seal or plan.destination.device_id != device:
                raise TransferRefusal("STATE_CORRUPT", "reservation differs from sealed plan")
            state, serial, acknowledged = con.execute(
                "SELECT state,stop_serial,acknowledged_stop_serial FROM transactions WHERE id=?", (tx,)).fetchone()
            if state == "complete":
                return None, Status(tx, state)
            # Check durable namespace claims in the same snapshot/commit as reserve.
            # A stopped owner still owns its output. A dead kernel lease is not
            # permission to adopt its tree or acquire an overlapping legacy drive.
            for other_tx, other_payload, other_seal in con.execute(
                    "SELECT o.tx,t.plan,t.seal FROM owners o JOIN transactions t ON t.id=o.tx WHERE o.tx!=?", (tx,)):
                other = _load_plan(other_payload)
                if other.seal != other_seal:
                    raise TransferRefusal("STATE_CORRUPT", "existing claim has a corrupt plan")
                if _claims_overlap(plan, other):
                    raise TransferRefusal("DESTINATION_BUSY", other_tx)
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

    def attempt_consumed(self, tx):
        with self._connection(write=False) as con:
            row = con.execute("SELECT consumed_attempt FROM transactions WHERE id=?", (tx,)).fetchone()
        return bool(row and row[0] is not None)

    def guard_preclaim(self, tx, device, reservation):
        """Read-only stop check while the caller holds device exclusion, before claim."""
        from .transaction import TransferRefusal
        with self._connection(write=False) as con:
            state, stop, serial, acknowledged = con.execute(
                "SELECT state,stop,stop_serial,acknowledged_stop_serial FROM transactions WHERE id=?", (tx,)).fetchone()
            owner = con.execute("SELECT tx FROM owners WHERE device=?", (device,)).fetchone()
        if owner != (tx,):
            raise TransferRefusal("EXECUTION_FENCE_LOST")
        resume = (reservation.state == state == "stopped"
                  and reservation.stop_serial == reservation.acknowledged_stop_serial == serial == acknowledged)
        if stop and not resume:
            raise TransferRefusal("STOPPED")
        if state not in RESUMABLE_STATES:
            raise TransferRefusal("NOT_RESUMABLE", state)

    def refuse_preclaim(self, tx, device, refusal):
        """Record read-only admission failure under exclusion; consume no FAT attempt.

        Only DeliveryAuthority calls this before publishing an attempt. Never
        clears a stop: acknowledge it and let a later explicit Start resume.
        """
        from .authority import refusal_state
        from .transaction import TransferRefusal
        with self._connection() as con:
            owner = con.execute("SELECT tx FROM owners WHERE device=?", (device,)).fetchone()
            if owner != (tx,):
                raise TransferRefusal("EXECUTION_FENCE_LOST")
            stop, serial = con.execute("SELECT stop,stop_serial FROM transactions WHERE id=?", (tx,)).fetchone()
            state = "stopped" if stop else refusal_state(refusal.code)
            if stop:
                con.execute("UPDATE transactions SET state='stopped',reason='STOPPED',"
                            "acknowledged_stop_serial=? WHERE id=?", (serial, tx))
            else:
                con.execute("UPDATE transactions SET state=?,reason=? WHERE id=?", (state, str(refusal), tx))

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
            encoded, seal, consumed = con.execute(
                "SELECT plan,seal,consumed_attempt FROM transactions WHERE id=?", (tx,)).fetchone()
            plan = _load_plan(encoded)
            if plan.seal != seal or plan.destination.device_id != device:
                raise TransferRefusal("STATE_CORRUPT", "claim differs from sealed intent")
            if getattr(plan, "session_only", False):
                if consumed is not None:
                    raise TransferRefusal("FAT32_NEW_ROOT_REQUIRED", "this intent has spent its only attempt")
            con.execute("UPDATE owners SET attempt=? WHERE device=? AND tx=?", (attempt.token, device, tx))
            # Only a Start issued AFTER this exact stop was acknowledged may clear it.
            resume = (reservation.state == state == "stopped"
                      and reservation.stop_serial == reservation.acknowledged_stop_serial == serial == acknowledged)
            if stop and not resume:
                con.execute("UPDATE transactions SET state='stopped',reason='STOPPED',"
                            "acknowledged_stop_serial=stop_serial WHERE id=?", (tx,))
                return Status(tx, "stopped", "STOPPED")
            if getattr(plan, "session_only", False):
                # A stop observed in this same atomic claim must not spend the
                # one-shot authority before any destination session can begin.
                con.execute("UPDATE transactions SET consumed_attempt=? WHERE id=?", (attempt.token, tx))
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
