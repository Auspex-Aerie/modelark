"""Stage E: public CLI entry point through real catalog, codecs and folder delivery.

Commands call cli.main(argv), not mocked dispatch or a separate installed launcher.
Only host/provider inputs and a unique launch-lock address are synthetic. Real
SQLite, source descriptors/fences, codec children, destination I/O, journals and
receipts run over disposable trees. This is NOT physical USB qualification.
"""
# ruff: noqa: F811 -- imported pytest fixtures deliberately name test parameters
import hashlib
import json
import os
import uuid

import pytest

from modelark import artifact_preflight, cli, codec_supervisor, compress, instance
from modelark.slice import state
from test_serial_repair_public_slice import (
    NAME, REPO, hardware, native, workflow,  # noqa: F401
)


@pytest.fixture
def public(workflow, monkeypatch, capsys):
    case = workflow
    monkeypatch.setattr(instance, "_ADDRESS", "\0modelark-stage-e-" + uuid.uuid4().hex)
    # Deterministic admission sample, not a claim about the host's free memory.
    # The real child still installs and reports the actual qualified AS guard.
    monkeypatch.setattr(artifact_preflight, "available_memory", lambda: {"available_bytes": 16 << 30})
    monkeypatch.setattr(codec_supervisor, "available_memory", lambda: {"available_bytes": 16 << 30})

    def command(*argv, exit_code=0):
        capsys.readouterr()
        code = 0
        try:
            cli.main(["slice", *(str(arg) for arg in argv)])
        except SystemExit as exc:
            code = exc.code
        captured = capsys.readouterr()
        assert code == exit_code, (argv, code, captured)
        assert captured.err == ""
        result = json.loads(captured.out)
        assert isinstance(result, dict)
        assert result.get("ok") is (exit_code == 0), result
        return result

    case.command = command
    case.destination = case.parent / "delivery"
    return case


def prepare(case, codec, dtype="bfloat16"):
    """Real writer fixtures, original identities recorded separately from storage."""
    raw = b"\x00\x00\x80\x3f" * 100000
    case.source.write_bytes(raw)
    stored = case.source
    if codec != compress.CODEC_RAW:
        if codec == compress.CODEC_ZSTD:
            zstd = pytest.importorskip("zstandard")
            assert zstd.__version__ == "0.25.0", "Slice qualified zstd version"
        stored = case.source.with_name(NAME + ".znn")
        compress.compress_file(case.source, stored, codec=codec, dtype=dtype, threads=1)
    digest = hashlib.sha256(raw).hexdigest()
    case.con.execute("UPDATE files SET size_bytes=?,sha256=?,quant=?", [len(raw), digest, dtype])
    case.con.execute("UPDATE archived SET stored_relpath=?,stored_bytes=?,orig_bytes=?,"
                     "orig_sha256=?,compressed=?",
                     [stored.name, stored.stat().st_size, len(raw), digest, codec != compress.CODEC_RAW])
    # Include a raw sidecar in the same selection: the delivered tree is a model
    # closure, not a codec-specific renamed export.
    config = b'{"synthetic_qualification":true}\n'
    (case.source.parent / "config.json").write_bytes(config)
    config_hash = hashlib.sha256(config).hexdigest()
    case.con.execute("INSERT INTO files(repo_id,rfilename,size_bytes,sha256,format) VALUES(?,?,?,?,?)",
                     [REPO, "config.json", len(config), config_hash, "aux"])
    case.con.execute("INSERT INTO archived(repo_id,rfilename,drive_label,stored_relpath,orig_bytes,"
                     "stored_bytes,orig_sha256,orig_sha256_provenance,compressed) "
                     "VALUES(?,?,'drive-00',?,?,?,?, 'ingestion_computed',0)",
                     [REPO, "config.json", "config.json", len(config), len(config), config_hash])
    case.expected = {NAME: raw, "config.json": config}
    case.stored = stored
    return case


def inventory(root):
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob("*") if path.is_file()}


def approve(case):
    preview = case.command("preview", "--catalog", case.path, "--destination", case.destination,
                           "--repo", REPO)
    assert preview["state"] == "ready"
    assert preview["ownership"] == "new-output-folder-only"
    assert not case.destination.exists()
    case.tx = preview["transaction_id"]
    case.command("approve", case.tx, "--seal", preview["seal"])
    assert not case.destination.exists()
    return preview


def start(case, **kwargs):
    return case.command("start", case.tx, "--destination", case.destination,
                        "--source", f"drive-00={case.archive}", **kwargs)


