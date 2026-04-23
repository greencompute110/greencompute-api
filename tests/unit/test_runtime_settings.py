from __future__ import annotations

from greencompute_persistence.runtime import load_runtime_settings


def test_runtime_settings_default_builder_execution_mode_is_live(monkeypatch) -> None:
    monkeypatch.delenv("GREENCOMPUTE_BUILD_EXECUTION_MODE", raising=False)

    settings = load_runtime_settings("greencompute-builder")

    assert settings.build_execution_mode == "live"


def test_runtime_settings_respects_explicit_builder_execution_mode(monkeypatch) -> None:
    monkeypatch.setenv("GREENCOMPUTE_BUILD_EXECUTION_MODE", "simulated")

    settings = load_runtime_settings("greencompute-builder")

    assert settings.build_execution_mode == "simulated"
