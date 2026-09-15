"""Real v9 acquisition routing; temporary repositories and synthetic devices only."""
import hashlib
import os
import sqlite3
from types import SimpleNamespace

import pytest

from modelark import archive_manifest, fetch, fetch_publication, publication_store as store
from modelark.publication_policy import PublicationRefused
from test_archive_publisher import fleet as archive_fleet  # noqa: F401
from test_publication_native import _connection, git_repository, publication_connection  # noqa: F401
from test_publication_native import prepared as native_prepared  # noqa: F401
from test_publication_replica_pipeline import replica_fleet as source_fleet, seed  # noqa: F401


@pytest.fixture
def fleet(request):
    return request.getfixturevalue("archive_fleet")


@pytest.fixture
def replica_fleet(request):
    return request.getfixturevalue("source_fleet")


def setup_download(con, fleet, monkeypatch, *, filename=".gitignore"):
    archive, _, _ = fleet
    data = b"# original upstream file\n*.safetensors\n"
    digest = hashlib.sha256(data).hexdigest()
    con.execute("INSERT INTO files(repo_id,rfilename,size_bytes,format,sha256) VALUES('org/a',?,?,'aux',?)",
                [filename, len(data), digest])
    manifest = archive_manifest.ManifestFile(filename, len(data), digest, "aux", None, "raw")
    downloaded = []
    def download(ctx, repo_id, name, stage_dir, base, *, inherit_fds):
        assert ctx._publication is not None
        assert len(inherit_fds) >= 3
        for descriptor in inherit_fds:
            os.fstat(descriptor)
        assert not (archive / "org/a").exists()  # No pre-intent model directory.
        assert stage_dir.is_relative_to(archive / ".git" / "annex" / "tmp")
        assert stage_dir.parent.name == ctx._publication.operation_id
        assert stage_dir.name == hashlib.sha256(repo_id.encode()).hexdigest()
        path = stage_dir / "downloaded"
        path.write_bytes(data)
        downloaded.append(path)
        return path
    monkeypatch.setattr(fetch, "_download_shard", download)
    monkeypatch.setattr(fetch, "_compression_from_ctx", lambda _: {"threads": 1, "max_compress_ram_gb": 1})
    def legacy(*args, **kwargs):
        raise AssertionError("v9 must never reach legacy IO")
    for name in ("_is_annex", "_publish_staged", "_annex_add", "_annex_metadata", "_dest_writable"):
        monkeypatch.setattr(fetch, name, legacy)
    return manifest, downloaded


def test_real_v9_acquisition_publishes_pair_and_keeps_failure_revision_bound(con, fleet, monkeypatch):
    manifest, downloaded = setup_download(con, fleet, monkeypatch)
    captured = {}
    def stop_at_closure(owner):
        captured["revision"] = con.execute("SELECT last_revision FROM publication_operations").fetchone()[0]
        captured["events"] = con.execute("SELECT count(*) FROM fetch_events").fetchone()[0]
        raise PublicationRefused("INJECTED_CLOSURE_REFUSAL")
    monkeypatch.setattr(fetch_publication.ArchivePublisher, "finish", stop_at_closure)
    ctx = fetch.RunCtx(con)
    result = fetch.run(dest=fleet[0], drive_label="d0", repos=["org/a"], max_24h_gb=0, ctx=ctx,
                       task_manifests={"org/a": (manifest,)})
    assert result["terminal_failure"]["code"] == "INJECTED_CLOSURE_REFUSAL"
    assert result["stored_repos"] == ["org/a"]
    assert con.execute("SELECT phase FROM publication_files").fetchone() == ("CATALOG_PUBLISHED",)
    assert con.execute("SELECT present FROM replicas WHERE rfilename='.gitignore'").fetchone() == (1,)
    assert con.execute("SELECT state,last_revision FROM publication_operations").fetchone() == (
        "PREPARED", captured["revision"])
    assert con.execute("SELECT planner_revision FROM planner_state").fetchone() == (captured["revision"],)
    assert con.execute("SELECT count(*) FROM fetch_events").fetchone() == (captured["events"],)
    assert ctx._publication is None
    assert not downloaded[0].exists()  # Exact owned duplicate released only after archive publication.
    assert con.execute("SELECT status FROM publication_actions WHERE kind='staging_release'").fetchone() == ("VERIFIED",)


