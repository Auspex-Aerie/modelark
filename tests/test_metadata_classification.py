"""Upstream controls are inert payload; catalog correction is explicit and sealed."""
from dataclasses import replace
import sqlite3
from unittest import mock

import pytest

from modelark import archive_manifest as manifest, formats, proposal
from modelark.core import db


@pytest.fixture
def con():
    value = sqlite3.connect(":memory:", isolation_level=None)
    for statement in db._statements(db.SCHEMA_PATH.read_text()):
        value.execute(statement)
    value.execute("PRAGMA user_version=8")
    value.execute("INSERT INTO models(repo_id) VALUES('org/model')")
    value.executemany(
        "INSERT INTO files(repo_id,rfilename,size_bytes,format,safety) "
        "VALUES('org/model',?,?,?,?)",
        [("weights.gguf", 100, "gguf", "safe"),
         (".gitignore", 12, "other", "unknown"),
         ("nested/.gitattributes", 15, "other", "unknown"),
         (".eval/任务.yaml", 23, "aux", "safe"),
         (".secret", 7, "other", "unknown")])
    yield value
    value.close()


@pytest.mark.parametrize("name", [".gitignore", ".gitattributes", "nested/.gitignore",
                                  "pytorch_model/.gitattributes", ".eval/任务.yaml",
                                  ".eval/tests/.results.json"])
def test_supported_metadata_is_auxiliary_payload(name):
    assert formats.classify_file(name) == ("aux", None, None, "safe")


@pytest.mark.parametrize("name", [".GITIGNORE", "foo.gitignore",
                                  ".secret", ".evaluation/arbitrary.unknown"])
def test_exact_controls_do_not_select_every_hidden_file(name):
    assert formats.classify_file(name) == ("other", None, None, "unknown")


def test_existing_gitattributes_extension_policy_is_not_narrowed():
    assert formats.classify_file("saved.gitattributes") == ("aux", None, None, "safe")


@pytest.mark.parametrize("name", [".git/config.json", "nested/.git/weights.gguf", ".git",
                                  "nested/.GIT/README.md", "../.gitignore", "/config.json",
                                  "nested//config.json", "nested\\.gitignore"])
def test_git_administration_and_noncanonical_paths_are_not_payload(name, con):
    assert formats.classify_file(name)[0] == "other"
    con.execute("INSERT INTO files(repo_id,rfilename,size_bytes,format) "
                "VALUES('org/model',?,1,'aux')", (name,))
    with pytest.raises(manifest.ArchivePolicyError, match="not an upstream payload path"):
        manifest.manifest_for_repo(con, "org/model")


def test_preview_does_not_reclassify_legacy_rows_or_mutate_catalog(con):
    before = list(con.iterdump())
    preview = manifest.preview_metadata_classification(con, ["org/model"])
    assert list(con.iterdump()) == before
    assert preview.changed_files == (("org/model", ".gitignore"),
                                     ("org/model", "nested/.gitattributes"))
    assert preview.manifest_changes == (("org/model", (".eval/任务.yaml", "weights.gguf"),
                                        (".eval/任务.yaml", ".gitignore",
                                         "nested/.gitattributes", "weights.gguf")),)
    assert [f.rfilename for f in manifest.manifest_for_repo(con, "org/model")] == [
        ".eval/任务.yaml", "weights.gguf"]


def test_apply_keeps_logical_names_and_invalidates_old_manifest(con):
    frozen = {"tasks": [{"repo_id": "org/model", "row_kind": "baseline_satisfied",
                         "full_manifest_hash": proposal._manifest_hash(con, "org/model")}]}
    preview = manifest.preview_metadata_classification(con, ["org/model"])
    assert manifest.apply_metadata_classification(con, preview) == preview.changed_files
    assert con.execute("SELECT planner_revision FROM planner_state").fetchone() == (1,)
    assert con.execute("SELECT count(*) FROM archived").fetchone() == (0,)
    assert con.execute("SELECT count(*) FROM replicas").fetchone() == (0,)
    records = {f.rfilename: f.as_fetch_record()
               for f in manifest.manifest_for_repo(con, "org/model")}
    assert records[".gitignore"]["mode"] == "raw"
    assert records["nested/.gitattributes"]["rfilename"] == "nested/.gitattributes"
    assert ".secret" not in records
    with pytest.raises(proposal.Refusal) as error:
        proposal.validate_exact_assignment(con, frozen)
    assert error.value.code == "APPROVED_INPUT_CHANGED"
    assert error.value.evidence["reason"] == "full_manifest_hash"
    assert manifest.apply_metadata_classification(
        con, manifest.preview_metadata_classification(con, ["org/model"])) == ()
    assert con.execute("SELECT planner_revision FROM planner_state").fetchone() == (1,)


