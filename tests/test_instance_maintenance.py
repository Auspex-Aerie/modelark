"""Offline maintenance entrypoints share launch exclusion; never open a real catalog."""
import importlib
import uuid

import pytest


@pytest.mark.parametrize("name", ["scripts.migrate_provenance", "scripts.migrate_legacy_runtime"])
def test_migration_refuses_before_parsing_or_accessing_catalog(name, monkeypatch):
    from modelark import instance
    module = importlib.import_module(name)
    monkeypatch.setattr(instance, "_ADDRESS", "\0modelark-test-maintenance-" + uuid.uuid4().hex)
    monkeypatch.setattr(module, "_main", lambda *a: pytest.fail("migration startup side effect"))
    with instance.launch(), pytest.raises(SystemExit, match="ModelArk is already running"):
        module.main([])


@pytest.mark.parametrize("name", ["scripts.migrate_provenance", "scripts.migrate_legacy_runtime"])
def test_migration_releases_launch_guard_after_completion(name, monkeypatch):
    from modelark import instance
    module = importlib.import_module(name)
    monkeypatch.setattr(instance, "_ADDRESS", "\0modelark-test-maintenance-" + uuid.uuid4().hex)
    monkeypatch.setattr(module, "_main", lambda *a: 7)
    assert module.main([]) == 7
    with instance.launch():
        pass
