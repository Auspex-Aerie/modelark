"""Journalled, path-scoped annex attribute policy, never upstream file rewriting.

The owning coordinator includes both qualified profiles and this exact byte plan
in PREPARED before calling apply. This module only changes .git/info/attributes;
upstream .gitattributes/.gitignore payloads remain separately mapped bytes.
"""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
import stat

from modelark import publication_actions as actions, publication_store as store
from modelark.publication_native import QualifiedRepository
from modelark.publication_policy import PublicationRefused, relative_path


def _pattern(path):
    # Git attributes use glob patterns even inside C-style quotes. Escape glob
    # metacharacters first, then quote control characters without losing UTF-8.
    relative_path(path)
    escaped = "".join("\\" + char if char in "*?[]\\" else char for char in path)
    return json.dumps("/" + escaped, ensure_ascii=False)


def preview(repository, paths):
    """Read-only exact setup plan; neither a caller request nor a write permit."""
    if type(repository) is not QualifiedRepository:
        raise PublicationRefused("PUBLICATION_NATIVE_CAPABILITY_REQUIRED")
    paths = sorted(set(paths))
    if not paths:
        raise PublicationRefused("PUBLICATION_POLICY_PATHS_REQUIRED")
    repository.ensure()
    before = repository._file(".git/info/attributes", absent=True)
    with repository.tree.parent(".git/info/attributes") as (parent, name):
        try:
            value = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISREG(value.st_mode):
                raise PublicationRefused("PUBLICATION_POLICY_FILE_INVALID")
            exists = True
        except FileNotFoundError:
            exists = False
    lines = [_pattern(path) + " filter=annex -text -ident !working-tree-encoding !eol "
             "annex.largefiles=anything annex.backend=SHA256\n" for path in paths]
    after = before + (b"\n" if before and not before.endswith(b"\n") else b"")
    after += b"# ModelArk journalled publication paths\n" + "".join(lines).encode("utf-8")
    if len(after) > 1024 * 1024:
        raise PublicationRefused("PUBLICATION_POLICY_SIZE_LIMIT")
    prospective = replace(repository.profile, attributes_sha256=hashlib.sha256(after).hexdigest())
    return {"version": 1, "path": ".git/info/attributes", "paths": paths,
            "before_exists": exists, "before_hex": before.hex(), "after_hex": after.hex(),
            "before_profile": repository.profile.record(), "after_profile": prospective.record()}


def _validated(repository, record):
    if record["kind"] != "policy_setup":
        raise PublicationRefused("PUBLICATION_POLICY_INTENT_MISMATCH")
    plan = record["intent"].get("filesystem_plan")
    if (type(plan) is not dict or set(plan) != {"version", "path", "paths", "before_exists", "before_hex",
                                               "after_hex", "before_profile", "after_profile"}
            or plan["version"] != 1 or plan["path"] != ".git/info/attributes"
            or type(plan["before_exists"]) is not bool or type(plan["paths"]) is not list
            or not plan["paths"] or sorted(set(plan["paths"])) != plan["paths"]):
        raise PublicationRefused("PUBLICATION_POLICY_INTENT_MISMATCH")
    try:
        before, after = bytes.fromhex(plan["before_hex"]), bytes.fromhex(plan["after_hex"])
    except (TypeError, ValueError) as exc:
        raise PublicationRefused("PUBLICATION_POLICY_INTENT_MISMATCH") from exc
    expected = before + (b"\n" if before and not before.endswith(b"\n") else b"")
    expected += b"# ModelArk journalled publication paths\n" + "".join(
        _pattern(path) + " filter=annex -text -ident !working-tree-encoding !eol "
        "annex.largefiles=anything annex.backend=SHA256\n" for path in plan["paths"]).encode("utf-8")
    if (after != expected or len(after) > 1024 * 1024
            or plan["after_profile"] != {**plan["before_profile"], "attributes_sha256": hashlib.sha256(after).hexdigest()}
            or plan["before_profile"].get("attributes_sha256") != hashlib.sha256(before).hexdigest()
            or store.digest(plan["before_profile"]) != record["intent"]["profile_digest"]):
        raise PublicationRefused("PUBLICATION_POLICY_INTENT_MISMATCH")
    current = repository.profile.record()
    if current not in (plan["before_profile"], plan["after_profile"]):
        raise PublicationRefused("PUBLICATION_PROFILE_CHANGED")
    return plan, before, after


def apply(repository, action_id):
    """Apply or acknowledge only the exact journalled old/new policy states.

    A partial temporary file is refused rather than silently replaced. The
    durable obligation remains for explicit recovery; no rollback of a native
    profile or clean-anchor publication occurs here.
    """
    if type(repository) is not QualifiedRepository:
        raise PublicationRefused("PUBLICATION_NATIVE_CAPABILITY_REQUIRED")
    scope = repository.scope
    record = actions.read(scope, action_id)
    plan, before, after = _validated(repository, record)
    repository.ensure()
    current = repository._file(plan["path"], absent=True)
    if current not in (before, after):
        raise PublicationRefused("PUBLICATION_POLICY_BEFORE_STATE_CHANGED")
    if record["status"] == "VERIFIED" and current != after:
        raise PublicationRefused("PUBLICATION_POLICY_RECEIPT_STALE")
    if current != after:
        temporary = "modelark-policy-" + store.canonical_uuid(action_id) + ".tmp"
        with repository.tree.parent(plan["path"]) as (parent, name):
            try:
                os.stat(name, dir_fd=parent, follow_symlinks=False)
                exists = True
            except FileNotFoundError:
                exists = False
            if exists != plan["before_exists"]:
                raise PublicationRefused("PUBLICATION_POLICY_BEFORE_STATE_CHANGED")
            try:
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
            except FileExistsError:
                # Only a complete byte-identical temporary is an admitted resume.
                if repository._file(".git/info/" + temporary) != after:
                    raise PublicationRefused("PUBLICATION_POLICY_TEMPORARY_UNPROVEN") from None
            else:
                try:
                    remaining = memoryview(after)
                    while remaining:
                        remaining = remaining[os.write(fd, remaining):]
                    os.fsync(fd)
                finally:
                    os.close(fd)
            scope.require_io()
            repository.ensure()
            if repository._file(plan["path"], absent=True) != before:
                raise PublicationRefused("PUBLICATION_POLICY_BEFORE_STATE_CHANGED")
            os.rename(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
        # Only adopt the exact prospective profile sealed before mutation.
        prospective = repository._inspect()
        if prospective.record() != plan["after_profile"]:
            raise PublicationRefused("PUBLICATION_POLICY_AFTER_STATE_CHANGED")
        repository.profile = prospective
    for path in plan["paths"]:
        repository.attributes(path)
    repository.ensure()
    receipt = {"version": 1, "profile": plan["after_profile"], "paths": plan["paths"],
               "attributes_sha256": hashlib.sha256(after).hexdigest()}
    return scope.write(lambda _: actions.verify(scope, action_id=action_id,
                                                intent_digest=record["intent_digest"], receipt=receipt))