@pytest.mark.parametrize("mutation", [
    "UPDATE planner_state SET planner_revision=planner_revision+1",
    "UPDATE files SET size_bytes=13 WHERE rfilename='.gitignore'",
    "UPDATE files SET sha256='changed' WHERE rfilename='weights.gguf'",
    "INSERT INTO files(repo_id,rfilename,format) VALUES('org/model','extra.json','aux')",
])
def test_stale_preview_rolls_back_without_reclassifying(con, mutation):
    preview = manifest.preview_metadata_classification(con, ["org/model"])
    con.execute(mutation)
    before = list(con.iterdump())
    with pytest.raises(proposal.Refusal) as error:
        manifest.apply_metadata_classification(con, preview)
    assert error.value.code == "METADATA_CLASSIFICATION_PREVIEW_STALE"
    assert list(con.iterdump()) == before


def test_forged_preview_cannot_broaden_selected_files(con):
    preview = manifest.preview_metadata_classification(con, ["org/model"])
    bad = replace(preview, changed_files=preview.changed_files + (("org/model", ".secret"),))
    with pytest.raises(proposal.Refusal, match="METADATA_CLASSIFICATION_PREVIEW_STALE"):
        manifest.apply_metadata_classification(con, bad)


def test_live_fill_authority_blocks_catalog_correction(con):
    preview = manifest.preview_metadata_classification(con, ["org/model"])
    before = list(con.iterdump())
    with mock.patch("modelark.execution_session.live_session_exists", return_value=True), \
            mock.patch("modelark.execution_session.live_owner", return_value={}), \
            pytest.raises(proposal.Refusal, match="FILL_SESSION_ACTIVE"):
        manifest.apply_metadata_classification(con, preview)
    assert list(con.iterdump()) == before


def test_failed_revision_commit_rolls_back_classification(con):
    preview = manifest.preview_metadata_classification(con, ["org/model"])
    before = list(con.iterdump())
    with mock.patch.object(proposal, "bump_revision", side_effect=RuntimeError("bump failed")), \
            pytest.raises(RuntimeError, match="bump failed"):
        manifest.apply_metadata_classification(con, preview)
    assert list(con.iterdump()) == before


def test_changed_approval_is_superseded_without_rewriting_frozen_files(con):
    con.execute("INSERT INTO placement_proposals(proposal_id,plan_id,based_on_revision,lifecycle,"
                "canonical_hash,mutation_kind,serializer_version) "
                "VALUES('p','ark',0,'approved',?,'adopt_current','1')", ("a" * 64,))
    con.execute("INSERT INTO proposal_tasks(proposal_id,requirement_id,row_kind,repo_id,"
                "full_manifest_hash) VALUES('p','r','executable','org/model',?)", ("b" * 64,))
    con.execute("INSERT INTO proposal_files(proposal_id,requirement_id,rfilename) "
                "VALUES('p','r','weights.gguf')")
    con.execute("UPDATE planner_state SET active_approved_proposal_id='p'")
    tasks = con.execute("SELECT * FROM proposal_tasks").fetchall()
    files = con.execute("SELECT * FROM proposal_files").fetchall()
    manifest.apply_metadata_classification(con,
                                          manifest.preview_metadata_classification(con, ["org/model"]))
    assert con.execute("SELECT lifecycle FROM placement_proposals").fetchone() == ("superseded",)
    assert con.execute("SELECT active_approved_proposal_id FROM planner_state").fetchone() == (None,)
    assert con.execute("SELECT * FROM proposal_tasks").fetchall() == tasks
    assert con.execute("SELECT * FROM proposal_files").fetchall() == files
