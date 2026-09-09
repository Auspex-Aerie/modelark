"""Launch-guarded native folder assembly. No Fill/catalog mutation or retrieval."""
from dataclasses import asdict, replace
import os
from pathlib import Path, PurePosixPath

from . import domain as d
from . import transaction as t
from . import state as private_state
from .catalog import read_catalog
from .folder_contract import _component
from .folder_destination import NativeFolderDestination
from .folder_observation import NativeFolderObserver
from .folder_plan import NativePlan, binding_for, estimate_metadata
from .hardware import LinuxObserver
from .host_observation import parse_mounts, read_fs_text
from .io_errors import io_boundary
from .linux import BoundTree
from .local_source import LocalArchiveReader
from .paths import canonical_attachment, utf8_size
from .sources import FencedSources


def _protected(catalog):
    # Actual runtime code, catalog and private state are roles, not whole disks.
    return (str(private_state.HOST_STATE_DIR), str(catalog), str(Path(__file__).resolve().parents[1]))


def _filesystem_type(parent):
    """Read-only routing hint, never filesystem or destination admission.

    The selected observer must still prove the actual retained parent, complete
    mount/backing/role evidence and capabilities. No fallback after an admission
    refusal is permitted, including a topology change after this hint was read.
    """
    with io_boundary(None, "DESTINATION_UNPROVEN", "folder profile routing inventory unavailable"):
        mounts = parse_mounts(read_fs_text("/proc/self/mountinfo"))
    covering = [mount for mount in mounts if parent.is_relative_to(mount.path)]
    if not covering:
        raise t.TransferRefusal("DESTINATION_UNPROVEN", "parent has no covering mount")
    longest = max(len(mount.path) for mount in covering)
    selected = [mount for mount in covering if len(mount.path) == longest]
    if len(selected) != 1:
        raise t.TransferRefusal("DESTINATION_UNPROVEN", "ambiguous folder profile routing")
    return selected[0].fs_type


def _layout(proposal, evidence):
    root = proposal.spec.destination_root
    paths = [str(PurePosixPath(root) / item.repo_id / item.rfilename) for item in proposal.closure]
    paths += [root + "/.modelark-slice-owner", root + "/.modelark-slice-receipt.json"]
    names = set(paths)
    for path in paths:
        value = PurePosixPath(path)
        if any(str(parent) in names for parent in value.parents):
            raise t.TransferRefusal("DESTINATION_LAYOUT_UNSUPPORTED", "file/directory layout collision")
        for part in value.parts:
            _component(part)
            if utf8_size(part) > evidence.name_max or part.startswith(".slice-"):
                raise t.TransferRefusal("DESTINATION_LAYOUT_UNSUPPORTED", "unsupported or reserved output name")
        temporary = str(value.parent / (".slice-" + "0" * 32))
        for name in (path, temporary):
            if utf8_size(name) >= 4096 or utf8_size(evidence.parent_path + "/" + name) >= 4096:
                raise t.TransferRefusal("DESTINATION_LAYOUT_UNSUPPORTED", "output or temporary path too long")


