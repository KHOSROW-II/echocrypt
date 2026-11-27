"""Import smoke coverage for all Python application surfaces outside the core suite."""
from __future__ import annotations

import importlib

import pytest


@pytest.mark.parametrize("module_name", [
    "echocrypt",
    "interface.audio_tests",
    "interface.chat",
    "interface.communication",
    "interface.dashboard",
    "interface.menu",
    "interface.settings",
    "interface.terminal",
    "interface.theme",
    "browser_extension.native_host",
    "scripts.build_browser_extensions",
    "scripts.generate_changelog",
    "scripts.package_release",
    "scripts.register_browser_host",
])
def test_application_surface_module_imports_cleanly(module_name: str) -> None:
    assert importlib.import_module(module_name) is not None
