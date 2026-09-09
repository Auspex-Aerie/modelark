"""Slice attachment spelling and output encoding, without filesystem resolution."""
import os
from pathlib import Path


def canonical_attachment(path):
    """Normalize an attachment root lexically; BoundTree still confines its opening.

    Host paths retain filesystem encoding semantics. This is not output-path
    validation and must not be applied to sealed relative paths or catalog paths.
    """
    return Path(os.path.abspath(Path(path).expanduser()))


def utf8_size(value):
    """Count strict UTF-8 output bytes, refusing unsupported filesystem names."""
    try:
        return len(value.encode('utf-8'))
    except UnicodeError as exc:
        from .transaction import TransferRefusal
        raise TransferRefusal('DESTINATION_LAYOUT_UNSUPPORTED',
                              'non-UTF-8 output name') from exc
