"""Pinned, bounded native Git observation under a real publication scope.

No shell, inherited Git routing, hook, filter probe, network fetch, annex init or
version upgrade is exposed by the read interface. Mutation entry points are
private coordinator operations and require a separately durable command intent.
"""
from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import stat
import subprocess
from types import MappingProxyType

from modelark import publication_locks, publication_store as store
from modelark.publication_policy import PublicationRefused, relative_path
from modelark.slice.linux import BoundTree


TOOLS = MappingProxyType({
    "/usr/bin/git": "5a39a7909c023f92a84b77b49e6b008f3f152b833135b96d73ac7c403314a88a",
    "/usr/bin/git-annex": "5de67e4fd40d011af9f99f563df80238b1d74c001c00cc1ed64227eb2e79ccc7",
    "/usr/bin/dash": "4f291296e89b784cd35479fca606f228126e3641f5bcaee68dee36583d7c9483",
})
FILTERS = {"filter.annex.clean": "git-annex smudge --clean -- %f",
           "filter.annex.smudge": "git-annex smudge -- %f"}
_OVERRIDES = ("core.hooksPath=/dev/null", "core.attributesFile=/dev/null",
              "core.excludesFile=/dev/null", "core.fsmonitor=false", "core.untrackedCache=false",
              "commit.gpgsign=false", "tag.gpgsign=false", "protocol.allow=never",
              "protocol.file.allow=never", "gc.auto=0", "maintenance.auto=false",
              "user.name=ModelArk", "user.email=modelark@localhost.invalid")
# Necessary command isolation, not process-wide/operator environment changes.
_ENV = {"PATH": "/usr/bin:/bin", "LC_ALL": "C", "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_ATTR_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0", "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_OPTIONAL_LOCKS": "0", "GIT_NO_LAZY_FETCH": "1"}
_SAFE_CORE = {"core.repositoryformatversion": {"0"}, "core.bare": {"false"},
              "core.filemode": {"true", "false"}, "core.logallrefupdates": {"true"},
              "core.symlinks": {"true"}, "core.ignorecase": {"true", "false"}}
_SAFE_ANNEX = {"annex.version": {"8"}, "annex.backend": {"SHA256", "SHA256E"},
               "annex.largefiles": {"anything"}, "annex.crippledfilesystem": {"false"},
               "annex.thin": {"false"}, "annex.securehashesonly": {"true"}}
_OID = re.compile(r"[0-9a-f]{40}\Z")
_REF = re.compile(r"refs/[A-Za-z0-9_./-]+\Z")


def _ref_or_oid(value):
    return bool(_OID.fullmatch(value) or _REF.fullmatch(value) and ".." not in value
                and not value.endswith(("/", ".", ".lock")) and "//" not in value)


