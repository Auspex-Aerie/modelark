"""Attended FAT32 folder assembly: approved intent, one live Start, no resume.

Kernel-vfat port qualification passed on the operator's disposable image. Every
actual destination still requires closed mount/backing/role/capability admission.
"""
from dataclasses import asdict, replace
import os
from pathlib import Path

from . import domain as d, state as private_state, transaction as t
from .authority import PreclaimInterrupted
from .catalog import read_catalog
from .fat32_destination import Fat32Destination
from .fat32_observation import Fat32FolderObserver, Fat32Tree
from .fat32_plan import Fat32Intent, Fat32Plan, binding_for, estimate_metadata
from .folder_contract import FolderProfile
from .folder_operator import _protected
from .hardware import LinuxObserver
from .io_errors import io_boundary
from .local_source import LocalArchiveReader
from .paths import canonical_attachment
from .sources import FencedSources
from modelark.artifact_policy import qualified_policy
from .fat32_plan import POLICY_VERSION


def _intent(evidence):
    return Fat32Intent(FolderProfile.FAT32_SESSION, evidence.filesystem_scope,
                       evidence.parent_parts, evidence.child_name)


def _request_stop_once(store, tx):
    # An interrupt inside the engine has already requested/acknowledged Stop
    # under its retained lease. Do not append a new unacknowledged request.
    if not store.stop_requested(tx):
        store.request_stop(tx)


def preview(catalog_path, destination_path, repo_ids):
    from .operator import _archives, _store
    catalog = str(Path(catalog_path).expanduser().resolve())
    destination = canonical_attachment(destination_path)
    spec = d.SliceSpec(tuple(repo_ids), "pending-fat32-intent", destination.name)
    snapshot = read_catalog(catalog, spec)
    proposal = d.preview(spec, snapshot)
    if not proposal.source_ready:
        return {"ok": False, "state": "blocked", "executable": False,
                "gaps": [asdict(gap) for gap in proposal.gaps], "preview": asdict(proposal)}
    observer = Fat32FolderObserver()
    evidence = observer.observe(destination, archives=_archives(catalog), protected_paths=_protected(catalog))
    target = _intent(evidence)
    proposal = d.preview(replace(spec, destination_id=target.target_id), snapshot)
    policy = qualified_policy()
    binding = binding_for(target, catalog, evidence.parent_path, evidence.backing_ids, policy)
    reserve = estimate_metadata(proposal, binding, catalog, evidence.parent_path, evidence.backing_ids,
                                evidence.capacity, private_state.HOST_STATE_DIR, policy)
    plan = Fat32Plan(proposal, binding, catalog, evidence.parent_path, evidence.backing_ids,
                     evidence.capacity, reserve, POLICY_VERSION, policy)
    with io_boundary(None, "DESTINATION_UNPROVEN", "FAT32 preview setup/teardown failed"), \
            Fat32Tree(evidence.parent_path, writable=True) as tree:
        fresh = observer.recheck(tree, evidence, archives=_archives(catalog))
        if fresh.capacity.available_bytes < plan.required_bytes(private_state.HOST_STATE_DIR):
            raise t.TransferRefusal("DESTINATION_CAPACITY_WAIT", "shared capacity insufficient at preview")
        try:
            os.stat(destination.name, dir_fd=tree.fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise t.TransferRefusal("OUTPUT_COLLISION", "output appeared during preview")
    approval = d.approve(proposal, expected_seal=proposal.seal, current_snapshot=snapshot)
    tx = _store().create(plan, approval)
    return {"ok": True, "state": "ready", "executable": False, "transaction_id": tx,
            "seal": plan.seal, "plan": asdict(plan), "ownership": "new-output-folder-only",
            "parent_continuity": "fresh-at-start-not-persistently-identified",
            "resume_policy": "new-root-after-any-ended-attempt",
            "capacity_policy": "advisory-shared-space-not-reserved"}


def start(store, tx, plan, destination_path, attachments):
    from .operator import _archives, _result
    if store.attempt_consumed(tx):
        raise t.TransferRefusal("FAT32_NEW_ROOT_REQUIRED", "this intent has spent its only attempt")
    path = canonical_attachment(destination_path)
    if str(path.parent) != plan.parent_path or path.name != plan.destination.target.child_name:
        raise t.TransferRefusal("DESTINATION_CHANGED", "Start must name the approved output path")
    allowed = {source.drive.drive_label for artifact in plan.proposal.closure for source in artifact.sources}
    if not isinstance(attachments, dict) or set(attachments) - allowed:
        raise t.TransferRefusal("SOURCE_ATTACHMENT_UNSEALED")
    observer = Fat32FolderObserver()
    evidence = observer.observe(path, archives=_archives(plan.catalog), protected_paths=_protected(plan.catalog))
    if _intent(evidence) != plan.destination.target or evidence.backing_ids != plan.backing_ids:
        raise t.TransferRefusal("DESTINATION_CHANGED", "FAT path/backing differs from approved intent")
    with io_boundary(None, "DESTINATION_IO_FAILED", "FAT32 setup/teardown failed"), \
            Fat32Tree(plan.parent_path, writable=True) as tree:
        def recheck(parent):
            return observer.recheck(parent, evidence, archives=_archives(plan.catalog))

        fresh = recheck(tree)
        if fresh.capacity.available_bytes < plan.required_bytes(store.root):
            raise t.TransferRefusal("DESTINATION_CAPACITY_WAIT", "shared capacity insufficient before claim")
        with Fat32Destination(tree, plan.destination, store, tx, recheck=recheck) as destination:
            options = {"policy": plan.decode_policy} if plan.decode_policy is not None else {}
            sources = FencedSources(plan.catalog, LocalArchiveReader(attachments, observer=LinuxObserver(), **options))
            try:
                session = t.start(store, tx, destination, sources)
            except PreclaimInterrupted as exc:
                return {**_result(exc.outcome, stop_requested=True), "ok": False,
                        "new_root_required": False}
            except KeyboardInterrupt:
                _request_stop_once(store, tx)
                # Never use native's fresh-Start interrupt acknowledgment path:
                # consumed FAT authority cannot be reacquired even to acknowledge.
                return {**_result(store.status(tx), stop_requested=True), "ok": False,
                        "new_root_required": store.attempt_consumed(tx)}
            except t.TransferRefusal as exc:
                if exc.code not in {"DESTINATION_CAPACITY_WAIT", "WAITING_DESTINATION", "STOPPED"}:
                    raise
                return {**_result(store.status(tx)), "new_root_required": store.attempt_consumed(tx)}
            if isinstance(session, t.Status):
                return {**_result(session), "new_root_required":
                        session.state != "complete" and store.attempt_consumed(tx)}
            with session:
                try:
                    result = session.run()
                except KeyboardInterrupt:
                    _request_stop_once(store, tx)
                    # step() has already revoked the port on an interrupted run.
                    # close() may acknowledge Stop with host authority if retained.
                    interrupted = True
                else:
                    interrupted = False
            if interrupted:
                return {**_result(store.status(tx), stop_requested=True), "ok": False,
                        "new_root_required": store.attempt_consumed(tx)}
            return {**_result(result), "new_root_required": result.state != "complete"}
