"""Read-only hardware eligibility using only synthetic inventories and disposable trees."""
import copy
import importlib
import os

import pytest

from modelark.slice.linux import BoundTree
from modelark.slice.transaction import TransferRefusal


@pytest.fixture
def hardware(tmp_path, monkeypatch):
    module = importlib.import_module("modelark.slice.hardware")
    monkeypatch.setattr(module, '_backing_identity', lambda key: (42, key))
    root = tmp_path / "usb"
    root.mkdir()
    with BoundTree(root) as tree:
        major_minor = f"{os.major(os.fstat(tree.fd).st_dev)}:{os.minor(os.fstat(tree.fd).st_dev)}"
        mount_id = tree.mount_id
    leaf = {"name": "/dev/testusb1", "path": "/dev/testusb1", "type": "part", "pkname": "/dev/testusb",
            "maj:min": major_minor, "size": 10**15, "fstype": "ext4", "uuid": "USB-FS", "ro": False}
    usb = {"name": "/dev/testusb", "path": "/dev/testusb", "type": "disk", "maj:min": "240:0",
           "size": 10**15, "serial": "USB-SERIAL", "wwn": "USB-WWN", "tran": "usb", "ro": False,
           "children": [leaf]}
    host = {"name": "/dev/testhost", "path": "/dev/testhost", "type": "disk", "maj:min": "241:0",
            "size": 10**15, "serial": "HOST-SERIAL", "wwn": "HOST-WWN", "tran": "nvme", "ro": False,
            "children": [{"name": "/dev/testhost1", "path": "/dev/testhost1", "type": "part",
                          "pkname": "/dev/testhost", "maj:min": "241:1", "size": 10**15,
                          "fstype": "ext4", "uuid": "HOST-FS", "ro": False}]}
    inventory = {"blockdevices": [host, usb]}
    mounts = ["1 0 241:1 / / rw,relatime - ext4 /dev/testhost1 rw",
              f"{mount_id} 1 {major_minor} / {root} rw,relatime - ext4 /dev/testusb1 rw"]
    state = {"inventory": inventory, "mounts": mounts, "swaps": "Filename\tType\tSize\tUsed\tPriority\n"}
    observer = module.LinuxObserver(inventory=lambda: state["inventory"], mounts=lambda: "\n".join(state["mounts"]),
                                    swaps=lambda: state["swaps"])
    return observer, root, state, usb, leaf, module


def test_direct_usb_evidence_binds_whole_disk_and_stable_capability_profile(hardware):
    observer, root, _, _, _, _ = hardware
    value = observer.observe(root, writable=True)
    assert value.serial == "USB-SERIAL"
    assert value.fs_uuid == "USB-FS"
    assert value.mount_path == str(root)
    assert value.fs_type == "ext4"
    assert value.available_bytes > 0
    assert value.block_size > 0
    assert value.name_max > 0
    capacity = os.statvfs(root)
    assert value.total_bytes == capacity.f_blocks * capacity.f_frsize
    assert value.device_size == 10**15
    assert value.binding().device_id == value.device_id
    assert value.binding().filesystem_id == "USB-FS"
    assert value.binding().mount_id != str(value.mount_id)
    assert value.device_id != value.fs_uuid


@pytest.mark.parametrize("field,value", [("tran", "sata"), ("serial", None), ("ro", True)])
def test_destination_requires_usb_identity_and_writability(hardware, field, value):
    observer, root, _, disk, _, _ = hardware
    disk[field] = value
    if field == "serial":
        disk["wwn"] = None
    with pytest.raises(TransferRefusal):
        observer.observe(root, writable=True)


def test_read_only_source_need_not_be_usb_and_may_be_xfs(hardware):
    observer, root, state, disk, leaf, _ = hardware
    disk["tran"] = "sata"
    leaf["fstype"] = "xfs"
    state["mounts"][1] = state["mounts"][1].replace(" - ext4 ", " - xfs ")
    assert observer.observe(root).fs_type == "xfs"
    with pytest.raises(TransferRefusal):
        observer.observe(root, writable=True)


@pytest.mark.parametrize("kind", ["crypt", "lvm", "raid1", "loop", "rom"])
def test_stacked_or_unknown_target_devices_refuse(hardware, kind):
    observer, root, _, _, leaf, _ = hardware
    leaf["type"] = kind
    with pytest.raises(TransferRefusal):
        observer.observe(root, writable=True)