def preview(catalog_path, destination_path, repo_ids):
    from .operator import _archives, _store
    catalog = str(Path(catalog_path).expanduser().resolve())
    destination = canonical_attachment(destination_path)
    spec = d.SliceSpec(tuple(repo_ids), "pending-folder-observation", destination.name)
    snapshot = read_catalog(catalog, spec)
    proposal = d.preview(spec, snapshot)
    if not proposal.source_ready:
        return {"ok": False, "state": "blocked", "executable": False,
                "gaps": [asdict(gap) for gap in proposal.gaps], "preview": asdict(proposal)}
    if _filesystem_type(destination.parent) == "vfat":
        from .fat32_operator import preview as fat32_preview
        return fat32_preview(catalog, destination, repo_ids)
    observer = NativeFolderObserver()
    archives = _archives(catalog)
    evidence = observer.observe(destination, archives=archives, protected_paths=_protected(catalog))
    proposal = d.preview(replace(spec, destination_id=evidence.target.target_id), snapshot)
    _layout(proposal, evidence)
    binding = binding_for(evidence.target, catalog, evidence.parent_path, evidence.backing_ids)
    reserve = estimate_metadata(proposal, binding, catalog, evidence.parent_path, evidence.backing_ids,
                                evidence.capacity, private_state.HOST_STATE_DIR)
    plan = NativePlan(proposal, binding, catalog, evidence.parent_path, evidence.backing_ids,
                      evidence.capacity, reserve)
    with io_boundary(None, "DESTINATION_UNPROVEN", "native folder preview setup/teardown failed"), \
            BoundTree(evidence.parent_path, writable=True) as tree:
        fresh = observer.recheck(tree, evidence, archives=_archives(catalog))
        if fresh.capacity.available_bytes < plan.required_bytes(private_state.HOST_STATE_DIR):
            raise t.TransferRefusal("DESTINATION_CAPACITY_WAIT", "shared capacity insufficient at preview")
        if fresh.capacity.free_inodes is None:
            raise t.TransferRefusal("DESTINATION_CAPACITY_UNPROVEN", "native free inode observation required")
        if fresh.capacity.free_inodes < plan.required_inodes:
            raise t.TransferRefusal("DESTINATION_CAPACITY_WAIT", "shared inode capacity insufficient at preview")
        # A caller may have occupied the name since the initial read-only observation.
        with tree.parent(destination.name) as (parent, name):
            try:
                os.stat(name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise t.TransferRefusal("OUTPUT_COLLISION", "output folder already exists")
    approval = d.approve(proposal, expected_seal=proposal.seal, current_snapshot=snapshot)
    tx = _store().create(plan, approval)
    return {"ok": True, "state": "ready", "executable": False, "transaction_id": tx,
            "seal": plan.seal, "plan": asdict(plan), "ownership": "new-output-folder-only",
            "capacity_policy": "advisory-shared-space-not-reserved", "resume_policy": "authenticated"}


def start(store, tx, plan, destination_path, attachments):
    from .operator import _archives, _result, _acknowledge_interrupt
    path = canonical_attachment(destination_path)
    if str(path.parent) != plan.parent_path or path.name != plan.destination.target.child_name:
        raise t.TransferRefusal("DESTINATION_CHANGED", "Start must name the approved output folder")
    allowed = {source.drive.drive_label for artifact in plan.proposal.closure for source in artifact.sources}
    if not isinstance(attachments, dict) or set(attachments) - allowed:
        raise t.TransferRefusal("SOURCE_ATTACHMENT_UNSEALED")
    observer = NativeFolderObserver()
    evidence = observer.observe(path, archives=_archives(plan.catalog),
                                protected_paths=_protected(plan.catalog), allow_existing=True)
    if (evidence.target != plan.destination.target or evidence.backing_ids != plan.backing_ids):
        raise t.TransferRefusal("DESTINATION_CHANGED", "folder parent or backing differs from approval")
    _layout(plan.proposal, evidence)
    with io_boundary(None, "DESTINATION_IO_FAILED", "native folder setup/teardown failed"), \
            BoundTree(plan.parent_path, writable=True) as tree:
        def recheck(parent):
            return observer.recheck(parent, evidence, archives=_archives(plan.catalog))

        with io_boundary(lambda: recheck(tree), "DESTINATION_IO_FAILED", "native folder execution failed"):
            recheck(tree)
            destination = NativeFolderDestination(tree, plan.destination, store, tx, recheck=recheck)
            # Destination folder eligibility does not replace archive source proof.
            sources = FencedSources(plan.catalog, LocalArchiveReader(attachments, observer=LinuxObserver()))
            try:
                session = t.start(store, tx, destination, sources)
            except KeyboardInterrupt:
                return _result(_acknowledge_interrupt(store, tx, destination, sources),
                               stop_requested=True)
            except t.TransferRefusal as exc:
                if exc.code == "DESTINATION_CAPACITY_WAIT":
                    return _result(store.status(tx))
                raise
            if isinstance(session, t.Status):
                return _result(session)
            with session:
                try:
                    return _result(session.run())
                except KeyboardInterrupt:
                    # Acknowledge while this attempt's lease is still retained.
                    return _result(_acknowledge_interrupt(store, tx, destination, sources, session),
                                   stop_requested=True)
