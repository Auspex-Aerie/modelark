"""INC-042 contracts: provenance publication preserves the git-annex map locator."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from modelark.core import db
from test_dec053_054_gate1_contracts import _seed_frozen_v6


_KW = {"confirm_stopped": "MODELARK-STOPPED", "writers_stopped": True}


def _rehearse(tmp_path: Path, payload: bytes | None = None):
    data = _seed_frozen_v6(tmp_path / "src")
    if payload is not None:
        (data / "library.json").write_bytes(payload)
    work = tmp_path / "work"
    work.mkdir()
    report = db.rehearse_provenance_migration(data, work, run_id="inc042")
    return data, work, report


def test_c01_rehearsal_and_publish_preserve_exact_library_locator(tmp_path):
    payload = json.dumps(
        {"library_root": "/srv/modelark/custom-map"},
        indent=2,
    ).encode() + b"\n"
    data, work, report = _rehearse(tmp_path, payload)

    companion = report["runtime_companions"]["library.json"]
    assert companion == {
        "present": True,
        "size": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }
    assert (data / "library.json").read_bytes() == payload

    dest = tmp_path / "dest"
    result = db.publish_provenance_migration(work, dest, **_KW)
    assert result["status"] == "ok"
    assert (dest / "catalog.sqlite").is_file()
    assert (dest / "library.json").read_bytes() == payload
    assert result["published_companions"]["library.json"] == companion


def test_c02_absent_library_locator_remains_absent(tmp_path):
    _data, work, report = _rehearse(tmp_path)
    assert report["runtime_companions"]["library.json"] == {
        "present": False,
        "size": None,
        "sha256": None,
    }

    dest = tmp_path / "dest"
    result = db.publish_provenance_migration(work, dest, **_KW)
    assert result["status"] == "ok"
    assert (dest / "catalog.sqlite").is_file()
    assert not (dest / "library.json").exists()
    assert result["published_companions"]["library.json"] == report[
        "runtime_companions"
    ]["library.json"]


@pytest.mark.parametrize(
    "payload",
    (
        b"not-json\n",
        b"[]\n",
        b'{"library_root": ""}\n',
        b'{"library_root": 7}\n',
    ),
)
def test_c03_malformed_library_locator_refuses_rehearsal(tmp_path, payload):
    data = _seed_frozen_v6(tmp_path / "src")
    locator = data / "library.json"
    locator.write_bytes(payload)
    before = locator.read_bytes()
    work = tmp_path / "work"

    with pytest.raises(RuntimeError, match="library.json"):
        db.rehearse_provenance_migration(data, work, run_id="malformed")

    assert locator.read_bytes() == before
    assert not (work / "malformed" / "report.json").exists()


def test_c04_library_locator_drift_after_rehearsal_refuses_publication(tmp_path):
    original = b'{"library_root":"/srv/modelark/map-a"}\n'
    data, work, _report = _rehearse(tmp_path, original)
    changed = b'{"library_root":"/srv/modelark/map-b"}\n'
    (data / "library.json").write_bytes(changed)
    dest = tmp_path / "dest"

    with pytest.raises(RuntimeError, match="library.json.*changed|changed.*library.json"):
        db.publish_provenance_migration(work, dest, **_KW)

    assert (data / "library.json").read_bytes() == changed
    assert not (dest / "catalog.sqlite").exists()
    assert not (dest / "library.json").exists()


def test_c05_destination_library_locator_occupancy_is_preserved_and_refused(tmp_path):
    payload = b'{"library_root":"/srv/modelark/intended"}\n'
    _data, work, _report = _rehearse(tmp_path, payload)
    dest = tmp_path / "dest"
    dest.mkdir()
    sentinel = b'{"library_root":"/srv/modelark/foreign"}\n'
    (dest / "library.json").write_bytes(sentinel)

    with pytest.raises(RuntimeError, match="library.json.*exists|exists.*library.json"):
        db.publish_provenance_migration(work, dest, **_KW)

    assert (dest / "library.json").read_bytes() == sentinel
    assert not (dest / "catalog.sqlite").exists()
    assert not (dest / ".catalog.sqlite.publish-staging").exists()


def test_c06_library_locator_symlink_refuses_rehearsal(tmp_path):
    data = _seed_frozen_v6(tmp_path / "src")
    outside = tmp_path / "outside.json"
    outside.write_text('{"library_root":"/srv/modelark/outside"}\n')
    (data / "library.json").symlink_to(outside)

    with pytest.raises(RuntimeError, match="library.json.*regular|regular.*library.json"):
        db.rehearse_provenance_migration(data, tmp_path / "work", run_id="symlink")

    assert outside.read_text() == '{"library_root":"/srv/modelark/outside"}\n'