def test_unknown_or_mismatched_filesystem_refuses(hardware):
    observer, root, _, _, leaf, _ = hardware
    leaf["fstype"] = "vfat"
    with pytest.raises(TransferRefusal):
        observer.observe(root, writable=True)


@pytest.mark.parametrize("protected", ["/", "/boot", "/home", "/var"])
def test_system_filesystem_sibling_excludes_whole_disk(hardware, protected):
    observer, root, state, disk, _, _ = hardware
    disk["children"].append({"name": "/dev/testusb2", "path": "/dev/testusb2", "type": "part",
                             "pkname": "/dev/testusb", "maj:min": "240:2", "size": 10**14,
                             "fstype": "ext4", "uuid": "SIBLING-FS", "ro": False})
    if protected == "/":
        state["mounts"][0] = "1 0 240:2 / / rw - ext4 /dev/testusb2 rw"
    else:
        state["mounts"].append(f"912 1 240:2 / {protected} rw - ext4 /dev/testusb2 rw")
    with pytest.raises(TransferRefusal):
        observer.observe(root, writable=True)


def test_active_swap_sibling_excludes_whole_disk(hardware):
    observer, root, state, disk, _, _ = hardware
    disk["children"].append({"name": "/dev/testusb2", "path": "/dev/testusb2", "type": "part",
                             "pkname": "/dev/testusb", "maj:min": "240:2", "size": 10**14,
                             "fstype": "swap", "uuid": "SWAP-FS", "ro": False})
    state["swaps"] += "/dev/testusb2 partition 4096 0 -2\n"
    with pytest.raises(TransferRefusal):
        observer.observe(root, writable=True)


@pytest.mark.parametrize("archive", [{"fs_uuid": "USB-FS", "serial": None},
                                     {"fs_uuid": "OFFLINE-FS", "serial": "USB-SERIAL"},
                                     {"fs_uuid": "OFFLINE-FS", "serial": "USB-WWN"}])
def test_all_registered_archive_identity_exclusions(hardware, archive):
    observer, root, _, _, _, _ = hardware
    from types import SimpleNamespace
    with pytest.raises(TransferRefusal):
        observer.observe(root, writable=True, archives=(SimpleNamespace(**archive),))


def test_duplicate_filesystem_uuid_refuses(hardware):
    observer, root, state, _, leaf, _ = hardware
    state["inventory"]["blockdevices"][0]["children"][0]["uuid"] = leaf["uuid"]
    with pytest.raises(TransferRefusal):
        observer.observe(root, writable=True)


def test_duplicate_whole_disk_identity_refuses(hardware):
    observer, root, state, disk, _, _ = hardware
    state["inventory"]["blockdevices"][0]["wwn"] = disk["wwn"]
    with pytest.raises(TransferRefusal):
        observer.observe(root, writable=True)


@pytest.mark.parametrize("change", ["alias", "nested", "bind", "mount-id", "major-minor"])
def test_mount_binding_must_be_unique_exact_and_descriptor_matched(hardware, change):
    observer, root, state, _, leaf, _ = hardware
    if change == "alias":
        state["mounts"].append(f"991 1 {leaf['maj:min']} / /different rw - ext4 /dev/testusb1 rw")
    elif change == "nested":
        state["mounts"].append(f"991 1 242:1 / {root}/nested rw - ext4 /dev/foreign rw")
    elif change == "bind":
        parts = state["mounts"][1].split()
        parts[3] = "/subdirectory"
        state["mounts"][1] = " ".join(parts)
    else:
        parts = state["mounts"][1].split()
        parts[0 if change == "mount-id" else 2] = "999999" if change == "mount-id" else "245:9"
        state["mounts"][1] = " ".join(parts)
    with pytest.raises(TransferRefusal):
        observer.observe(root, writable=True)


def test_descendant_of_mount_root_is_not_a_dedicated_destination(hardware):
    observer, root, _, _, _, _ = hardware
    child = root / "child"
    child.mkdir()
    with pytest.raises(TransferRefusal):
        observer.observe(child, writable=True)


