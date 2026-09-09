"""Attended kernel-vfat test on a NEW disposable image; never accepts a device.

Run as the ordinary operator with .venv-dev Python. Only mount/unmount request
sudo. Image, private synthetic state and test outputs are retained under /tmp.
This tests driver/port behavior, not real archive or physical USB admission.
"""
import argparse
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch


def public_workflow(base, exports, mounted):
    """Real public routing and vfat I/O; source/hardware evidence is synthetic."""
    from modelark.slice import fat32_operator as fat, folder_operator as folder, operator
    from modelark.slice import transaction as t
    from modelark.slice.fat32_observation import (
        Fat32FolderEvidence, Fat32FolderObserver, Fat32Tree, _mount_policy, check_directory,
    )
    from modelark.slice.folder_contract import CapacityObservation
    from test_slice_transaction import DATA, Sources, proposal

    canonical = Fat32FolderObserver()
    _, _, snapshot = proposal()

    class SyntheticBackingObserver:
        def observe(self, destination, **kwargs):
            destination = Path(destination)
            with Fat32Tree(destination.parent) as retained:
                _mount_policy(mounted)
                check_directory(retained.fd)
                parts = canonical._canonical_parent(retained, mounted)
                try:
                    os.stat(destination.name, dir_fd=retained.fd, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    raise t.TransferRefusal("OUTPUT_COLLISION")
                fs = os.fstatvfs(retained.fd)
                return Fat32FolderEvidence(
                    str(destination.parent), str(destination), "synthetic-loop-uuid",
                    "synthetic-public-loop", parts, destination.name, ("synthetic-loop",),
                    retained.mount_id, mounted.major_minor,
                    CapacityObservation(fs.f_bavail * fs.f_frsize, None), 255, fs.f_frsize)

        def recheck(self, retained, evidence, **kwargs):
            retained.check()
            _mount_policy(mounted)
            check_directory(retained.fd)
            assert canonical._canonical_parent(retained, mounted) == evidence.parent_parts
            fs = os.fstatvfs(retained.fd)
            return replace(evidence, capacity=CapacityObservation(fs.f_bavail * fs.f_frsize, None))

    child = exports / "public-delivery"
    # Deliberately leave folder._filesystem_type unpatched: dispatch must read
    # the actual kernel mount table. Never fabricate physical USB admission.
    with (patch.object(fat, "Fat32FolderObserver", SyntheticBackingObserver),
          patch.object(fat, "read_catalog", return_value=snapshot),
          patch.object(folder, "read_catalog", return_value=snapshot),
          patch.object(operator, "read_catalog", return_value=snapshot),
          patch.object(operator, "_archives", return_value=()),
          patch.object(fat, "FencedSources", side_effect=lambda *args: Sources(snapshot, t))):
        preview = operator.preview(base / "synthetic-catalog-never-opened", child, ("org/model",), None)
        assert not child.exists()
        assert preview["resume_policy"] == "new-root-after-any-ended-attempt"
        operator.approve(preview["transaction_id"], preview["seal"])
        assert not child.exists()
        result = operator.start(preview["transaction_id"], child, {})
        assert result["state"] == "complete" and result["ok"]
        assert (child / "org/model/model.safetensors").read_bytes() == DATA
        report = json.loads((child / ".modelark-slice-receipt.json").read_text())
        assert report["host_transaction_completion"] == "not-claimed"
        assert not (exports / ".modelark-slice-owner").exists()


def parent_alias_checks(exports, mounted):
    from modelark.slice.fat32_observation import Fat32FolderObserver, Fat32Tree
    from modelark.slice.transaction import TransferRefusal

    canonical = Fat32FolderObserver()
    parent = exports / "LongParentForAlias"
    parent.mkdir()
    with Fat32Tree(parent) as exact:
        assert canonical._canonical_parent(exact, mounted)[-1] == parent.name
        # Known isolated test vector for shortname=mixed, codepage=437.
        # Require actual alias resolution before testing the refusal, so an
        # absent alias cannot be mistaken for successful protection.
        for spelling in ("longparentforalias", "LONGPA~1"):
            with Fat32Tree(exports / spelling) as alias:
                assert Fat32Tree.identity(alias.fd) == Fat32Tree.identity(exact.fd)
                try:
                    canonical._canonical_parent(alias, mounted)
                except TransferRefusal as exc:
                    assert exc.code in {"PATH_UNSAFE", "DESTINATION_LAYOUT_UNSUPPORTED"}
                else:
                    raise AssertionError("parent alias admitted: " + spelling)
    try:
        (exports / "longparentforalias").mkdir()
    except FileExistsError:
        pass
    else:
        raise AssertionError("case-equivalent directory creation succeeded")


def exercise(base, mount):
    repo = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(repo))
    sys.path.insert(0, str(repo / "tests"))
    from modelark.slice import domain as d, state, transaction as t
    from modelark.slice.fat32_destination import Fat32Destination
    from modelark.slice.fat32_observation import Fat32Tree, _mount_policy, check_directory
    from modelark.slice.fat32_plan import Fat32Intent, Fat32Plan, binding_for, estimate_metadata
    from modelark.slice.folder_contract import CapacityObservation, FolderProfile
    from modelark.slice.host_observation import parse_mounts, read_fs_text
    from modelark.slice.linux import rename_noreplace
    from test_slice_transaction import DATA, Sources, proposal
    from test_slice_fat32_transactions import FAULTS

    state.HOST_STATE_DIR = base / "private-host-state"
    store = state.Store()
    exports = mount / "exports"
    exports.mkdir()
    (exports / "sibling.txt").write_bytes(b"untouched synthetic sibling")
    checks = []
    with Fat32Tree(exports, writable=True) as tree:
        matches = [m for m in parse_mounts(read_fs_text("/proc/self/mountinfo"))
                   if m.mount_id == tree.mount_id]
        assert len(matches) == 1 and matches[0].fs_type == "vfat"
        _mount_policy(matches[0])
        check_directory(tree.fd)
        # The loop image deliberately is not a qualified physical USB source or
        # destination. Driver/port tests use explicit synthetic backing evidence.
        def recheck(retained):
            retained.check()
            check_directory(retained.fd)

        for name, data in (("rename-source.txt", b"source"), ("rename-target.txt", b"target")):
            fd = os.open(name, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600, dir_fd=tree.fd)
            try:
                os.write(fd, data)
                os.fsync(fd)
            finally:
                os.close(fd)
        try:
            rename_noreplace(tree.fd, "rename-source.txt", tree.fd, "rename-target.txt")
        except FileExistsError:
            pass
        else:
            raise AssertionError("RENAME_NOREPLACE overwrote existing target")
        assert (exports / "rename-target.txt").read_bytes() == b"target"
        rename_noreplace(tree.fd, "rename-source.txt", tree.fd, "rename-published.txt")
        os.fsync(tree.fd)
        assert (exports / "rename-published.txt").read_bytes() == b"source"
        checks.append("kernel-no-replace-and-directory-flush")
        parent_alias_checks(exports, matches[0])
        checks.append("real-vfat-exact-parent-case-and-numbered-short-alias-refusal")
        public_workflow(base, exports, matches[0])
        checks.append("public-preview-approve-start-real-vfat-synthetic-source-and-backing")

        def create(child):
            target = Fat32Intent(FolderProfile.FAT32_SESSION, "synthetic-loop-qualification",
                                 ("exports",), child)
            catalog, backing = str(base / "synthetic-catalog-never-opened"), ("synthetic-loop",)
            binding = binding_for(target, catalog, str(exports), backing)
            original, _, snapshot = proposal()
            preview = d.preview(replace(original.spec, destination_id=target.target_id,
                                        destination_root=child), snapshot)
            fs = os.fstatvfs(tree.fd)
            capacity = CapacityObservation(fs.f_bavail * fs.f_frsize, None)
            reserve = estimate_metadata(preview, binding, catalog, str(exports), backing,
                                        capacity, store.root)
            plan = Fat32Plan(preview, binding, catalog, str(exports), backing, capacity, reserve)
            approval = d.approve(preview, expected_seal=preview.seal, current_snapshot=snapshot)
            tx = store.create(plan, approval)
            store.approve(tx, expected_seal=plan.seal)
            return tx, plan, Sources(snapshot, t)

        def run(tx, plan, sources, fault=None):
            with Fat32Destination(tree, plan.destination, store, tx, recheck=recheck) as port:
                with t.start(store, tx, port, sources, fault=fault) as session:
                    return session.run()

        tx, plan, sources = create("complete")
        assert run(tx, plan, sources).state == "complete"
        assert (exports / "complete/org/model/model.safetensors").read_bytes() == DATA
        report = json.loads((exports / "complete/.modelark-slice-receipt.json").read_text())
        assert report["host_transaction_completion"] == "not-claimed"
        checks.append("real-vfat-port-completion-original-hashes-and-report")

        for index, point in enumerate(FAULTS):
            tx, plan, sources = create("fault-" + str(index))
            reached = False

            def crash(current):
                nonlocal reached
                if current == point:
                    reached = True
                    raise RuntimeError("synthetic interruption")

            try:
                run(tx, plan, sources, crash)
            except RuntimeError as exc:
                assert str(exc) == "synthetic interruption"
            else:
                raise AssertionError("fault boundary was not reached: " + point)
            assert reached and store.attempt_consumed(tx)
            assert store.status(tx).state != "complete"
            try:
                run(tx, plan, sources)
            except t.TransferRefusal as exc:
                assert exc.code == "FAT32_NEW_ROOT_REQUIRED"
            else:
                raise AssertionError("consumed FAT intent resumed")
            checks.append("interruption:" + point)
        assert (exports / "sibling.txt").read_bytes() == b"untouched synthetic sibling"
        checks.append("sibling-preserved")
    return checks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    if os.geteuid() == 0:
        raise SystemExit("Run as your ordinary user; the script requests sudo only for mount/unmount.")
    base = Path(tempfile.mkdtemp(prefix="modelark-fat32-qualification-"))
    image, mount = base / "disposable.img", base / "mount"
    mount.mkdir(mode=0o700)
    print("Disposable qualification directory:", base, flush=True)
    # -C creates a new regular image in a fresh private directory. No device/path
    # argument is accepted from the operator; no existing filesystem is formatted.
    subprocess.run(["/usr/sbin/mkfs.fat", "-F", "32", "-C", str(image), "65536"], check=True)
    options = (f"loop,uid={os.geteuid()},gid={os.getegid()},umask=0077,"
               "shortname=mixed,codepage=437,iocharset=iso8859-1,utf8=1")
    mounted = False
    try:
        subprocess.run(["sudo", "--", "mount", "-t", "vfat", "-o", options, str(image), str(mount)], check=True)
        mounted = True
        checks = exercise(base, mount)
    finally:
        # Never lazy/force unmount; a failed detach stays visible for the operator.
        if mounted or os.path.ismount(mount):
            subprocess.run(["sudo", "--", "umount", "--", str(mount)], check=True)
    result = {"status": "passed", "checks": checks, "retained_directory": str(base),
              "physical_USB_or_archive_test": False}
    print(json.dumps(result, indent=2), flush=True)


if __name__ == "__main__":
    main()