def test_v9_stopped_work_remains_pending_without_fake_file_or_clean_anchor(con, fleet, monkeypatch):
    manifest, downloaded = setup_download(con, fleet, monkeypatch)
    ctx = fetch.RunCtx(con, should_stop=lambda: True)
    result = fetch.run(dest=fleet[0], drive_label="d0", repos=["org/a"], max_24h_gb=0, ctx=ctx,
                       task_manifests={"org/a": (manifest,)})
    assert result["terminal_failure"]
    assert not downloaded
    assert con.execute("SELECT count(*) FROM publication_files").fetchone() == (0,)
    assert con.execute("SELECT state FROM publication_operations").fetchone() == ("PREPARED",)
    assert con.execute("SELECT count(*) FROM drive_clean_anchors WHERE generation=3").fetchone() == (0,)
    assert ctx._publication is None


def test_v9_stopped_fill_reopens_exact_operation_then_closes(con, fleet, monkeypatch):
    manifest, downloaded = setup_download(con, fleet, monkeypatch)
    ctx = fetch.RunCtx(con, should_stop=lambda: True)
    args = dict(dest=fleet[0], drive_label="d0", repos=["org/a"], max_24h_gb=0, ctx=ctx,
                task_manifests={"org/a": (manifest,)})
    assert fetch.run(**args)["terminal_failure"]
    identifier = con.execute("SELECT operation_id FROM publication_operations").fetchone()[0]
    ctx.should_stop = lambda: False
    result = fetch.run(**args)
    assert result["terminal_failure"] is None
    assert len(downloaded) == 1
    assert con.execute("SELECT operation_id,state FROM publication_operations").fetchall() == [(identifier, "CLOSED")]


def test_v9_unexpected_acquisition_error_does_not_probe_legacy_dest(con, fleet, monkeypatch):
    manifest, _ = setup_download(con, fleet, monkeypatch)
    def boom(*args, **kwargs):
        raise RuntimeError("injected download failure")
    monkeypatch.setattr(fetch, "_download_shard", boom)
    result = fetch.run(dest=fleet[0], drive_label="d0", repos=["org/a"], max_24h_gb=0,
                       ctx=fetch.RunCtx(con), task_manifests={"org/a": (manifest,)})
    assert result["terminal_failure"]["code"] == "PUBLICATION_ACQUISITION_FAILED"
    assert con.execute("SELECT state FROM publication_operations").fetchone() == ("PREPARED",)
    assert con.execute("SELECT count(*) FROM publication_files").fetchone() == (0,)
    assert con.execute("SELECT count(*) FROM drive_clean_anchors WHERE generation=3").fetchone() == (0,)


def test_v9_published_fill_reopens_for_closure_without_redownload(con, fleet, monkeypatch):
    manifest, downloaded = setup_download(con, fleet, monkeypatch)
    original = fetch_publication.ArchivePublisher.finish
    def interrupt(owner):
        raise PublicationRefused("INJECTED_CLOSURE_REFUSAL")
    monkeypatch.setattr(fetch_publication.ArchivePublisher, "finish", interrupt)
    ctx = fetch.RunCtx(con)
    args = dict(dest=fleet[0], drive_label="d0", repos=["org/a"], max_24h_gb=0, ctx=ctx,
                task_manifests={"org/a": (manifest,)})
    assert fetch.run(**args)["terminal_failure"]["code"] == "INJECTED_CLOSURE_REFUSAL"
    identifier = con.execute("SELECT operation_id FROM publication_operations").fetchone()[0]
    monkeypatch.setattr(fetch_publication.ArchivePublisher, "finish", original)
    assert fetch.run(**args)["terminal_failure"] is None
    assert len(downloaded) == 1
    assert con.execute("SELECT operation_id,state FROM publication_operations").fetchall() == [(identifier, "CLOSED")]


def test_v9_pending_fill_refuses_changed_workset_or_owner(con, fleet, monkeypatch):
    manifest, _ = setup_download(con, fleet, monkeypatch)
    ctx = fetch.RunCtx(con, should_stop=lambda: True)
    fetch.run(dest=fleet[0], drive_label="d0", repos=["org/a"], max_24h_gb=0, ctx=ctx,
              task_manifests={"org/a": (manifest,)})
    revision = store._revision(con)
    with pytest.raises(PublicationRefused, match="RESUME_WORKSET_CHANGED"):
        fetch_publication.fill_requests(ctx, ["org/a"], "d0", {"org/a": ()})
    ctx.session_id, ctx.fencing_token = "another-owner", 99
    with pytest.raises(PublicationRefused, match="RESUME_OWNER_OR_WORKSET_CHANGED"):
        fetch_publication.fill_requests(ctx, ["org/a"], "d0", {"org/a": (manifest,)})
    assert store._revision(con) == revision