def test_archive_subdirectory_uses_covering_mount_for_read_only_evidence(hardware):
    observer, root, _, _, _, _ = hardware
    archive = root / "modelark"
    archive.mkdir()
    evidence = observer.observe(archive)
    assert evidence.mount_path == str(root)
    assert evidence.fs_uuid == "USB-FS"
    with pytest.raises(TransferRefusal):
        observer.observe(archive, writable=True)


def test_archive_subdirectory_still_rejects_nested_mounts_and_system_disk(hardware):
    observer, root, state, _, leaf, _ = hardware
    archive = root / "modelark"
    archive.mkdir()
    state["mounts"].append(f"991 1 242:1 / {root}/other rw - ext4 /dev/foreign rw")
    with pytest.raises(TransferRefusal):
        observer.observe(archive)
    state["mounts"].pop()
    state["mounts"][0] = state["mounts"][0].replace("241:1", leaf["maj:min"])
    with pytest.raises(TransferRefusal):
        observer.observe(archive)


def test_observations_do_not_mutate_supplied_inventory(hardware):
    observer, root, state, _, _, _ = hardware
    before = copy.deepcopy(state)
    observer.observe(root, writable=True)
    assert state == before


def test_remount_attachment_changes_do_not_change_stable_binding(hardware):
    observer, root, state, _, _, module = hardware
    before = observer.observe(root, writable=True)
    class ReattachedTree(BoundTree):
        @property
        def mount_id(self):
            return self._actual_mount_id + 123

        @mount_id.setter
        def mount_id(self, value):
            self._actual_mount_id = value

        def check(self):
            # Synthetic reattachment evidence: retain actual disposable identity; the
            # fixture changes mountinfo's attachment number consistently with this view.
            actual = self._actual_mount_id
            self._actual_mount_id -= 123
            try:
                super().check()
            finally:
                self._actual_mount_id = actual
    parts = state["mounts"][1].split()
    parts[0] = str(int(parts[0]) + 123)
    state["mounts"][1] = " ".join(parts)
    after = module.LinuxObserver(inventory=lambda: state["inventory"], mounts=lambda: "\n".join(state["mounts"]),
                                 swaps=lambda: state["swaps"], tree_factory=ReattachedTree).observe(root, writable=True)
    assert after.mount_id == before.mount_id + 123
    assert after.binding().mount_id == before.binding().mount_id
    assert after.device_id == before.device_id


def test_archive_uuid_on_unmounted_sibling_excludes_whole_disk(hardware):
    from types import SimpleNamespace
    observer, root, _, disk, _, _ = hardware
    disk["children"].append({"name": "/dev/testusb2", "path": "/dev/testusb2", "type": "part",
                             "pkname": "/dev/testusb", "maj:min": "240:2", "size": 10**14,
                             "fstype": "ext4", "uuid": "ARCHIVE-FS", "ro": False})
    with pytest.raises(TransferRefusal):
        observer.observe(root, writable=True, archives=(SimpleNamespace(fs_uuid="archive-fs", serial=None),))


def test_swapfile_backing_disk_is_excluded(hardware):
    observer, root, state, _, _, _ = hardware
    state["swaps"] += f"{root}/swapfile file 4096 0 -2\n"
    with pytest.raises(TransferRefusal):
        observer.observe(root, writable=True)


def test_nested_system_filesystem_on_sibling_is_excluded(hardware):
    observer, root, state, disk, _, _ = hardware
    disk["children"].append({"name": "/dev/testusb2", "path": "/dev/testusb2", "type": "part",
                             "pkname": "/dev/testusb", "maj:min": "240:2", "size": 10**14,
                             "fstype": "ext4", "uuid": "VAR-FS", "ro": False})
    state["mounts"].append("912 1 240:2 / /var/lib rw - ext4 /dev/testusb2 rw")
    with pytest.raises(TransferRefusal):
        observer.observe(root, writable=True)


def test_unknown_protected_backing_disk_refuses(hardware):
    observer, root, state, _, _, _ = hardware
    state["mounts"][0] = "1 0 245:1 / / rw - overlay overlay rw"
    with pytest.raises(TransferRefusal):
        observer.observe(root, writable=True)