def _identity(value):
    return (value.st_dev, value.st_ino, value.st_mode, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def _bounded_process(argv, *, cwd, pass_fds, environment, limit, input_data=None):
    """Drain both pipes with hard byte budgets; no unbounded communicate capture.

    On overflow/error terminate and reap only this owned subprocess. No command
    timeout or caller-selected executable is used. Stdin is bounded intent data.
    """
    if type(limit) is not int or not 0 < limit <= 64 * 1024 * 1024:
        raise PublicationRefused("PUBLICATION_COMMAND_LIMIT_INVALID")
    if input_data is not None and (type(input_data) is not bytes or len(input_data) > 1024 * 1024):
        raise PublicationRefused("PUBLICATION_COMMAND_INPUT_INVALID")
    buffers = {"out": bytearray(), "err": bytearray()}
    with subprocess.Popen(argv, cwd=cwd, env=dict(environment), pass_fds=tuple(pass_fds),
                          stdin=subprocess.PIPE if input_data is not None else subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE) as child:
        try:
            with selectors.DefaultSelector() as selector:
                for pipe, name in ((child.stdout, "out"), (child.stderr, "err")):
                    os.set_blocking(pipe.fileno(), False)
                    selector.register(pipe, selectors.EVENT_READ, name)
                pending = memoryview(input_data or b"")
                if child.stdin is not None:
                    if pending:
                        os.set_blocking(child.stdin.fileno(), False)
                        selector.register(child.stdin, selectors.EVENT_WRITE, "in")
                    else:
                        child.stdin.close()
                while selector.get_map():
                    for event, _ in selector.select():
                        pipe, name = event.fileobj, event.data
                        if name == "in":
                            try:
                                pending = pending[os.write(pipe.fileno(), pending[:65536]):]
                            except BrokenPipeError:
                                pending = pending[:0]
                            if not pending:
                                selector.unregister(pipe)
                                pipe.close()
                            continue
                        chunk = os.read(pipe.fileno(), 65536)
                        if not chunk:
                            selector.unregister(pipe)
                            continue
                        budget = limit if name == "out" else 65536
                        if len(buffers[name]) + len(chunk) > budget:
                            raise PublicationRefused("PUBLICATION_COMMAND_OUTPUT_LIMIT", stream=name)
                        buffers[name].extend(chunk)
            code = child.wait()
        except BaseException:
            if child.poll() is None:
                child.kill()
            child.wait()
            raise
    if code:
        raise PublicationRefused("PUBLICATION_NATIVE_COMMAND_FAILED", returncode=code,
                                 command=argv[0], stderr=bytes(buffers["err"]).decode(errors="replace")[:1024])
    return bytes(buffers["out"])


@dataclass(frozen=True)
class QualifiedAnnexProfile:
    root_identity: tuple
    mount_id: int
    annex_uuid: str
    config_sha256: str
    attributes_sha256: str
    effective_config: tuple
    tools: tuple

    def record(self):
        from dataclasses import asdict
        return json.loads(store.canonical({"version": 1, "repository_format": 8,
                                          "environment": _ENV, "overrides": _OVERRIDES, **asdict(self)}))

    @property
    def digest(self):
        return store.digest(self.record())


class QualifiedRepository:
    """Actual native reader bound to a held scope, UUID, root and sealed profile.

    Catalog-to-attachment admission belongs to the owning coordinator; this
    factory additionally compares the selected UUID and retains the opened root.
    A copied UUID/profile object is not accepted in place of the active scope.
    """

    def __init__(self, scope, root, *, drive_label=None):
        if type(scope) is not publication_locks._FenceScope:
            raise PublicationRefused("PUBLICATION_FENCE_AUTHORITY_MISSING")
        scope.require_io()
        self.scope = scope
        if drive_label is None:
            self.expected_uuid = scope.library[1]
        elif drive_label in scope.identities:
            self.expected_uuid = scope.identities[drive_label].annex_uuid
        else:
            raise PublicationRefused("PUBLICATION_PARTICIPANT_UNSELECTED")
        self.drive_label = drive_label
        self._stack = ExitStack()
        self._closed = False
        self._tools = {}
        try:
            self.tree = self._stack.enter_context(BoundTree(root))
            self._gitdir = self.tree.open(".git", os.O_RDONLY | os.O_DIRECTORY)
            self._stack.callback(os.close, self._gitdir)
            self._git_identity = _identity(os.fstat(self._gitdir))[:3]
            for path, expected in TOOLS.items():
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
                self._stack.callback(os.close, fd)
                before = _identity(os.fstat(fd))
                digest = hashlib.sha256()
                while data := os.read(fd, 1024 * 1024):
                    digest.update(data)
                if (not stat.S_ISREG(before[2]) or digest.hexdigest() != expected
                        or _identity(os.fstat(fd)) != before):
                    raise PublicationRefused("PUBLICATION_TOOL_UNQUALIFIED", path=path)
                self._tools[path] = (fd, before)
            if Path("/bin/sh").resolve() != Path("/usr/bin/dash"):
                raise PublicationRefused("PUBLICATION_HELPER_UNQUALIFIED")
            self.profile = self._inspect()
        except BaseException:
            self._stack.close()
            self._closed = True
            raise

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self._closed = True
        self._stack.close()

    def _require(self):
        if self._closed:
            raise PublicationRefused("PUBLICATION_NATIVE_SCOPE_CLOSED")
        self.scope.require_io()
        self.tree.check()
        current = self.tree.open(".git", os.O_RDONLY | os.O_DIRECTORY)
        try:
            if _identity(os.fstat(current))[:3] != self._git_identity:
                raise PublicationRefused("PUBLICATION_GIT_DIRECTORY_CHANGED")
        finally:
            os.close(current)
        for path, (fd, before) in self._tools.items():
            if _identity(os.fstat(fd)) != before or _identity(os.stat(path, follow_symlinks=False)) != before:
                raise PublicationRefused("PUBLICATION_TOOL_CHANGED", path=path)

    def _file(self, path, *, absent=False, limit=1024 * 1024):
        try:
            fd = self.tree.open(path, os.O_RDONLY | os.O_NONBLOCK)
        except FileNotFoundError:
            if absent:
                return b""
            raise PublicationRefused("PUBLICATION_PROFILE_MISSING", path=path) from None
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
                raise PublicationRefused("PUBLICATION_PROFILE_FILE_UNQUALIFIED", path=path)
            result = bytearray()
            while chunk := os.read(fd, min(65536, limit + 1 - len(result))):
                result.extend(chunk)
                if len(result) > limit:
                    raise PublicationRefused("PUBLICATION_PROFILE_FILE_UNQUALIFIED", path=path)
            if _identity(os.fstat(fd)) != _identity(before):
                raise PublicationRefused("PUBLICATION_PROFILE_CHANGED")
            return bytes(result)
        finally:
            os.close(fd)

    def _exec(self, args, *, limit=64 * 1024 * 1024, input_data=None, extra_fds=()):
        self._require()
        argv = ["/usr/bin/git", "--no-pager", "--no-replace-objects", "--literal-pathspecs"]
        for setting in _OVERRIDES:
            argv.extend(("-c", setting))
        argv.extend(args)
        out = _bounded_process(argv, cwd=f"/proc/self/fd/{self.tree.fd}",
                               pass_fds=(*self.scope.child_fence_fds, self.tree.fd, *extra_fds),
                               environment=_ENV, limit=limit, input_data=input_data)
        self._require()
        return out

    def _inspect(self):
        self._require()
        # Linked worktrees, alternate object stores and shallow/graft routing are
        # not qualified. Reject before invoking a repository-aware native reader.
        for path in (".git/commondir", ".git/shallow", ".git/info/grafts",
                     ".git/objects/info/alternates", ".git/objects/info/http-alternates"):
            with self.tree.parent(path) as (parent, name):
                try:
                    os.stat(name, dir_fd=parent, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                raise PublicationRefused("PUBLICATION_GIT_ROUTING_UNQUALIFIED", path=path)
        config = self._file(".git/config")
        fd = self.tree.open(".git/config", os.O_RDONLY)
        try:
            raw = self._exec(("config", "--file", f"/proc/self/fd/{fd}", "--no-includes", "--null", "--list"),
                             limit=1024 * 1024, extra_fds=(fd,))
        finally:
            os.close(fd)
        if self._file(".git/config") != config or raw and not raw.endswith(b"\0"):
            raise PublicationRefused("PUBLICATION_PROFILE_CHANGED")
        entries = {}
        try:
            for cell in raw.split(b"\0")[:-1]:
                key, value = cell.decode("utf-8").split("\n", 1)
                if key in entries:
                    raise PublicationRefused("PUBLICATION_CONFIG_AMBIGUOUS", key=key)
                entries[key] = value
        except (ValueError, UnicodeDecodeError) as exc:
            if isinstance(exc, PublicationRefused):
                raise
            raise PublicationRefused("PUBLICATION_CONFIG_UNQUALIFIED") from exc
        for key, value in entries.items():
            admitted = (key in FILTERS and value == FILTERS[key]
                        or key in _SAFE_CORE and value in _SAFE_CORE[key]
                        or key in _SAFE_ANNEX and value in _SAFE_ANNEX[key]
                        or key == "annex.uuid" and value == self.expected_uuid
                        or key in {"user.name", "user.email"} and bool(value) and "\n" not in value)
            if key.startswith("remote.") and re.fullmatch(r"remote\.[A-Za-z0-9_-]+\.(url|fetch|annex-uuid)", key):
                if key.endswith(".url"):
                    admitted = value.startswith("/") and "\n" not in value and "\0" not in value
                elif key.endswith(".fetch"):
                    admitted = bool(re.fullmatch(r"\+refs/heads/\*:refs/remotes/[A-Za-z0-9_-]+/\*", value))
                else:
                    store.canonical_uuid(value)
                    admitted = True
            if re.fullmatch(r"branch\.[A-Za-z0-9_/-]+\.(remote|merge)", key):
                admitted = bool(re.fullmatch(r"[A-Za-z0-9_-]+", value)) if key.endswith(".remote") else _ref_or_oid(value)
            if not admitted:
                raise PublicationRefused("PUBLICATION_CONFIG_UNQUALIFIED", key=key)
        required = {"core.repositoryformatversion": "0", "core.bare": "false",
                    "annex.version": "8", "annex.uuid": self.expected_uuid, **FILTERS}
        if any(entries.get(key) != value for key, value in required.items()):
            raise PublicationRefused("PUBLICATION_PROFILE_UNQUALIFIED")
        attributes = self._file(".git/info/attributes", absent=True)
        return QualifiedAnnexProfile(tuple(self.tree.identity(self.tree.fd)), self.tree.mount_id,
                                     self.expected_uuid, hashlib.sha256(config).hexdigest(),
                                     hashlib.sha256(attributes).hexdigest(),
                                     tuple((key, value, "local", "file:.git/config")
                                           for key, value in sorted(entries.items())), tuple(TOOLS.items()))

    def ensure(self):
        if self._inspect() != self.profile:
            raise PublicationRefused("PUBLICATION_PROFILE_CHANGED")

    def read(self, *args):
        """Closed raw-observation command vocabulary, never arbitrary Git options."""
        if args[:2] == ("--no-replace-objects", "--literal-pathspecs"):
            args = args[2:]
        admitted = args == ("symbolic-ref", "HEAD")
        if len(args) == 3 and args[:2] == ("rev-parse", "--verify"):
            admitted = _ref_or_oid(args[2])
        if len(args) == 3 and args[0] == "cat-file" and args[1] in {"-s", "blob", "tree", "commit"}:
            admitted = bool(_OID.fullmatch(args[2]))
        if len(args) == 5 and args[:4] == ("ls-tree", "-r", "--full-tree", "-z"):
            admitted = bool(_OID.fullmatch(args[4]))
        if args == ("ls-files", "--full-name", "--stage", "-v", "-z"):
            admitted = True
        if len(args) == 3 and args[:2] == ("for-each-ref", "--format=%(refname) %(objectname)"):
            admitted = bool(_REF.fullmatch(args[2]) and _ref_or_oid(args[2]))
        if not admitted:
            raise PublicationRefused("PUBLICATION_COMMAND_UNQUALIFIED")
        self.ensure()
        result = self._exec(args)
        self.ensure()
        return result

    def ref_oid(self, ref):
        if not isinstance(ref, str) or not _REF.fullmatch(ref) or not _ref_or_oid(ref):
            raise PublicationRefused("PUBLICATION_GIT_REF_UNQUALIFIED")
        raw = self.read("for-each-ref", "--format=%(refname) %(objectname)", ref)
        found = []
        for line in raw.splitlines():
            name, separator, oid = line.partition(b" ")
            if not separator or not _OID.fullmatch(oid.decode("ascii")):
                raise PublicationRefused("PUBLICATION_GIT_REF_UNQUALIFIED")
            if name == ref.encode("ascii"):
                found.append(oid.decode("ascii"))
        if len(found) > 1:
            raise PublicationRefused("PUBLICATION_GIT_REF_UNQUALIFIED")
        return found[0] if found else None

    def object_path(self, key):
        from modelark.publication_policy import parse_sha256_key, check_committed_pointer
        parse_sha256_key(key)
        self.ensure()
        raw = self._exec(("annex", "examinekey", "--format=${objectpath}", key), limit=4096)
        self.ensure()
        try:
            path = raw.decode("ascii").removesuffix("\n")
        except UnicodeDecodeError as exc:
            raise PublicationRefused("PUBLICATION_OBJECT_PATH_INVALID") from exc
        # The shared exact parser also checks the qualified path's full grammar.
        check_committed_pointer(mode="100644", blob=f"/annex/objects/{key}\n".encode(),
                                stored_path="probe", key=key, qualified_object_path=path)
        return path

    def metadata_path(self, key):
        """Native bucket observation only on an already qualified repository."""
        from modelark.publication_policy import parse_sha256_key
        parse_sha256_key(key)
        self.ensure()
        raw = self._exec(("annex", "examinekey", "--format=${hashdirlower}", key), limit=4096)
        self.ensure()
        if not re.fullmatch(rb"[0-9a-f]{3}/[0-9a-f]{3}/", raw):
            raise PublicationRefused("PUBLICATION_METADATA_PATH_UNQUALIFIED")
        return raw.decode("ascii") + key + ".log"

    def effective_locations(self, key, expected_annex_oid):
        """Confirm the native reader's effective UUID set for a sealed ref.

        This is not physical presence evidence. A coordinator compares it with
        admitted logs and independently proven selected copies after publication.
        """
        from modelark.publication_policy import parse_sha256_key
        parse_sha256_key(key)
        if not _OID.fullmatch(expected_annex_oid):
            raise PublicationRefused("PUBLICATION_GIT_OID_UNQUALIFIED")
        expected = expected_annex_oid.encode() + b"\n"
        if self.read("rev-parse", "--verify", "refs/heads/git-annex") != expected:
            raise PublicationRefused("PUBLICATION_METADATA_REF_CHANGED")
        self.ensure()
        raw = self._exec(("annex", "whereis", "--json", "--key=" + key), limit=1024 * 1024)
        self.ensure()
        if self.read("rev-parse", "--verify", "refs/heads/git-annex") != expected:
            raise PublicationRefused("PUBLICATION_METADATA_REF_CHANGED")
        try:
            value = json.loads(raw)
            if (not isinstance(value, dict) or value.get("command") != "whereis"
                    or value.get("key") != key or value.get("success") is not True
                    or type(value.get("whereis")) is not list or value.get("untrusted", [])
                    or value.get("error-messages", [])):
                raise ValueError()
            identities = []
            for entry in value["whereis"]:
                if not isinstance(entry, dict):
                    raise ValueError()
                identities.append(store.canonical_uuid(entry.get("uuid")))
            if len(identities) != len(set(identities)):
                raise ValueError()
        except (TypeError, ValueError) as exc:
            raise PublicationRefused("PUBLICATION_NATIVE_LOCATIONS_UNQUALIFIED") from exc
        return frozenset(identities)

    def attributes(self, path):
        relative_path(path)
        self.ensure()
        names = ("filter", "text", "ident", "working-tree-encoding", "eol", "annex.largefiles", "annex.backend")
        raw = self._exec(("check-attr", "-z", *names, "--", path), limit=16384)
        self.ensure()
        cells = raw.split(b"\0")
        if cells[-1] or len(cells) != 3 * len(names) + 1:
            raise PublicationRefused("PUBLICATION_ATTRIBUTES_UNQUALIFIED")
        expected = dict(zip(names, ("annex", "unset", "unset", "unspecified", "unspecified", "anything", "SHA256")))
        for offset, name in enumerate(names):
            if cells[3 * offset:3 * offset + 3] != [path.encode(), name.encode(), expected[name].encode()]:
                raise PublicationRefused("PUBLICATION_ATTRIBUTES_UNQUALIFIED", path=path)
        return expected

    def _run_action(self, action_id, *args, input_data=b""):
        """Private journal-bound command adapter; completion is verified elsewhere.

        A successful process is deliberately not an action receipt. Only the
        coordinator's independent state observer may mark the action VERIFIED.
        The public read interface cannot reach this command vocabulary.
        """
        from modelark import publication_actions as actions
        record = actions.read(self.scope, action_id)
        value = record["intent"]
        profile = self.profile.record()
        expected_root = {name: profile[name] for name in ("root_identity", "mount_id", "annex_uuid")}
        if (record["status"] != "PREPARED" or value.get("profile_digest") != self.profile.digest
                or value.get("root") != expected_root
                or value.get("command") != {"argv": list(args), "stdin_digest": hashlib.sha256(input_data).hexdigest()}):
            raise PublicationRefused("PUBLICATION_COMMAND_INTENT_MISMATCH")
        kind = record["kind"]
        admitted = False
        commands = [args]
        if kind == "annex_metadata" and len(args) == 3 and args[0] == "modelark-tag-and-flush":
            from modelark.publication_policy import parse_sha256_key
            parse_sha256_key(args[1])
            try:
                assignments = json.loads(args[2])
            except (ValueError, TypeError) as exc:
                raise PublicationRefused("PUBLICATION_COMMAND_UNQUALIFIED") from exc
            if (type(assignments) is not dict or not assignments
                    or not assignments.keys() <= {"model", "format", "quant", "params"}
                    or any(type(value) is not str or not value or "\0" in value or "\n" in value
                           for value in assignments.values()) or store.canonical(assignments) != args[2]):
                raise PublicationRefused("PUBLICATION_COMMAND_UNQUALIFIED")
            fields = tuple(part for key, value in sorted(assignments.items()) for part in ("-s", key + "=" + value))
            commands = [("annex", "metadata", "--key=" + args[1], *fields),
                        ("annex", "sync", "--only-annex", "--no-content", "--no-pull", "--no-push", "--no-commit")]
            admitted = not input_data
        if kind == "annex_add" and len(args) == 7 and args[:6] == (
                "annex", "add", "--force", "--force-large", "--backend=SHA256", "--"):
            relative_path(args[6])
            self.attributes(args[6])
            admitted = not input_data
        if kind == "annex_add" and len(args) == 5 and args[:3] == ("annex", "setkey", "--"):
            from modelark.publication_policy import parse_sha256_key
            from modelark.publication_payload import _regular
            size, digest = parse_sha256_key(args[3])
            relative_path(args[4])
            identity = _regular(self.tree, args[4], size, digest)
            if value.get("content_input") != {"path": args[4], "identity": list(identity),
                                               "stored_bytes": size, "stored_sha256": digest}:
                raise PublicationRefused("PUBLICATION_COMMAND_CONTENT_CHANGED")
            admitted = not input_data
        if kind == "annex_add" and len(args) == 4 and args[:3] == ("add", "-f", "--"):
            relative_path(args[3])
            # This path only stages an independently verified, operation-owned
            # symlink. Never allow the forced Git path to stage raw payload bytes.
            with self.tree.parent(args[3]) as (parent, name):
                if not stat.S_ISLNK(os.stat(name, dir_fd=parent, follow_symlinks=False).st_mode):
                    raise PublicationRefused("PUBLICATION_COMMAND_POINTER_REQUIRED")
            admitted = not input_data
        if kind == "file_commit" and len(args) == 6 and args[:5] == (
                "commit", "--quiet", "--no-gpg-sign", "--no-verify", "-m"):
            admitted = args[5] == "ModelArk publication " + self.scope.operation_id and not input_data
        if kind == "annex_metadata" and args == (
                "annex", "sync", "--only-annex", "--no-content", "--no-pull", "--no-push", "--no-commit"):
            admitted = not input_data
        if kind == "map_stage" and args == (
                "annex", "sync", "--only-annex", "--no-content", "--no-pull", "--no-push", "--no-commit"):
            admitted = not input_data
        if (kind == "map_stage" and len(args) == 4
                and args[:2] == ("update-ref", "refs/remotes/sealed/git-annex")):
            admitted = bool(_OID.fullmatch(args[2]) and _OID.fullmatch(args[3])) and not input_data
        if kind == "map_stage" and args == ("update-index", "-z", "--index-info"):
            from modelark.publication_map_stage import require_stage
            from modelark.publication_tree import TreeEntry
            require_stage(self)
            if not input_data or not input_data.endswith(b"\0"):
                raise PublicationRefused("PUBLICATION_COMMAND_INPUT_INVALID")
            seen = set()
            try:
                for row in input_data.split(b"\0")[:-1]:
                    header, path = row.split(b"\t", 1)
                    mode, oid = header.decode("ascii").split(" ")
                    entry = TreeEntry(path.decode("utf-8"), mode, oid)
                    if entry.mode not in {"100644", "120000"} or entry.path in seen:
                        raise ValueError()
                    seen.add(entry.path)
            except (TypeError, ValueError, UnicodeError) as exc:
                raise PublicationRefused("PUBLICATION_COMMAND_INPUT_INVALID") from exc
            admitted = True
        if kind == "map_stage" and args == ("write-tree",):
            from modelark.publication_map_stage import require_stage
            require_stage(self)
            admitted = not input_data
        if kind == "map_refs" and args == ("update-ref", "--stdin"):
            from modelark.publication_map import require_map_action, ref_transaction
            plan = require_map_action(self, record)
            admitted = input_data == ref_transaction(plan)
        if kind == "map_checkout" and len(args) == 4 and args[:3] == ("read-tree", "--reset", "-u"):
            from modelark.publication_map import require_map_action
            plan = require_map_action(self, record)
            admitted = args[3] == plan["files"]["new_head"] and not input_data
        if (kind == "map_stage" and len(args) == 6 and args[0] == "commit-tree"
                and args[2] == "-p" and args[4] == "-m"):
            from modelark.publication_map_stage import require_stage
            require_stage(self)
            admitted = bool(_OID.fullmatch(args[1]) and _OID.fullmatch(args[3])) and not input_data
            admitted = admitted and args[5] == "ModelArk map publication " + self.scope.operation_id
        if kind == "annex_metadata" and len(args) >= 5 and args[:2] == ("annex", "metadata"):
            from modelark.publication_policy import parse_sha256_key
            if args[2].startswith("--key=") and len(args[3:]) % 2 == 0:
                parse_sha256_key(args[2][6:])
                seen = set()
                admitted = not input_data
                for offset in range(3, len(args), 2):
                    name, separator, setting = args[offset + 1].partition("=")
                    if (args[offset] != "-s" or not separator or name not in {"model", "format", "quant", "params"}
                            or name in seen or not setting or "\0" in setting or "\n" in setting):
                        admitted = False
                    seen.add(name)
        if not admitted:
            raise PublicationRefused("PUBLICATION_COMMAND_UNQUALIFIED")
        self.ensure()
        result = b""
        for command in commands:
            result = self._exec(command, input_data=input_data, limit=1024 * 1024)
        self.ensure()
        if actions.read(self.scope, action_id)["intent_digest"] != record["intent_digest"]:
            raise PublicationRefused("PUBLICATION_COMMAND_INTENT_CHANGED")
        return result

    def _fetch_action(self, action_id, source, oid, ref):
        """Fetch one sealed object closure from a retained same-scope local repo.

        The local protocol exception is confined to this constructor: callers
        cannot supply an URL, remote helper, mutable configured remote or path.
        Native annex cannot see the quarantine namespace until admission.
        """
        from modelark import publication_actions as actions
        if (type(source) is not QualifiedRepository or source.scope is not self.scope
                or not _OID.fullmatch(oid) or not ref.startswith("refs/modelark/") or not _ref_or_oid(ref)):
            raise PublicationRefused("PUBLICATION_LOCAL_FETCH_UNQUALIFIED")
        source.ensure()
        self.ensure()
        record = actions.read(self.scope, action_id)
        semantic = ("fetch-local-object", source.profile.digest, oid, ref)
        profile = self.profile.record()
        if (record["kind"] != "map_stage" or record["status"] != "PREPARED"
                or record["intent"]["profile_digest"] != self.profile.digest
                or record["intent"]["root"] != {name: profile[name] for name in ("root_identity", "mount_id", "annex_uuid")}
                or record["intent"].get("command") != {"argv": list(semantic), "stdin_digest": hashlib.sha256(b"").hexdigest()}):
            raise PublicationRefused("PUBLICATION_COMMAND_INTENT_MISMATCH")
        # Verify the sealed object exists at the retained source before fetching;
        # no source ref expression or operator-controlled remote is consulted.
        if source.read("rev-parse", "--verify", oid) != oid.encode() + b"\n":
            raise PublicationRefused("PUBLICATION_FETCH_SOURCE_CHANGED")
        args = ("-c", "protocol.file.allow=always", "fetch", "--quiet", "--no-tags", "--no-write-fetch-head",
                "--no-recurse-submodules", "--no-auto-maintenance", f"/proc/self/fd/{source.tree.fd}", oid + ":" + ref)
        result = self._exec(args, extra_fds=(source.tree.fd,), limit=1024 * 1024)
        source.ensure()
        self.ensure()
        if self.read("rev-parse", "--verify", ref) != oid.encode() + b"\n":
            raise PublicationRefused("PUBLICATION_FETCH_REF_MISMATCH")
        actions.read(self.scope, action_id)
        return result