def test_v9_direct_model_and_whole_repo_replica_require_owning_adapters(con, fleet, monkeypatch):
    manifest, _ = setup_download(con, fleet, monkeypatch)
    ctx = fetch.RunCtx(con)
    with pytest.raises(PublicationRefused, match="FETCH_SCOPE_REQUIRED"):
        fetch.fetch_model(ctx, "org/a", fleet[0], "d0", True, {}, manifest=(manifest,))
    with pytest.raises(PublicationRefused, match="EXACT_REPLICA_TASKS_REQUIRED"):
        fetch.run_replica({"d0": [{"repo": "org/a"}]}, "d1", ctx)
    assert con.execute("SELECT count(*) FROM publication_operations").fetchone() == (0,)


def test_v9_missing_exact_manifest_refuses_before_staging(con, fleet, monkeypatch):
    _, downloaded = setup_download(con, fleet, monkeypatch)
    result = fetch.run(dest=fleet[0], drive_label="d0", repos=["org/a"], max_24h_gb=0, ctx=fetch.RunCtx(con),
                       task_manifests={})
    assert result["terminal_failure"]["code"] == "PUBLICATION_TASK_MANIFEST_MISSING"
    assert not downloaded
    assert con.execute("SELECT count(*) FROM publication_operations").fetchone() == (0,)


def test_v9_replica_tasks_refuse_stale_target_without_legacy_heal(con, fleet, monkeypatch):
    def legacy(*args, **kwargs):
        raise AssertionError("no legacy replica or native probe")
    monkeypatch.setattr(fetch, "_dest_writable", legacy)
    con.execute("INSERT INTO files(repo_id,rfilename,size_bytes,format) VALUES('org/a','config.json',1,'aux')")
    con.execute("INSERT INTO archived(repo_id,rfilename,drive_label,compressed) VALUES('org/a','config.json','d0',0)")
    task = SimpleNamespace(source_drive="d1", target_drive="d0", repo_id="org/a", requirement_id="r1",
                           budget=SimpleNamespace(missing_files=("config.json",)))
    result = fetch.run_replica_tasks([task], fetch.RunCtx(con))
    assert result["failed"][0]["code"] == "PUBLICATION_REPLICA_TARGET_REQUIRES_RECONCILIATION"
    assert result["completed_requirements"] == []
    assert store.library(con) is not None


def test_publication_writer_isolated_from_ui_read_transaction(con, fleet, monkeypatch):
    manifest, _ = setup_download(con, fleet, monkeypatch)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=1700")
    ctx = fetch.RunCtx(con)
    request = fetch_publication.FileRequest("org/a", manifest.rfilename, "d0")
    with pytest.raises(PublicationRefused, match="TEST_PENDING"):
        with fetch_publication.scope(ctx, [request], destination=fleet[0], drive_label="d0"):
            owner = ctx._publication
            assert owner._connection is not con
            assert owner._connection.execute("PRAGMA foreign_keys").fetchone() == (1,)
            assert owner._connection.execute("PRAGMA busy_timeout").fetchone() == (1700,)
            con.execute("BEGIN")
            old_count = con.execute("SELECT count(*) FROM fetch_events").fetchone()[0]
            ctx.write(lambda dedicated: fetch._event(dedicated, "org/a", "archived", detail="isolated"))
            assert con.in_transaction
            assert con.execute("SELECT count(*) FROM fetch_events").fetchone()[0] == old_count
            assert owner._connection.execute("SELECT count(*) FROM fetch_events").fetchone()[0] == old_count + 1
            con.rollback()
            assert con.execute("SELECT count(*) FROM fetch_events").fetchone()[0] == old_count + 1
            assert owner._connection.execute("SELECT last_revision FROM publication_operations").fetchone() == (
                owner._connection.execute("SELECT planner_revision FROM planner_state").fetchone()[0],)
            raise PublicationRefused("TEST_PENDING")
    assert ctx._publication is None


