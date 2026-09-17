"""Exact archived/copy/source-fact CAS inside the publication owner's transaction.

This module proves a catalog transition, not physical bytes or Git state. The
publisher must establish those independently before calling the store transition
with this callback. No connection, transaction, revision, or timestamp is created
here; the complete intended rows (including explicit absence) belong in PREPARED.
"""
from __future__ import annotations

from modelark import publication_store as store
from modelark.publication_policy import PublicationRefused, relative_path


_COLUMNS = {
    "files": ("repo_id", "rfilename", "size_bytes", "is_lfs", "sha256", "format", "quant", "quant_bits", "safety"),
    "archived": ("repo_id", "rfilename", "stored_name", "stored_relpath", "drive_label", "orig_sha256",
                 "znn_sha256", "orig_bytes", "stored_bytes", "compressed", "annex_key", "verified_at",
                 "orig_sha256_provenance"),
    "replicas": ("repo_id", "rfilename", "drive_label", "annex_key", "present", "verified_at", "added_at"),
}
_KEYS = {table: ("repo_id", "rfilename") + (() if table == "files" else ("drive_label",))
         for table in _COLUMNS}


def _schema(con):
    # A changed table cannot silently add facts outside our before/after binding.
    for table, columns in _COLUMNS.items():
        observed = [row[1] for row in con.execute(f"PRAGMA table_info({table})")]
        # Historical ALTER TABLE migrations append stored_relpath/provenance.
        # All IO below names columns explicitly; physical order is not a fact.
        if len(observed) != len(columns) or set(observed) != set(columns):
            raise PublicationRefused("PUBLICATION_CATALOG_SCHEMA_UNQUALIFIED", table=table)


def _row(con, table, key):
    keys = _KEYS[table]
    rows = con.execute(f"SELECT {','.join(_COLUMNS[table])} FROM {table} WHERE "
                       + " AND ".join(f"{column} IS ?" for column in keys),
                       [key[column] for column in keys]).fetchall()
    if len(rows) > 1:
        raise PublicationRefused("PUBLICATION_CATALOG_ROW_AMBIGUOUS", table=table)
    return dict(zip(_COLUMNS[table], rows[0])) if rows else None


def _identity(repo_id, rfilename, drive_label, scope):
    if len(relative_path(repo_id).parts) != 2:
        raise PublicationRefused("PUBLICATION_REPOSITORY_INVALID")
    relative_path(rfilename)
    if drive_label not in scope.identities:
        raise PublicationRefused("PUBLICATION_CATALOG_PARTICIPANT_MISMATCH")
    return {"repo_id": repo_id, "rfilename": rfilename, "drive_label": drive_label}


def capture(scope, *, repo_id, rfilename, drive_label, source_drive=None):
    """Capture the complete target pair and source facts in one owned snapshot.

    Replica publication additionally binds the selected source's *whole* pair,
    not just its key. Capture does not assert either endpoint physically holds it.
    """
    con = scope.connection
    scope.require_transaction(con)
    return _capture_rows(scope, repo_id=repo_id, rfilename=rfilename,
                         drive_label=drive_label, source_drive=source_drive)


def _capture_rows(scope, *, repo_id, rfilename, drive_label, source_drive=None):
    """DB-only snapshot primitive; grants no transition or physical authority."""
    con = scope.connection
    scope.require()
    if not con.in_transaction:
        raise PublicationRefused("PUBLICATION_CATALOG_SNAPSHOT_REQUIRED")
    _schema(con)
    key = _identity(repo_id, rfilename, drive_label, scope)
    facts = _row(con, "files", key)
    if facts is None:
        raise PublicationRefused("PUBLICATION_SOURCE_FACTS_MISSING")
    source = None
    if source_drive is not None:
        source_key = _identity(repo_id, rfilename, source_drive, scope)
        if source_drive == drive_label:
            raise PublicationRefused("PUBLICATION_REPLICA_SOURCE_IS_TARGET")
        source = {"key": source_key, "archived": _row(con, "archived", source_key),
                  "replicas": _row(con, "replicas", source_key)}
        if source["archived"] is None:
            raise PublicationRefused("PUBLICATION_REPLICA_SOURCE_MISSING")
    return {"version": 1, "key": key, "files": facts,
            "archived": _row(con, "archived", key), "replicas": _row(con, "replicas", key), "source": source}


def intended_pair(before, *, archived, replicas):
    """Freeze complete post-state; this pure builder neither writes nor proves IO.

    An absent replica stays absent only when explicitly requested. Existing copy
    rows cannot be silently deleted, and no other drive can be inserted here.
    The enclosing publisher determines whether the proven operation merits a copy
    assertion. Source discovery facts are compared, never rewritten by ingestion.
    """
    value = {"version": 1, "before": before, "after": {"archived": archived, "replicas": replicas}}
    _validate(value)
    # Detach from caller-owned mutable dictionaries before sealing in PREPARED.
    import json
    return json.loads(store.canonical(value))