@pytest.mark.parametrize("workflow", ["canonical"], indirect=True)
@pytest.mark.parametrize("codec,dtype", [
    (compress.CODEC_RAW, "bfloat16"),
    *[(codec, dtype) for codec in (compress.CODEC_WHOLE, compress.CODEC_STREAM)
      for dtype in ("bfloat16", "float16", "float32")],
    (compress.CODEC_ZSTD, "bfloat16"),
])
def test_cli_complete_original_tree_receipt_and_repeat_start(public, codec, dtype, monkeypatch):
    case = prepare(public, codec, dtype)
    before_archive = inventory(case.archive)
    before_catalog = tuple(case.con.iterdump())
    launched = []
    real_launch = codec_supervisor._launch

    def launch(*args):
        child = real_launch(*args)
        launched.append(child)
        return child

    monkeypatch.setattr(codec_supervisor, "_launch", launch)
    preview = approve(case)
    assert start(case)["state"] == "complete"
    assert case.command("status", case.tx)["state"] == "complete"
    receipt_path = case.destination / ".modelark-slice-receipt.json"
    receipt_bytes = receipt_path.read_bytes()
    receipt = json.loads(receipt_bytes)
    assert receipt["transaction"] == case.tx and receipt["seal"] == preview["seal"]
    assert receipt["status"] == "complete"
    assert len(receipt["files"]) == len(case.expected)
    by_path = {item["path"]: item for item in receipt["files"]}
    for name, original in case.expected.items():
        output = case.destination / REPO / name
        assert output.read_bytes() == original
        entry = by_path[f"delivery/{REPO}/{name}"]
        assert entry["size"] == len(original)
        assert entry["sha256"] == hashlib.sha256(original).hexdigest()
    assert set(inventory(case.destination / REPO)) == set(case.expected)
    assert not tuple(case.destination.rglob(".slice-*"))
    assert (case.parent / "unrelated").read_bytes() == b"unrelated sibling"
    assert not (case.parent / ".modelark-slice-owner").exists()
    # Completed Start neither reopens a missing source nor rewrites its receipt.
    absent = case.parent / "unattached"
    assert case.command("start", case.tx, "--destination", absent)["state"] == "complete"
    assert not absent.exists()
    assert receipt_path.read_bytes() == receipt_bytes
    assert inventory(case.archive) == before_archive
    assert tuple(case.con.iterdump()) == before_catalog
    assert bool(launched) is (codec in (compress.CODEC_WHOLE, compress.CODEC_STREAM))
    for child in launched:
        assert child.poll() == 0
        with pytest.raises(ChildProcessError):
            os.waitpid(child.pid, os.WNOHANG)


@pytest.mark.parametrize("workflow", ["canonical"], indirect=True)
@pytest.mark.parametrize("fault", ["missing_attachment", "resource", "truncated", "trailing",
                                    "digest", "worker_crash", "wrong_destination"])
def test_cli_failure_never_claims_complete_or_changes_archive(public, monkeypatch, fault):
    case = prepare(public, compress.CODEC_WHOLE)
    if fault == "truncated":
        case.stored.write_bytes(case.stored.read_bytes()[:-1])
        case.con.execute("UPDATE archived SET stored_bytes=? WHERE rfilename=?",
                         [case.stored.stat().st_size, NAME])
    elif fault == "trailing":
        case.stored.write_bytes(case.stored.read_bytes() + b"extra")
        case.con.execute("UPDATE archived SET stored_bytes=? WHERE rfilename=?",
                         [case.stored.stat().st_size, NAME])
    elif fault == "digest":
        case.con.execute("UPDATE files SET sha256=? WHERE rfilename=?", ["0" * 64, NAME])
        case.con.execute("UPDATE archived SET orig_sha256=? WHERE rfilename=?", ["0" * 64, NAME])
    before_archive, before_catalog = inventory(case.archive), tuple(case.con.iterdump())
    approve(case)
    children = []
    if fault == "resource":
        monkeypatch.setattr(artifact_preflight, "available_memory", lambda: {"available_bytes": 0})
        monkeypatch.setattr(codec_supervisor, "_launch", lambda *a: pytest.fail("launch after refusal"))
    elif fault == "worker_crash":
        real_launch = codec_supervisor._launch

        def killed(*args):
            child = real_launch(*args)
            children.append(child)
            child.kill()
            return child

        monkeypatch.setattr(codec_supervisor, "_launch", killed)
    if fault == "missing_attachment":
        result = case.command("start", case.tx, "--destination", case.destination, exit_code=1)
    elif fault == "wrong_destination":
        result = case.command("start", case.tx, "--destination", case.parent / "other", exit_code=1)
        assert result["code"] == "DESTINATION_CHANGED"
    else:
        result = start(case, exit_code=1)
    assert result.get("state") != "complete"
    expected_code = {
        "missing_attachment": "WAITING_SOURCE", "resource": "SOURCE_DECODE_RESOURCE",
        "truncated": "SOURCE_DECODE_INVALID", "trailing": "SOURCE_DECODE_INVALID",
        "digest": "SOURCE_DIGEST_MISMATCH", "worker_crash": "SOURCE_DECODE_WORKER_FAILED",
        "wrong_destination": "DESTINATION_CHANGED",
    }[fault]
    if fault == "wrong_destination":
        assert result["code"] == expected_code
    else:
        details = json.loads(result["reason"].partition(": ")[2])
        assert [candidate["code"] for candidate in details["candidates"]] == [expected_code]
    assert state.Store().status(case.tx).state != "complete"
    assert not (case.destination / ".modelark-slice-receipt.json").exists()
    assert not (case.destination / REPO / NAME).exists()
    if fault in {"missing_attachment", "resource", "truncated", "trailing", "wrong_destination"}:
        assert not case.destination.exists()
        assert state.Store().events(case.tx) == []
    assert inventory(case.archive) == before_archive
    assert tuple(case.con.iterdump()) == before_catalog
    assert (case.parent / "unrelated").read_bytes() == b"unrelated sibling"
    assert not (case.parent / "other").exists()
    if fault == "worker_crash":
        assert len(children) == 1, "must reach the injected real-child failure"
    for child in children:
        assert child.poll() is not None
        with pytest.raises(ChildProcessError):
            os.waitpid(child.pid, os.WNOHANG)
