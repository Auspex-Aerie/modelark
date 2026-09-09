"""Sealed FAT plans route only to their dedicated public assembly."""
from types import SimpleNamespace

import pytest

from modelark.slice import fat32_operator, operator, transaction as t


@pytest.mark.parametrize("state", ["approved", "starting", "stopped", "waiting_destination"])
def test_public_fat_start_routes_by_sealed_plan(monkeypatch, state):
    plan = SimpleNamespace(session_only=True, is_folder=True)
    store = SimpleNamespace(load=lambda tx: plan, status=lambda tx: t.Status(tx, state))
    monkeypatch.setattr(operator, "_store", lambda: store)
    calls = []
    def start(*args):
        calls.append(args)
        return {"ok": True, "state": "observed"}
    monkeypatch.setattr(fat32_operator, "start", start)
    assert operator.start("tx", "/unused/not-opened", {}) == {"ok": True, "state": "observed"}
    assert calls == [(store, "tx", plan, "/unused/not-opened", {})]