@pytest.mark.parametrize("inventory", [{}, {"blockdevices": []}, {"blockdevices": [None]}])
def test_missing_or_malformed_inventory_refuses(hardware, inventory):
    observer, root, state, _, _, _ = hardware
    state["inventory"] = inventory
    with pytest.raises(TransferRefusal):
        observer.observe(root, writable=True)


def test_inventory_change_during_observation_refuses(hardware):
    _, root, state, _, _, module = hardware
    count = 0
    def changes():
        nonlocal count
        count += 1
        result = copy.deepcopy(state["inventory"])
        if count > 1:
            result["blockdevices"][1]["serial"] = "REPLACEMENT"
        return result
    observer = module.LinuxObserver(inventory=changes, mounts=lambda: "\n".join(state["mounts"]),
                                    swaps=lambda: state["swaps"])
    with pytest.raises(TransferRefusal):
        observer.observe(root, writable=True)


def test_construction_never_observes_live_hardware(monkeypatch, hardware):
    *_, module = hardware
    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: pytest.fail("implicit hardware discovery"))
    module.LinuxObserver()


def test_lsblk_uses_explicit_read_only_json_columns(monkeypatch, hardware):
    from types import SimpleNamespace
    *_, module = hardware
    def run(command, **kwargs):
        assert command == ["lsblk", "--json", "--bytes", "--paths", "--output",
                           "NAME,PATH,TYPE,PKNAME,MAJ:MIN,SIZE,FSTYPE,UUID,SERIAL,WWN,TRAN,RO"]
        assert kwargs == {"check": True, "capture_output": True, "text": True}
        return SimpleNamespace(stdout='{"blockdevices": []}')
    monkeypatch.setattr(module.subprocess, "run", run)
    assert module._inventory() == {"blockdevices": []}


def test_unrelated_mounted_loop_does_not_block_usb(hardware):
    observer, root, state, _, _, _ = hardware
    state['inventory']['blockdevices'].append({'name': '/dev/loop0', 'path': '/dev/loop0',
                                              'type': 'loop', 'maj:min': '7:0'})
    state['mounts'].append('901 1 7:0 / /snap/example ro - squashfs /dev/loop0 ro')
    assert observer.observe(root, writable=True).fs_uuid == 'USB-FS'


def test_still_mounted_device_missing_from_inventory_is_attended_loss(hardware):
    observer, root, state, _, _, _ = hardware
    state['inventory']['blockdevices'].pop()
    with pytest.raises(TransferRefusal, match='WAITING_DESTINATION'):
        observer.observe(root, writable=True)


@pytest.mark.parametrize('side', ['mount', 'super'])
def test_disabled_user_xattrs_refuse_readonly_admission(hardware, side):
    observer, root, state, _, _, _ = hardware
    line = state['mounts'][1]
    left, right = line.split(' - ')
    if side == 'mount':
        left = left.replace('rw,relatime', 'rw,relatime,nouser_xattr')
    else:
        right += ',nouser_xattr'
    state['mounts'][1] = left + ' - ' + right
    with pytest.raises(TransferRefusal, match='DESTINATION_NOT_WRITABLE'):
        observer.observe(root, writable=True)
    assert not list(root.iterdir())


def test_operator_without_root_creation_permission_refuses_before_writes(hardware):
    observer, root, *_ = hardware
    root.chmod(0o555)
    try:
        with pytest.raises(TransferRefusal, match='DESTINATION_NOT_WRITABLE'):
            observer.observe(root, writable=True)
        assert not list(root.iterdir())
    finally:
        root.chmod(0o755)


def test_chunk_checks_do_not_repeat_full_inventory(hardware):
    observer, root, state, _, _, _ = hardware
    calls = []
    observer._inventory = lambda: calls.append('inventory') or state['inventory']
    evidence = observer.observe(root, writable=True)
    with BoundTree(root, writable=True) as tree:
        for _ in range(20):
            observer.check_attachment(tree, evidence)
    assert calls == ['inventory', 'inventory']


