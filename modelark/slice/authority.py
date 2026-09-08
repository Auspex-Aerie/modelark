"""Live delivery-attempt capabilities, separate from durable plans and archive authority."""
import errno
import hashlib
import os
import socket
import uuid

from modelark.execution_authority import Attempt


RESUMABLE_STATES = frozenset({"approved", "starting", "transferring", "verifying", "stopped",
                              "waiting_source", "blocked_source", "waiting_destination"})
RUNNING_STATES = RESUMABLE_STATES - {"approved"}
TERMINAL_STATES = frozenset({"complete", "failed", "invalidated"})
_OUTCOMES = frozenset({"stopped", "waiting_source", "blocked_source", "waiting_destination",
                       "failed", "invalidated"})
TRANSITIONS = {
    "starting": _OUTCOMES | {"transferring"},
    "transferring": _OUTCOMES | {"transferring", "verifying"},
    "verifying": _OUTCOMES | {"verifying"},
    **{state: _OUTCOMES | {"transferring"}
       for state in ("waiting_source", "blocked_source", "waiting_destination")},
    "stopped": frozenset({"stopped"}),  # Resume requires a newly claimed attempt.
}


def refusal_state(code):
    return {"STOPPED": "stopped", "WAITING_SOURCE": "waiting_source",
            "WAITING_DESTINATION": "waiting_destination", "SOURCE_BLOCKED": "blocked_source"}.get(
                code, "invalidated" if code.startswith("DESTINATION_") else "failed")


def _marker_address(device, attempt):
    key = hashlib.sha256(f"{device}\0{attempt.owner}\0{attempt.token}".encode()).hexdigest()
    return "\0modelark-slice-attempt-" + key


class Lease:
    """Two kernel-held descriptors: device exclusion and unique live-attempt identity.

    Markers are bound datagram endpoints, queried by connect only (no messages/listener/thread).
    Always close marker before exclusion. Fork children inherit both; a writing exec child must
    inherit both fds and follow this release order. The engine itself creates no writer children.
    """
    def __init__(self, device, owner=None):
        from .transaction import TransferRefusal
        self.device, self.pid = device, os.getpid()
        self.attempt = Attempt(owner, uuid.uuid4().hex) if owner is not None else None
        self.marker = None
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.socket.bind("\0modelark-slice-device-" + hashlib.sha256(device.encode()).hexdigest())
            if self.attempt is not None:
                self.marker = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
                self.marker.bind(_marker_address(device, self.attempt))
        except OSError as exc:
            self.close()
            if exc.errno == errno.EADDRINUSE:
                raise TransferRefusal("DESTINATION_BUSY") from exc
            raise

    @staticmethod
    def retained(device, attempt):
        if attempt is None:
            return False
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as query:
            query.setblocking(False)
            try:
                query.connect(_marker_address(device, attempt))
            except OSError:
                return False  # No current proof, including an endpoint disappearing during query.
        return True

    def close(self):
        if self.marker is not None:
            self.marker.close()
        self.socket.close()

    def check(self):
        from .transaction import TransferRefusal
        if (os.getpid() != self.pid or self.socket.fileno() < 0
                or (self.attempt is not None and self.marker.fileno() < 0)):
            raise TransferRefusal("EXECUTION_FENCE_LOST")


class DeliveryAuthority:
    """The sole execution facade for activation, boundaries, outcomes and release."""
    def __init__(self, store, tx, device, lease):
        self.store, self.tx, self.device, self.lease = store, tx, device, lease
        self.attempt = lease.attempt

    @classmethod
    def acquire(cls, store, tx, device, reservation):
        from .transaction import TransferRefusal
        try:
            lease = Lease(device, tx)
        except TransferRefusal:
            current = store.status(tx)
            if current.state == "complete":
                return current
            attempt = store.current_attempt(device)
            if (attempt is not None and attempt.owner == tx and Lease.retained(device, attempt)
                    and store.current_attempt(device) == attempt):
                return store.status(tx)  # Retained attempt exclusion, not a progress/heartbeat claim.
            raise
        authority = cls(store, tx, device, lease)
        try:
            current = store.status(tx)
            if current.state == "complete":
                lease.close()
                return current
            outcome = store.claim(tx, device, lease.attempt, reservation)
            if outcome.state in {"complete", "stopped"}:
                lease.close()
                if outcome.state == "stopped":
                    raise TransferRefusal("STOPPED")
                return outcome
            return authority
        except BaseException:
            lease.close()
            raise

    def activate(self):
        from .transaction import TransferRefusal
        self.lease.check()
        outcome = self.store.transition(self.attempt, self.device, "transferring")
        if outcome.state == "stopped":
            raise TransferRefusal("STOPPED")
        return outcome

    def boundary(self):
        from .transaction import TransferRefusal
        self.lease.check()
        stopped, head = self.store.guard(self.attempt, self.device)
        if stopped:
            raise TransferRefusal("STOPPED")
        return head

    def transition(self, state, reason=""):
        from .transaction import TransferRefusal
        self.lease.check()
        outcome = self.store.transition(self.attempt, self.device, state, reason)
        if outcome.state == "stopped" and state != "stopped":
            raise TransferRefusal("STOPPED")
        return outcome

    def append(self, event, payload, *, expected_head):
        self.lease.check()
        return self.store.append(self.tx, event, payload, expected_head=expected_head,
                                 attempt=self.attempt, device=self.device)

    def complete(self):
        self.lease.check()
        self.store.complete(self.attempt, self.device, self.lease.close)

    def refuse(self, exc):
        # A failed/closed capability may never publish a later failure over a new attempt.
        return self.transition(refusal_state(exc.code), str(exc))

    def close(self):
        try:
            if self.lease.socket.fileno() >= 0 and self.lease.pid == os.getpid():
                current = self.store.status(self.tx)
                if current.state in {"starting", "transferring", "verifying"}:
                    self.transition("stopped")
        finally:
            self.lease.close()
