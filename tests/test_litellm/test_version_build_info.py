"""
Tests for litellm._version build-time identity injection.

The internal fork ships images tagged like ``v1.83.10-internal.5``. To surface
that tag in the UI / logs without mutating ``pyproject.toml`` on every release
(which would conflict with every upstream sync), the Docker build injects
``LITELLM_BUILD_TAG`` and ``LITELLM_BUILD_SHA`` env vars that ``_version.py``
prefers over ``importlib.metadata``.
"""

import importlib
import os
import sys

import pytest


@pytest.fixture
def reload_version_module(monkeypatch):
    """
    Re-import litellm._version cleanly with the current env, returning the
    freshly-loaded module. Restores the original module on teardown so other
    tests that import `version` keep seeing the real value.
    """
    original = sys.modules.get("litellm._version")
    sys.modules.pop("litellm._version", None)

    def _load():
        return importlib.import_module("litellm._version")

    yield _load

    sys.modules.pop("litellm._version", None)
    if original is not None:
        sys.modules["litellm._version"] = original


def test_build_tag_overrides_package_version(monkeypatch, reload_version_module):
    monkeypatch.setenv("LITELLM_BUILD_TAG", "v1.83.10-internal.5")
    monkeypatch.setenv("LITELLM_BUILD_SHA", "abc1234")

    mod = reload_version_module()

    assert mod.version == "v1.83.10-internal.5"
    assert mod.build_sha == "abc1234"
    # Base version still reflects upstream package metadata, not the override.
    assert mod.build_base_version != "v1.83.10-internal.5"
    assert mod.build_base_version  # non-empty


def test_falls_back_to_package_metadata_without_env(monkeypatch, reload_version_module):
    monkeypatch.delenv("LITELLM_BUILD_TAG", raising=False)
    monkeypatch.delenv("LITELLM_BUILD_SHA", raising=False)

    mod = reload_version_module()

    assert mod.version == mod.build_base_version
    assert mod.build_sha == ""


def test_empty_env_treated_as_unset(monkeypatch, reload_version_module):
    # CI passes through env vars as empty strings when the source value is
    # missing — ``LITELLM_BUILD_TAG=`` must NOT override the package version.
    monkeypatch.setenv("LITELLM_BUILD_TAG", "")
    monkeypatch.setenv("LITELLM_BUILD_SHA", "   ")

    mod = reload_version_module()

    assert mod.version == mod.build_base_version
    assert mod.build_sha == ""
