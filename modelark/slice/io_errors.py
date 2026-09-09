"""Preserve probe evidence until an attachment-aware boundary chooses the outcome."""
from functools import wraps
from contextlib import contextmanager

from .transaction import TransferRefusal


class ProbeFailure(OSError):
    """An IO failure plus its fallback meaning, not yet a transaction refusal."""

    def __init__(self, original, code, detail):
        super().__init__(original.errno, original.strerror, original.filename)
        self.original = original
        self.filename2 = original.filename2
        self.code, self.detail = code, detail + ': ' + str(original)


def probe_io(code, detail):
    """Annotate IO failure without swallowing it or overriding a narrower probe."""
    def decorate(method):
        @wraps(method)
        def call(*args, **kwargs):
            try:
                return method(*args, **kwargs)
            except ProbeFailure:
                raise
            except OSError as exc:
                raise ProbeFailure(exc, code, detail) from exc
        return call
    return decorate


def attachment_refusal(check):
    """Return only conclusive raw attachment evidence, never an unknown probe error."""
    if check is not None:
        try:
            check()
        except TransferRefusal as refusal:
            if refusal.code in {'WAITING_DESTINATION', 'DESTINATION_CHANGED'}:
                return refusal
        except OSError:
            pass
    return None


def classify_io(exc, check, fallback):
    """Only proven attachment loss/replacement overrides the original IO meaning.

    check is a raw, read-only proof, never another error classifier. An unavailable
    re-probe is not absence evidence; neither errno nor a generic refusal proves loss.
    """
    refusal = attachment_refusal(check)
    if refusal is not None:
        return refusal
    if isinstance(exc, ProbeFailure):
        return TransferRefusal(exc.code, exc.detail)
    return fallback


@contextmanager
def io_boundary(check, code, detail):
    """Classify only IO executed inside this boundary, not a consumer's work."""
    try:
        yield
    except OSError as exc:
        raise classify_io(exc, check, TransferRefusal(code, detail + ': ' + str(exc))) from exc
