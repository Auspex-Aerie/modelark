"""Pure archive-only slice proposal and approval contracts (DEC-101 through DEC-103).

A domain seal binds catalog evidence and destination *intent*. It is not a transfer approval:
hardware preflight and durable execution ownership must be added in the later implementation
slices. These objects are internal trusted-process values, not signed capabilities from clients.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import hashlib
import json
from pathlib import PurePosixPath, PureWindowsPath
import re

from modelark import archive_hash
from modelark.capacity_evidence import identity_fingerprint_v1


DOMAIN_VERSION = "modelark.slice.domain.v1"
PROFILE = "hf-tree-v1"
_SHA256 = re.compile(r"[0-9a-fA-F]{64}\Z")
_ANNEX = re.compile(r"SHA256E?-s(\d+)--[0-9a-f]{64}(?:\.[^/\\\x00]*)?\Z")


class SliceRefusal(ValueError):
    def __init__(self, code: str, detail: str = ""):
        self.code, self.detail = code, detail
        super().__init__(f"{code}: {detail}")


def _path(value: str) -> bool:
    if not isinstance(value, str) or not value or "\\" in value or "\0" in value:
        return False
    rel = PurePosixPath(value)
    return (not rel.is_absolute() and not PureWindowsPath(value).drive
            and ".." not in rel.parts and value != "." and value == rel.as_posix())


def _digest(value) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def _integer(value) -> bool:
    return type(value) is int and value >= 0


def _json(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("utf-8")


def _hash(value) -> str:
    return hashlib.sha256(_json(value)).hexdigest()


@dataclass(frozen=True)
class SliceSpec:
    repo_ids: tuple[str, ...]
    destination_id: str
    destination_root: str
    consumer_profile: str = PROFILE

    def __post_init__(self):
        if not isinstance(self.repo_ids, (tuple, list)):
            raise SliceRefusal("INVALID_SPEC", "repositories must be an explicit sequence")
        repos = tuple(self.repo_ids)
        if (not repos
                or any(not _path(r) or len(PurePosixPath(r).parts) != 2 for r in repos)
                or not isinstance(self.destination_id, str) or not self.destination_id.strip()
                or not _path(self.destination_root)):
            raise SliceRefusal("INVALID_SPEC", "explicit repositories and safe destination intent required")
        object.__setattr__(self, "repo_ids", tuple(sorted(set(repos))))
        if self.consumer_profile != PROFILE:
            raise SliceRefusal("UNSUPPORTED_PROFILE", str(self.consumer_profile))


@dataclass(frozen=True)
class FileFact:
    repo_id: str
    rfilename: str
    size_bytes: int | None
    sha256: str | None
    format: str | None
    quant: str | None


@dataclass(frozen=True)
class CopyFact:
    repo_id: str
    rfilename: str
    drive_label: str
    stored_relpath: str | None
    orig_bytes: int | None
    stored_bytes: int | None
    orig_sha256: str | None
    provenance: str | None
    annex_key: str | None
    compressed: bool
    present: bool | None = None


@dataclass(frozen=True)
class DriveFact:
    drive_label: str
    fs_uuid: str | None
    annex_uuid: str | None
    serial: str | None
    identity_epoch: int
    write_generation: int
    identity_fingerprint: str | None
    filesystem_capacity_bytes: int | None
    write_authority: str
    lifecycle: str
    eligibility: str


@dataclass(frozen=True)
class AnchorFact:
    drive_label: str
    identity_epoch: int
    generation: int
    identity_fingerprint: str
    filesystem_capacity_bytes: int
    write_authority: str
    anchor_id: str


@dataclass(frozen=True)
class Gap:
    repo_id: str
    rfilename: str | None
    code: str
    drive_label: str | None = None
    detail: str = ""


@dataclass(frozen=True)
class CatalogSnapshot:
    catalog_id: str
    files: tuple[FileFact, ...]
    copies: tuple[CopyFact, ...]
    drives: tuple[DriveFact, ...]
    anchors: tuple[AnchorFact, ...]
    issues: tuple[Gap, ...] = ()
    schema_version: int = 7

    def __post_init__(self):
        # Copy caller-owned containers; facts themselves are frozen scalar records.
        for name, cls in (("files", FileFact), ("copies", CopyFact), ("drives", DriveFact),
                          ("anchors", AnchorFact), ("issues", Gap)):
            items = tuple(getattr(self, name))
            if any(type(item) is not cls for item in items):
                raise SliceRefusal("INVALID_SNAPSHOT", name)
            if any(type(value) not in (str, int, bool, type(None))
                   for item in items for value in asdict(item).values()):
                raise SliceRefusal("INVALID_SNAPSHOT", f"non-scalar {name} evidence")
            object.__setattr__(self, name, tuple(sorted(items, key=lambda item: _json(asdict(item)))))

    @property
    def snapshot_id(self) -> str:
        return _hash(asdict(self))


@dataclass(frozen=True)
class SourceEvidence:
    copy: CopyFact
    drive: DriveFact
    anchor: AnchorFact
    digest_provenance: str


@dataclass(frozen=True)
class Artifact:
    repo_id: str
    rfilename: str
    size_bytes: int
    sha256: str
    format: str | None
    quant: str | None
    sources: tuple[SourceEvidence, ...]

    def __post_init__(self):
        object.__setattr__(self, "sources", tuple(self.sources))


@dataclass(frozen=True)
class SlicePreview:
    spec: SliceSpec
    snapshot_id: str
    closure: tuple[Artifact, ...]
    gaps: tuple[Gap, ...]
    seal: str
    version: str = DOMAIN_VERSION

    def __post_init__(self):
        object.__setattr__(self, "closure", tuple(self.closure))
        object.__setattr__(self, "gaps", tuple(self.gaps))

    @property
    def source_ready(self) -> bool:
        return bool(self.closure) and not self.gaps

    @property
    def execution_ready(self) -> bool:
        return False  # No hardware preflight or durable execution owner in Slice 1.

    @property
    def total_bytes(self) -> int:
        """Resolved original bytes only; blocked previews may have a partial closure."""
        return sum(item.size_bytes for item in self.closure)

    @property
    def required_drives(self) -> tuple[str, ...]:
        """Preferred source schedule; each artifact retains its sealed alternatives."""
        return tuple(sorted({item.sources[0].drive.drive_label for item in self.closure}))

    def canonical_bytes(self) -> bytes:
        payload = asdict(self)
        del payload["seal"]
        for item in payload["closure"]:
            for source in item["sources"]:
                # Placement eligibility is captured annotation, never read-source authority.
                del source["drive"]["eligibility"]
        return _json(payload)


@dataclass(frozen=True)
class SliceApproval:
    preview_seal: str
    stage: str = "domain"
    version: str = DOMAIN_VERSION


def _index(items, key):
    result = {}
    for item in items:
        identity = key(item)
        if identity in result:
            raise SliceRefusal("INVALID_SNAPSHOT", f"duplicate evidence: {identity}")
        result[identity] = item
    return result


def _scope(snapshot: CatalogSnapshot, spec: SliceSpec) -> CatalogSnapshot:
    files = tuple(f for f in snapshot.files if f.repo_id in spec.repo_ids)
    keys = {(f.repo_id, f.rfilename) for f in files}
    copies = tuple(c for c in snapshot.copies if (c.repo_id, c.rfilename) in keys)
    labels = {c.drive_label for c in copies}
    drives = tuple(d for d in snapshot.drives if d.drive_label in labels)
    current = {(d.drive_label, d.identity_epoch, d.write_generation) for d in drives}
    anchors = tuple(a for a in snapshot.anchors
                    if (a.drive_label, a.identity_epoch, a.generation) in current)
    return replace(snapshot, files=files, copies=copies, drives=drives, anchors=anchors,
                   issues=tuple(g for g in snapshot.issues if g.repo_id in spec.repo_ids))


def _source_snapshot_id(snapshot: CatalogSnapshot) -> str:
    payload = asdict(snapshot)
    for drive in payload["drives"]:
        del drive["eligibility"]
    return _hash(payload)


def _source(file, copy, drive, anchors):
    """Return a (digest, evidence) pair or one exact candidate refusal."""
    def fail(code, detail=""):
        return Gap(file.repo_id, file.rfilename, code, copy.drive_label, detail)

    if copy.present is False:
        return fail("SOURCE_COPY_ABSENT")
    if (drive is None
            or any(v is not None and not isinstance(v, str) for v in (drive.fs_uuid, drive.annex_uuid))
            or not any(v and v.strip() for v in (drive.fs_uuid, drive.annex_uuid))):
        return fail("SOURCE_IDENTITY_UNPROVEN")
    if drive.lifecycle != "active":
        return fail("SOURCE_INACTIVE", drive.lifecycle)
    if (not _integer(drive.identity_epoch) or drive.identity_epoch < 1
            or not _integer(drive.write_generation) or not drive.write_generation
            or not _integer(drive.filesystem_capacity_bytes)
            or drive.write_authority != "dedicated_local"):
        return fail("SOURCE_RECONCILIATION_REQUIRED", "current clean generation required")
    fingerprint = identity_fingerprint_v1(
        fs_uuid=drive.fs_uuid, annex_uuid=drive.annex_uuid, serial=drive.serial,
        filesystem_capacity_bytes=drive.filesystem_capacity_bytes)
    anchor = anchors.get((drive.drive_label, drive.identity_epoch, drive.write_generation))
    if (drive.identity_fingerprint != fingerprint or anchor is None or not anchor.anchor_id
            or anchor.identity_fingerprint != fingerprint
            or anchor.filesystem_capacity_bytes != drive.filesystem_capacity_bytes
            or anchor.write_authority != "dedicated_local"):
        return fail("SOURCE_RECONCILIATION_REQUIRED", "anchor does not bind current identity/generation")
    if not _path(copy.stored_relpath):
        return fail("SOURCE_PATH_UNSAFE", str(copy.stored_relpath))
    key = _ANNEX.fullmatch(copy.annex_key or "")
    if key is None:
        return fail("SOURCE_IDENTITY_UNPROVEN", "canonical SHA256 annex identity required")
    if (not _integer(copy.orig_bytes) or copy.orig_bytes != file.size_bytes
            or not _integer(copy.stored_bytes) or int(key[1]) != copy.stored_bytes
            or (copy.compressed is False and copy.orig_bytes != copy.stored_bytes)):
        return fail("SOURCE_SIZE_MISMATCH")
    if type(copy.compressed) is not bool:
        return fail("SOURCE_PROVENANCE_UNPROVEN", "compression state unknown")
    if copy.orig_sha256 is not None and not _digest(copy.orig_sha256):
        return fail("SOURCE_PROVENANCE_UNPROVEN", "malformed original digest")
    try:
        digest = archive_hash.expected_sha256(
            catalog_sha=file.sha256, orig_sha256=copy.orig_sha256,
            compressed=copy.compressed, annex_key=copy.annex_key)
    except archive_hash.DigestEvidenceError as exc:
        return fail("SOURCE_DIGEST_CONFLICT", str(exc))
    provenance = copy.provenance
    if (copy.orig_sha256 is not None and provenance in {"ingestion_computed", "hub_confirmed", "archive-head-blob"}
            and (provenance != "hub_confirmed" or file.sha256 is not None)
            and (provenance != "archive-head-blob" or not copy.compressed)):
        proof = provenance
    elif not copy.compressed:
        proof = "annex_key"  # Independent raw original-byte identity, not a database backfill.
    else:
        return fail("SOURCE_PROVENANCE_UNPROVEN", "no independently established original-byte digest")
    if not _digest(digest):
        return fail("SOURCE_PROVENANCE_UNPROVEN", "original digest unknown")
    return digest.lower(), SourceEvidence(copy, drive, anchor, proof)


def preview(spec: SliceSpec, snapshot: CatalogSnapshot) -> SlicePreview:
    if snapshot.schema_version != 7 or not snapshot.catalog_id:
        raise SliceRefusal("INVALID_SNAPSHOT", "schema v7 and snapshot provenance required")
    snapshot = _scope(snapshot, spec)
    files = _index(snapshot.files, lambda f: (f.repo_id, f.rfilename))
    _index(snapshot.copies, lambda c: (c.repo_id, c.rfilename, c.drive_label))
    drives = _index(snapshot.drives, lambda d: d.drive_label)
    anchors = _index(snapshot.anchors, lambda a: (a.drive_label, a.identity_epoch, a.generation))
    copies = {}
    for copy in snapshot.copies:
        copies.setdefault((copy.repo_id, copy.rfilename), []).append(copy)
    gaps = [g for g in snapshot.issues if g.repo_id in spec.repo_ids]
    closure = []
    selected = [f for f in files.values() if f.repo_id in spec.repo_ids]
    for repo in spec.repo_ids:
        if not any(f.repo_id == repo for f in selected) and not any(g.repo_id == repo for g in gaps):
            gaps.append(Gap(repo, None, "MANIFEST_UNAVAILABLE", detail="repository has no archive manifest"))
    output_paths = {f"{f.repo_id}/{f.rfilename}" for f in selected if _path(f.rfilename)}
    for file in sorted(selected, key=lambda f: (f.repo_id, f.rfilename)):
        def gap(code, detail=""):
            gaps.append(Gap(file.repo_id, file.rfilename, code, detail=detail))

        if not _path(file.rfilename):
            gap("ARTIFACT_PATH_UNSAFE", str(file.rfilename))
            continue
        output = PurePosixPath(file.repo_id) / file.rfilename
        if any(str(parent) in output_paths for parent in output.parents):
            gap("OUTPUT_COLLISION", str(output))
            continue
        if not _integer(file.size_bytes):
            gap("ARTIFACT_SIZE_UNKNOWN")
            continue
        if file.sha256 is not None and not _digest(file.sha256):
            gap("ARTIFACT_DIGEST_INVALID")
            continue
        available, rejected = [], []
        for copy in copies.get((file.repo_id, file.rfilename), ()):
            evidence = _source(file, copy, drives.get(copy.drive_label), anchors)
            if isinstance(evidence, Gap):
                rejected.append(evidence)
            else:
                available.append(evidence)
        if not available:
            gaps.extend(rejected or (Gap(file.repo_id, file.rfilename, "ARCHIVE_MISSING"),))
            continue
        if len({digest for digest, _ in available}) != 1:
            gap("SOURCE_DIGEST_AMBIGUOUS", "qualifying archived originals disagree")
            continue
        sources = tuple(sorted((src for _, src in available), key=lambda src: src.drive.drive_label))
        closure.append(Artifact(file.repo_id, file.rfilename, file.size_bytes, available[0][0],
                                file.format, file.quant, sources))
    p = SlicePreview(spec, _source_snapshot_id(snapshot), tuple(closure),
                     tuple(sorted(gaps, key=lambda g: _json(asdict(g)))), "")
    return replace(p, seal=hashlib.sha256(p.canonical_bytes()).hexdigest())


def _verify(preview: SlicePreview) -> None:
    if (preview.version != DOMAIN_VERSION
            or preview.seal != hashlib.sha256(preview.canonical_bytes()).hexdigest()):
        raise SliceRefusal("PREVIEW_TAMPERED")


def approve(proposal: SlicePreview, *, expected_seal: str,
            current_snapshot: CatalogSnapshot) -> SliceApproval:
    """Pure approval of reviewed domain intent; caller supplies a fresh trusted snapshot.

    No persistence, CAS, live preflight, or Start is performed. The later state store must
    authenticate the caller and persist the approved artifact before any execution is possible.
    """
    _verify(proposal)
    if expected_seal != proposal.seal:
        raise SliceRefusal("PREVIEW_STALE", "reviewed seal differs")
    if not proposal.source_ready:
        raise SliceRefusal("PREVIEW_BLOCKED")
    if preview(proposal.spec, current_snapshot).seal != proposal.seal:
        raise SliceRefusal("PREVIEW_STALE", "catalog evidence changed since preview")
    return SliceApproval(proposal.seal)


def validate_approval(proposal: SlicePreview, approval: SliceApproval) -> None:
    """Validate the stored domain pair, not current source availability or hardware readiness."""
    _verify(proposal)
    if (approval.stage != "domain" or approval.version != DOMAIN_VERSION
            or approval.preview_seal != proposal.seal or not proposal.source_ready):
        raise SliceRefusal("APPROVAL_MISMATCH")
