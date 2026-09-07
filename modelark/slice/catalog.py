"""One explicit, read-only SQLite snapshot for a Usable Slice domain proposal.

Never configures the global catalog, bootstraps schemas, resolves source paths, calls annex,
or acquires archive mutation fences. Mount availability is deliberately not a catalog fact.
"""
from __future__ import annotations

from pathlib import Path, PurePosixPath
import sqlite3

from modelark import archive_manifest
from .domain import AnchorFact, CatalogSnapshot, CopyFact, DriveFact, FileFact, Gap, SliceRefusal, SliceSpec


def read_catalog(path: str | Path, spec: SliceSpec) -> CatalogSnapshot:
    catalog = Path(path).expanduser().resolve()
    con = None
    try:
        con = sqlite3.connect(f"{catalog.as_uri()}?mode=ro", uri=True, isolation_level=None)
        con.execute("PRAGMA query_only=ON")
        con.execute("BEGIN")
        version = con.execute("PRAGMA user_version").fetchone()[0]
        if version != 7:
            raise SliceRefusal("CATALOG_VERSION_UNSUPPORTED", f"expected 7, observed {version}")
        files, copies, issues = [], [], []
        # Bounded batches avoid SQLite's variable limit for large explicit subsections.
        for start in range(0, len(spec.repo_ids), 400):
            repos = spec.repo_ids[start:start + 400]
            marks = ",".join("?" for _ in repos)
            rows = con.execute(
                "SELECT repo_id,rfilename,size_bytes,sha256,format,quant FROM files "
                f"WHERE repo_id IN ({marks})", repos).fetchall()
            manifests = archive_manifest.inspect_manifests_for_repos(
                con, repos, archive_manifest.recovery_policy())
            names = {(repo, f.rfilename) for repo, manifest in manifests.manifests.items() for f in manifest}
            files.extend(FileFact(*row) for row in rows if (row[0], row[1]) in names)
            issues.extend(Gap(repo, None, "MANIFEST_UNAVAILABLE", detail=str(error))
                          for repo, error in manifests.errors.items())
            rows = con.execute(
                "SELECT a.repo_id,a.rfilename,a.drive_label,a.stored_relpath,a.stored_name,"
                "a.orig_bytes,a.stored_bytes,a.orig_sha256,a.orig_sha256_provenance,a.annex_key,"
                "a.compressed,r.present FROM archived a LEFT JOIN replicas r "
                "ON r.repo_id=a.repo_id AND r.rfilename=a.rfilename AND r.drive_label=a.drive_label "
                f"WHERE a.repo_id IN ({marks})", repos).fetchall()
            for repo, name, label, rel, legacy, orig_size, stored_size, digest, provenance, key, compressed, present in rows:
                if (repo, name) not in names:
                    continue
                # Match existing restore's legacy basename layout, without touching its source adapter.
                if rel is None and legacy:
                    rel = str(PurePosixPath(name).parent / legacy)
                copies.append(CopyFact(repo, name, label, rel, orig_size, stored_size, digest,
                                       provenance, key, bool(compressed),
                                       None if present is None else bool(present)))
        labels = {copy.drive_label for copy in copies}
        drives = tuple(DriveFact(*row) for row in con.execute(
            "SELECT drive_label,fs_uuid,annex_uuid,serial,identity_epoch,write_generation,"
            "identity_fingerprint,filesystem_capacity_bytes,write_authority,lifecycle,eligibility "
            "FROM drives") if row[0] in labels)
        anchors = tuple(AnchorFact(*row[:-1], str(row[-1])) for row in con.execute(
            "SELECT a.drive_label,a.identity_epoch,a.generation,a.identity_fingerprint,"
            "a.filesystem_capacity_bytes,a.write_authority,a.anchor_id FROM drive_clean_anchors a "
            "JOIN drives d ON d.drive_label=a.drive_label AND d.identity_epoch=a.identity_epoch "
            "AND d.write_generation=a.generation") if row[0] in labels)
        return CatalogSnapshot(catalog.as_uri(), tuple(files), tuple(copies), drives, anchors,
                               tuple(issues), version)
    except sqlite3.Error as exc:
        raise SliceRefusal("CATALOG_UNAVAILABLE", str(exc)) from exc
    finally:
        if con is not None:
            con.close()  # Rolls back the read transaction, including on schema/query failure.
