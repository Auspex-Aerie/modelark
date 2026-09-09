"""Non-executable FAT32 candidate layout preflight (DEC-130, Gate C).

This deliberately restricted ASCII grammar is not driver/alias qualification.
In particular, rejecting requested '~' names is insufficient on a mount using
nonumtail. No result is a plan, live parent binding, lease, or ownership proof.
Control/report sizes must be computed from the eventual exact sealed protocol.
"""
from dataclasses import dataclass
from pathlib import PurePosixPath
import re

from .folder_plan import _absolute
from .transaction import TransferRefusal


MAX_FILE_BYTES = (1 << 32) - 1
MAX_COMPONENT = 255
MAX_PATH_BYTES = 4096
_ASCII_NAME = re.compile(r"[A-Za-z0-9_. -]+\Z")
_DEVICES = {"CON", "PRN", "AUX", "NUL", "CLOCK$",
            *("COM" + str(n) for n in range(1, 10)),
            *("LPT" + str(n) for n in range(1, 10))}
_TEMPORARY = ".slice-" + "0" * 32


def _refuse(detail):
    raise TransferRefusal("DESTINATION_LAYOUT_UNSUPPORTED", detail)


def _name(value, *, generated=False):
    if (not isinstance(value, str) or not _ASCII_NAME.fullmatch(value)
            or value in {".", ".."} or value != value.strip()
            or value.endswith(".") or len(value) > MAX_COMPONENT):
        _refuse("unsupported FAT32 component")
    if value.split(".", 1)[0].upper() in _DEVICES:
        _refuse("reserved DOS device name")
    if value.lower().startswith(".slice-") and not generated:
        _refuse("reserved temporary namespace")


def _size(value):
    if type(value) is not int or not 0 <= value <= MAX_FILE_BYTES:
        _refuse("FAT32 requires each file, control and report to fit 32-bit size")


@dataclass(frozen=True)
class Fat32Layout:
    """Candidate paths only; never interpreted by the private execution store."""

    files: tuple[tuple[str, int], ...]
    directories: tuple[str, ...]
    temporary_paths: tuple[str, ...]

    @property
    def execution_ready(self):
        return False


def admit_layout(files, *, output_root, parent_path, control_size, receipt_size):
    """Check the whole proposed tree without IO, adoption, renaming or splitting.

    Input payload paths are relative to the intended output child. Directory
    spellings must agree exactly even where FAT would collapse ASCII case.
    Generated staging paths use fixed-length transaction tokens; repeated paths
    in temporary_paths are templates, not an allocation or uniqueness claim.
    Parent is a host attachment spelling, not a newly generated FAT pathname;
    parent alias/case eligibility belongs to the future live observer.
    """
    _name(output_root)
    try:
        _absolute(parent_path)
    except TransferRefusal as exc:
        _refuse(str(exc))
    _size(control_size)
    _size(receipt_size)
    if isinstance(files, (str, bytes, dict)):
        _refuse("payload path/size pairs required")
    try:
        records = tuple(files)
    except TypeError:
        _refuse("payload path/size pairs required")
    if not records:
        _refuse("nonempty payload layout required")

    def path_length(path):
        full = parent_path.rstrip("/") + "/" + path
        if len(path.encode("utf-8")) >= MAX_PATH_BYTES or len(full.encode("utf-8")) >= MAX_PATH_BYTES:
            _refuse("FAT32 output or intermediate path exceeds qualified path bound")

    paths = []
    for record in records:
        if not isinstance(record, (tuple, list)) or len(record) != 2:
            _refuse("payload path/size pair required")
        path, size = record
        if not isinstance(path, str):
            _refuse("relative payload path required")
        for part in path.split("/"):
            _name(part)
        _size(size)
        paths.append((output_root + "/" + path, size))
    paths += [(output_root + "/.modelark-slice-owner", control_size),
              (output_root + "/.modelark-slice-receipt.json", receipt_size)]

    namespace = {}
    directories = set()
    temporary_paths = []
    for path, _ in paths:
        parts = PurePosixPath(path).parts
        for index, part in enumerate(parts):
            _name(part)
            current = "/".join(parts[:index + 1])
            kind = "file" if index == len(parts) - 1 else "directory"
            key = current.lower()
            prior = namespace.get(key)
            if prior is not None and (prior != (current, kind) or kind == "file"):
                _refuse("duplicate, case-equivalent or file/directory collision")
            namespace[key] = (current, kind)
            if kind == "directory":
                directories.add(current)
        path_length(path)
        temporary = str(PurePosixPath(path).parent / _TEMPORARY)
        _name(_TEMPORARY, generated=True)
        path_length(temporary)
        temporary_paths.append(temporary)
    return Fat32Layout(tuple(paths), tuple(sorted(directories)), tuple(temporary_paths))