def test_backing_loss_is_sticky_even_while_mount_id_survives(hardware):
    observer, root, *_ = hardware
    evidence = observer.observe(root, writable=True)
    original = observer._backing
    def gone(key):
        raise FileNotFoundError(key)
    with BoundTree(root, writable=True) as tree:
        observer._backing = gone
        with pytest.raises(TransferRefusal, match='WAITING_DESTINATION'):
            observer.check_attachment(tree, evidence)
        observer._backing = original
        with pytest.raises(TransferRefusal, match='WAITING_DESTINATION'):
            observer.check_attachment(tree, evidence)
    # Only a new retained attachment can continue after an explicit fresh observation.
    fresh = observer.observe(root, writable=True)
    with BoundTree(root, writable=True) as tree:
        observer.check_attachment(tree, fresh)


@pytest.mark.parametrize('change', ['sibling', 'swap', 'archive', 'readonly', 'backing'])
def test_hot_checks_revalidate_changed_topology_or_roles(hardware, change):
    from types import SimpleNamespace
    observer, root, state, disk, _, _ = hardware
    evidence = observer.observe(root, writable=True)
    archives = ()
    if change in {'sibling', 'swap'}:
        disk['children'].append({'name': '/dev/testusb2', 'type': 'part', 'pkname': '/dev/testusb',
                                 'maj:min': '240:2', 'uuid': 'other-fs'})
        if change == 'sibling':
            state['mounts'].append('901 1 240:2 / /other rw - ext4 /dev/testusb2 rw')
        else:
            state['swaps'] += '/dev/testusb2 partition 4096 0 -2\n'
    elif change == 'archive':
        archives = (SimpleNamespace(fs_uuid='USB-FS', serial=None),)
    elif change == 'readonly':
        state['mounts'][1] = state['mounts'][1].replace('rw', 'ro')
    else:
        observer._backing = lambda key: (99, key)
    with BoundTree(root, writable=True) as tree, pytest.raises(TransferRefusal):
        observer.check_attachment(tree, evidence, archives=archives)


def test_protected_loop_backing_remains_unproven(hardware):
    observer, root, state, *_ = hardware
    state['inventory']['blockdevices'].append({'name': '/dev/loop0', 'type': 'loop', 'maj:min': '7:0'})
    state['mounts'].append('901 1 7:0 / /var/lib/foreign ro - squashfs /dev/loop0 ro')
    with pytest.raises(TransferRefusal, match='DESTINATION_UNPROVEN'):
        observer.observe(root, writable=True)


def test_user_xattr_read_probe_refuses_unsupported_namespace(hardware, monkeypatch):
    import errno
    observer, root, _, _, _, module = hardware
    def unsupported(*args):
        raise OSError(errno.EOPNOTSUPP, 'user namespace disabled')
    monkeypatch.setattr(module.os, 'getxattr', unsupported)
    with pytest.raises(TransferRefusal, match='DESTINATION_NOT_WRITABLE'):
        observer.observe(root, writable=True)


def test_missing_effective_access_syscall_reports_unsupported_platform(hardware, monkeypatch):
    import ctypes
    import errno
    from modelark.slice import linux
    observer, root, *_ = hardware
    libc = linux._libc()
    class OlderKernel:
        def __getattr__(self, name):
            return getattr(libc, name)

        def syscall(self, number, *args):
            if number.value == 439:
                ctypes.set_errno(errno.ENOSYS)
                return -1
            return libc.syscall(number, *args)
    monkeypatch.setattr(linux, '_libc', OlderKernel)
    with pytest.raises(TransferRefusal, match='FILESYSTEM_UNSUPPORTED.*faccessat2'):
        observer.observe(root, writable=True)
    assert not list(root.iterdir())


@pytest.mark.parametrize('capability', ['openat2', 'statx'])
def test_other_missing_confinement_syscalls_report_unsupported_platform(monkeypatch, capability):
    import ctypes
    import errno
    from types import SimpleNamespace
    from modelark.slice import linux
    def unavailable(*args):
        ctypes.set_errno(errno.ENOSYS)
        return -1
    monkeypatch.setattr(linux, '_libc', lambda: SimpleNamespace(syscall=unavailable, statx=unavailable))
    with pytest.raises(TransferRefusal, match='FILESYSTEM_UNSUPPORTED.*' + capability):
        if capability == 'openat2':
            linux._openat2(-1, '.', os.O_RDONLY)
        else:
            linux._statx(-1)