def _validate_row(table, row, key, *, absent=False):
    if row is None and absent:
        return
    if (not isinstance(row, dict) or set(row) != set(_COLUMNS[table])
            or any(row[column] != key[column] for column in _KEYS[table])):
        raise PublicationRefused("PUBLICATION_CATALOG_PAIR_INVALID", table=table)


def _validate(value):
    if not isinstance(value, dict) or set(value) != {"version", "before", "after"} or value["version"] != 1:
        raise PublicationRefused("PUBLICATION_CATALOG_PAIR_INVALID")
    before, after = value["before"], value["after"]
    if (not isinstance(before, dict)
            or set(before) != {"version", "key", "files", "archived", "replicas", "source"}
            or before["version"] != 1 or not isinstance(after, dict) or set(after) != {"archived", "replicas"}):
        raise PublicationRefused("PUBLICATION_CATALOG_PAIR_INVALID")
    key = before["key"]
    if not isinstance(key, dict) or set(key) != set(_KEYS["archived"]):
        raise PublicationRefused("PUBLICATION_CATALOG_PAIR_INVALID")
    _validate_row("files", before["files"], key)
    for table in ("archived", "replicas"):
        _validate_row(table, before[table], key, absent=True)
        _validate_row(table, after[table], key, absent=(table == "replicas" and before[table] is None))
    source = before["source"]
    if source is not None:
        if not isinstance(source, dict) or set(source) != {"key", "archived", "replicas"}:
            raise PublicationRefused("PUBLICATION_CATALOG_PAIR_INVALID")
        source_key = source["key"]
        if (not isinstance(source_key, dict) or set(source_key) != set(key)
                or source_key["repo_id"] != key["repo_id"] or source_key["rfilename"] != key["rfilename"]
                or source_key["drive_label"] == key["drive_label"]):
            raise PublicationRefused("PUBLICATION_CATALOG_PAIR_INVALID")
        _validate_row("archived", source["archived"], source_key)
        _validate_row("replicas", source["replicas"], source_key, absent=True)
    archived, replica = after["archived"], after["replicas"]
    if (not archived["annex_key"] or not archived["stored_relpath"]
            or relative_path(archived["stored_relpath"]).name != archived["stored_name"]):
        raise PublicationRefused("PUBLICATION_CATALOG_PAIR_INVALID")
    if replica is not None and (replica["present"] != 1 or replica["annex_key"] != archived["annex_key"]):
        raise PublicationRefused("PUBLICATION_CATALOG_COPY_MISMATCH")


def compare_and_swap(scope, value):
    """DB-only callback, used exclusively inside CATALOG_PUBLISHED's owned TX.

    Checks every source/target field before either write and verifies the entire
    intended pair afterward. Trigger changes are caught too. Any failure must
    propagate to the owning adapter (which rolls back phase, pair and revision).
    No upsert-by-key shortcut, fabricated timestamps or implicit replay/adoption.
    """
    con = scope.connection
    scope.require_transaction(con)
    store.require_catalog_transition(scope, value)
    _schema(con)
    _validate(value)
    before, after = value["before"], value["after"]
    key = before["key"]
    _identity(**key, scope=scope)
    observed = capture(scope, **key, source_drive=(before["source"]["key"]["drive_label"]
                                                 if before["source"] else None))
    if store.canonical(observed) != store.canonical(before):
        raise PublicationRefused("PUBLICATION_CATALOG_PAIR_STALE")
    for table in ("archived", "replicas"):
        intended = after[table]
        if intended is None:
            continue
        columns = _COLUMNS[table]
        if before[table] is None:
            con.execute(f"INSERT INTO {table}({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
                        [intended[column] for column in columns])
        else:
            # Exact full-row SQL predicate also guards against trigger-induced
            # changes from the first write before we attempt the second write.
            changed = con.execute(f"UPDATE {table} SET " + ",".join(f"{column}=?" for column in columns)
                                  + " WHERE " + " AND ".join(f"{column} IS ?" for column in columns),
                                  [intended[column] for column in columns] + [before[table][column] for column in columns])
            if changed.rowcount != 1:
                raise PublicationRefused("PUBLICATION_CATALOG_PAIR_STALE")
    observed = capture(scope, **key, source_drive=(before["source"]["key"]["drive_label"]
                                                 if before["source"] else None))
    expected = {**before, **after}
    if store.canonical(observed) != store.canonical(expected):
        raise PublicationRefused("PUBLICATION_CATALOG_PAIR_POSTCONDITION_FAILED")
    return {"version": 1, "before_digest": store.digest(before), "after_digest": store.digest(expected)}
