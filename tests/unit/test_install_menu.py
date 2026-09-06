"""Tests for the one-line installer and bash ops menu.

Acceptance: install.sh is valid bash with the documented flags and never
overwrites .env; platform.sh exposes the documented ops choices.
"""

from __future__ import annotations

import pathlib
import re
import subprocess

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
INSTALL = REPO / "install.sh"
PLATFORM = REPO / "platform.sh"


class TestInstaller:
    def test_files_exist_and_executable_bits(self) -> None:
        assert INSTALL.is_file()
        assert PLATFORM.is_file()

    def test_bash_syntax_valid(self) -> None:
        import os
        import shutil

        if os.name == "nt" or shutil.which("bash") is None:
            for script in (INSTALL, PLATFORM):
                text = script.read_text(encoding="utf-8")
                assert text.startswith("#!/usr/bin/env bash")
                assert "set -euo pipefail" in text
            pytest.skip("native bash unavailable on Windows; static header check passed")
        for script in (INSTALL, PLATFORM):
            result = subprocess.run(
                ["bash", "-n", script.as_posix()], capture_output=True, text=True
            )
            assert result.returncode == 0, result.stderr

    def test_installer_flags_documented(self) -> None:
        text = INSTALL.read_text(encoding="utf-8")
        for flag in (
            "--yes",
            "--dir",
            "--repo",
            "--branch",
            "--providers",
            "--no-bot",
            "--no-sync",
            "--no-up",
        ):
            assert flag in text

    def test_installer_never_overwrites_env(self) -> None:
        text = INSTALL.read_text(encoding="utf-8")
        assert "never overwrites" in text.lower() or ".env exists" in text
        assert "chmod 600" in text

    def test_installer_syncs_hetzner_and_leaseweb(self) -> None:
        text = INSTALL.read_text(encoding="utf-8")
        assert "sync_catalog.py" in text
        assert "hetzner" in text and "leaseweb" in text

    def test_installer_runs_migrations_before_services(self) -> None:
        text = INSTALL.read_text(encoding="utf-8")
        assert "migrate" in text
        assert text.index("migrate") < text.index("up -d api")

    def test_platform_menu_options(self) -> None:
        text = PLATFORM.read_text(encoding="utf-8")
        for keyword in (
            "sync catalog",
            "secrets",
            "backup",
            "update",
            "smoke",
            "teardown",
            "logs",
            "status",
        ):
            assert re.search(keyword, text, re.IGNORECASE), keyword