def _default_acl(named_users):
    import struct
    entries = [(1, 7, 0xffffffff)]
    entries += [(2, 7, 100000 + i) for i in range(named_users)]
    entries += [(4, 5, 0xffffffff), (16, 7, 0xffffffff), (32, 5, 0xffffffff)]
    return struct.pack('<I', 2) + b''.join(struct.pack('<HHI', *entry) for entry in entries)


@pytest.mark.parametrize('named_users', [1, 400])
def test_inherited_default_acl_refuses_before_admission(hardware, named_users):
    observer, root, *_ = hardware
    acl = _default_acl(named_users)
    os.setxattr(root, 'system.posix_acl_default', acl)
    with pytest.raises(TransferRefusal, match='DESTINATION_CAPACITY_UNPROVEN.*default ACL'):
        observer.observe(root, writable=True)
    assert os.getxattr(root, 'system.posix_acl_default') == acl
    assert not list(root.iterdir())
    # Source-only observation must not apply destination inheritance policy.
    assert observer.observe(root).fs_uuid == 'USB-FS'


def test_default_acl_added_after_observation_refuses_hot_check(hardware):
    observer, root, *_ = hardware
    evidence = observer.observe(root, writable=True)
    os.setxattr(root, 'system.posix_acl_default', _default_acl(1))
    with BoundTree(root, writable=True) as tree:
        with pytest.raises(TransferRefusal, match='DESTINATION_CAPACITY_UNPROVEN.*default ACL'):
            observer.check_attachment(tree, evidence)


def test_noninherited_access_acl_remains_supported(hardware):
    observer, root, *_ = hardware
    os.setxattr(root, 'system.posix_acl_access', _default_acl(1))
    assert observer.observe(root, writable=True).fs_uuid == 'USB-FS'


@pytest.mark.parametrize('number', [5, 13, 95])
def test_unknown_default_acl_probe_failure_is_not_absence(hardware, monkeypatch, number):
    observer, root, _, _, _, module = hardware
    original = module.os.getxattr
    def uncertain(fd, name):
        if name == 'system.posix_acl_default':
            raise OSError(number, 'default ACL cannot be inspected')
        return original(fd, name)
    monkeypatch.setattr(module.os, 'getxattr', uncertain)
    code = 'DESTINATION_UNPROVEN' if number == 5 else 'DESTINATION_CAPACITY_UNPROVEN'
    with pytest.raises(TransferRefusal, match=code + '.*default ACL'):
        observer.observe(root, writable=True)
    assert not list(root.iterdir())


def test_unplug_between_backing_probe_and_descriptor_check_remains_waiting(hardware, monkeypatch):
    import errno
    observer, root, *_ = hardware
    evidence = observer.observe(root, writable=True)
    def absent(key):
        raise FileNotFoundError(key)
    with BoundTree(root, writable=True) as tree:
        def unplug_during_check():
            observer._backing = absent
            raise OSError(errno.EIO, 'device unplugged during descriptor check')
        monkeypatch.setattr(tree, 'check', unplug_during_check)
        with pytest.raises(TransferRefusal, match='WAITING_DESTINATION'):
            observer.check_attachment(tree, evidence)
        assert tree._attachment_lost


def test_unplug_during_full_refresh_io_remains_waiting(hardware):
    import errno
    observer, root, *_ = hardware
    observer.observe(root, writable=True)
    def absent(key):
        raise FileNotFoundError(key)
    def unplug_during_bind(*args, **kwargs):
        observer._backing = absent
        raise OSError(errno.EIO, 'device unplugged during full observation')
    observer._tree_factory = unplug_during_bind
    with pytest.raises(TransferRefusal, match='WAITING_DESTINATION'):
        observer.observe(root, writable=True)


def test_unplug_during_full_refresh_xattr_proof_remains_waiting(hardware, monkeypatch):
    import errno
    observer, root, _, _, _, module = hardware
    observer.observe(root, writable=True)
    def absent(key):
        raise FileNotFoundError(key)
    def unplug_during_xattr(*args):
        observer._backing = absent
        raise OSError(errno.EIO, 'device unplugged during namespace probe')
    monkeypatch.setattr(module.os, 'getxattr', unplug_during_xattr)
    with pytest.raises(TransferRefusal, match='WAITING_DESTINATION'):
        observer.observe(root, writable=True)
