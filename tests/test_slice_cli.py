"""Operator CLI dispatch only: mocked operator, disposable launch socket, no devices/state."""
import json
import sys
from types import SimpleNamespace
import uuid

import pytest


@pytest.fixture
def operator(monkeypatch):
    from modelark import instance
    from modelark import cli
    calls = []
    fake = SimpleNamespace()
    for name in ("preview", "approve", "start", "status", "stop"):
        def call(*args, _name=name):
            calls.append((_name, args))
            return {"z": 1, "ok": True, "operation": _name}
        setattr(fake, name, call)
    monkeypatch.setitem(sys.modules, "modelark.slice.operator", fake)
    monkeypatch.setattr(instance, "_ADDRESS", "\0modelark-slice-cli-test-" + uuid.uuid4().hex)
    monkeypatch.setattr(cli.db, "configure", lambda *a: pytest.fail("default db configuration"))
    monkeypatch.setattr(cli.db, "connect", lambda *a, **k: pytest.fail("default catalog access"))
    monkeypatch.setattr(cli.wishlist, "configure", lambda *a: pytest.fail("global configuration"))
    return fake, calls


@pytest.mark.parametrize("argv,name,expected", [
    (["preview", "--catalog", "/explicit/catalog.sqlite", "--destination", "/exports/delivery",
      "--repo", "org/a"], "preview",
     ("/explicit/catalog.sqlite", "/exports/delivery", ("org/a",), None)),
    (["preview", "--catalog", "/explicit/catalog.sqlite", "--destination", "/usb",
      "--repo", "org/a", "--repo", "org/b", "--root", "delivery"], "preview",
     ("/explicit/catalog.sqlite", "/usb", ("org/a", "org/b"), "delivery")),
    (["approve", "tx", "--seal", "sealed"], "approve", ("tx", "sealed")),
    (["start", "tx", "--destination", "/usb", "--source", "drive-a=/source/modelark",
      "--source", "drive-b=/second/modelark"], "start",
     ("tx", "/usb", {"drive-a": "/source/modelark", "drive-b": "/second/modelark"})),
    (["status", "tx"], "status", ("tx",)),
    (["stop", "tx"], "stop", ("tx",)),
])
def test_exact_operator_dispatch(operator, capsys, argv, name, expected):
    from modelark import cli
    _, calls = operator
    cli.main(["slice", *argv])
    assert calls == [(name, expected)]
    assert capsys.readouterr().out == json.dumps(
        {"z": 1, "ok": True, "operation": name}, sort_keys=True) + "\n"


@pytest.mark.parametrize("source", ["missing-equals", "=path", "label=", " label=/path",
                                    "label =/path", "../label=/path"])
def test_malformed_attachment_never_dispatches(operator, source):
    from modelark import cli
    _, calls = operator
    with pytest.raises(SystemExit) as caught:
        cli.main(["slice", "start", "tx", "--destination", "/usb", "--source", source])
    assert caught.value.code != 0
    assert calls == []


def test_duplicate_source_label_never_dispatches(operator):
    from modelark import cli
    _, calls = operator
    with pytest.raises(SystemExit) as caught:
        cli.main(["slice", "start", "tx", "--destination", "/usb",
                  "--source", "a=/first", "--source", "a=/second"])
    assert caught.value.code != 0
    assert calls == []


@pytest.mark.parametrize("option", ["--data-dir", "--state-dir", "--config"])
def test_slice_rejects_global_runtime_overrides(operator, option):
    from modelark import cli
    _, calls = operator
    with pytest.raises(SystemExit) as caught:
        cli.main([option, "/unused", "slice", "status", "tx"])
    assert caught.value.code != 0
    assert calls == []


@pytest.mark.parametrize("kind", ["transaction", "domain"])
def test_typed_refusal_is_json_and_nonzero(operator, capsys, kind):
    from modelark import cli
    from modelark.slice.domain import SliceRefusal
    from modelark.slice.transaction import TransferRefusal
    fake, _ = operator

    def refuse(tx):
        refusal = TransferRefusal if kind == "transaction" else SliceRefusal
        raise refusal("APPROVAL_MISSING", "explicit approval required")

    fake.status = refuse
    with pytest.raises(SystemExit) as caught:
        cli.main(["slice", "status", "tx"])
    assert caught.value.code == 1
    assert json.loads(capsys.readouterr().out) == {
        "ok": False, "code": "APPROVAL_MISSING", "detail": "explicit approval required"}


def test_slice_help_does_not_import_operator(operator, monkeypatch, capsys):
    from modelark import cli
    monkeypatch.delitem(sys.modules, "modelark.slice.operator")
    with pytest.raises(SystemExit) as caught:
        cli.main(["slice", "--help"])
    assert caught.value.code == 0
    assert "modelark.slice.operator" not in sys.modules
    assert "preview" in capsys.readouterr().out


def test_no_sources_does_not_invent_attachment(operator):
    from modelark import cli
    _, calls = operator
    cli.main(["slice", "start", "tx", "--destination", "/usb"])
    assert calls == [("start", ("tx", "/usb", {}))]


def test_operator_refusal_result_is_nonzero(operator, capsys):
    from modelark import cli
    fake, _ = operator
    fake.start = lambda *args: {"ok": False, "state": "waiting_source"}
    with pytest.raises(SystemExit) as caught:
        cli.main(["slice", "start", "tx", "--destination", "/usb"])
    assert caught.value.code == 1
    assert json.loads(capsys.readouterr().out) == {"ok": False, "state": "waiting_source"}


def test_surrogate_escaped_root_returns_json_layout_refusal(operator, capsys):
    import os
    from modelark import cli
    from modelark.slice.capacity import layout
    fake, _ = operator
    fake.preview = lambda catalog, destination, repos, root: layout([root + '/org/model/file'])
    with pytest.raises(SystemExit) as caught:
        cli.main(['slice', 'preview', '--catalog', '/unused', '--destination', '/usb',
                  '--repo', 'org/model', '--root', os.fsdecode(b'delivery-\xff')])
    assert caught.value.code == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out)['code'] == 'DESTINATION_LAYOUT_UNSUPPORTED'
    assert captured.err == ''
