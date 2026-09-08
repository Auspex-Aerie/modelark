"""One ModelArk launch per host Linux network namespace, before app side effects.

The fixed abstract address is independent of user, catalog, state directory and
portal port. The kernel owns lifetime: no pidfiles, stale-owner takeover, waits,
or process-local reentrancy. Fork descendants retain the descriptor until they
close it or exit; exec children do not inherit it unless explicitly passed.
Existing archive/device/attempt fences remain defense in depth.
"""
from contextlib import contextmanager
import errno
import os
import socket
import sys


_ADDRESS = "\0modelark-instance-v1"


class _LaunchPermit:
    """Internal single-use CLI-to-portal handoff, not a second launch exemption."""

    def __init__(self, held):
        self._held = held
        self._pid = os.getpid()
        self._server_used = False

    def consume_server(self):
        if (self._pid != os.getpid() or self._held.fileno() < 0 or self._server_used):
            raise SystemExit("invalid or already used ModelArk launch permit")
        self._server_used = True


@contextmanager
def launch():
    """Reject an additional CLI/direct portal launch immediately, before parsing."""
    if sys.platform != "linux":
        raise SystemExit("ModelArk instance exclusion requires Linux abstract Unix sockets")
    held = None
    try:
        try:
            held = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            held.bind(_ADDRESS)
        except OSError as exc:
            if exc.errno == errno.EADDRINUSE:
                raise SystemExit(
                    "ModelArk is already running; close the existing instance before launching another."
                ) from None
            raise SystemExit(f"cannot establish ModelArk instance exclusion: {exc}") from None
        yield _LaunchPermit(held)
    finally:
        if held is not None:
            held.close()