def test_publication_connection_refuses_existing_ui_write_transaction(con, fleet):
    ctx = fetch.RunCtx(con)
    con.execute("BEGIN")
    try:
        with pytest.raises(PublicationRefused, match="CATALOG_TRANSACTION_ACTIVE"):
            with fetch_publication._connection(ctx):
                raise AssertionError("must not enter")
    finally:
        con.rollback()


def test_dedicated_connection_refuses_catalog_path_replacement(con, tmp_path):
    ctx = fetch.RunCtx(con)
    path = fetch_publication._catalog_path(con)
    replacement = tmp_path / "replacement.sqlite"
    with sqlite3.connect(replacement) as copied:
        con.backup(copied)
    retained = tmp_path / "retained.sqlite"
    swapped = False
    try:
        with pytest.raises(PublicationRefused, match="CATALOG_IDENTITY_CHANGED"):
            with fetch_publication._connection(ctx):
                path.rename(retained)
                replacement.rename(path)
                swapped = True
    finally:
        if swapped:
            path.rename(replacement)
            retained.rename(path)


@pytest.mark.parametrize("configured", [None, "multiple", "wrong", "relative"])
def test_legacy_map_routing_refuses_unbound_remote(con, tmp_path, monkeypatch, configured):
    selected, wrong = tmp_path / "selected", tmp_path / "wrong"
    selected.mkdir()
    wrong.mkdir()
    value = {None: "", "multiple": f"{selected}\n{wrong}", "wrong": str(wrong), "relative": "selected"}[configured]
    monkeypatch.setattr(fetch_publication.register, "_git", lambda *args, **kwargs: value)
    with pytest.raises(PublicationRefused, match="MAP_REMOTE_MISMATCH"):
        fetch_publication.legacy_map_targets(tmp_path, {"d0": selected})


def test_legacy_map_routing_accepts_only_exact_current_paths(tmp_path, monkeypatch):
    selected = tmp_path / "selected"
    selected.mkdir()
    seen = []
    def config(*args, **kwargs):
        seen.append(args)
        return str(selected)
    monkeypatch.setattr(fetch_publication.register, "_git", config)
    fetch_publication.legacy_map_targets(tmp_path, {"d0": selected})
    assert seen == [(tmp_path, "config", "--local", "--get-all", "remote.d0.url")]


def test_real_v9_replica_adapter_publishes_target_before_closure_refusal(con, replica_fleet, tmp_path, monkeypatch):
    request, source_path, _, key, original = seed(con, replica_fleet, tmp_path, backend="SHA256")
    def legacy(*args, **kwargs):
        raise AssertionError("v9 replica must never use legacy transport")
    monkeypatch.setattr(fetch, "_dest_writable", legacy)
    def pending(owner):
        raise PublicationRefused("TEST_REPLICA_PENDING")
    monkeypatch.setattr(fetch_publication.ArchivePublisher, "finish", pending)
    task = SimpleNamespace(source_drive="d1", target_drive="d0", repo_id=request.repo_id, requirement_id="r1",
                           budget=SimpleNamespace(missing_files=(request.rfilename,)))
    ctx = fetch.RunCtx(con)
    result = fetch.run_replica_tasks([task], ctx)
    assert result["copied_files"] == 1
    assert result["completed_requirements"] == []
    assert result["failed"][0]["code"] == "TEST_REPLICA_PENDING"
    assert con.execute("SELECT annex_key,present FROM replicas WHERE drive_label='d0'").fetchone() == (key, 1)
    assert source_path.read_bytes() == original
    assert ctx._publication is None


def test_real_v9_acquisition_completes_enclosing_publication(con, fleet, monkeypatch):
    manifest, _ = setup_download(con, fleet, monkeypatch)
    ctx = fetch.RunCtx(con)
    result = fetch.run(dest=fleet[0], drive_label="d0", repos=["org/a"], max_24h_gb=0, ctx=ctx,
                       task_manifests={"org/a": (manifest,)})
    assert result["terminal_failure"] is None, result
    assert con.execute("SELECT state FROM publication_operations").fetchone() == ("CLOSED",)
    assert con.execute("SELECT count(*) FROM drive_clean_anchors WHERE generation=3").fetchone() == (1,)
    assert ctx._publication is None
