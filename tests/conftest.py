"""Suite-wide hermeticity boundaries — test-only, never production behavior.

Unit tests must never read the operator's real ``./configuration.toml``:
that file is git-ignored, machine-specific, and may describe production
(which the fail-closed Settings validators rightly reject under test
defaults). Several modules also call ``get_settings()`` at import time
(``db/session.py``, ``worker/settings.py``), so the isolation has to apply
before any test module is imported — not just inside fixtures.

What this does:

* at import time (before collection imports test modules), default the
  ``CLOUD_PLATFORM_CONFIG_FILE`` bootstrap variable to an empty temporary
  TOML file — unless the operator already pointed it somewhere explicit
  (``setdefault`` never overrides an explicit choice);
* reset the process-wide settings cache around every test.

An empty TOML file maps to zero overrides, which is exactly equivalent to
"no configuration file" for every test that does not pass an explicit
path. Tests that need a real file keep full control through
``monkeypatch.setenv`` / explicit paths / ``chdir`` (see
``test_config_toml.py``); production loading behavior is untouched.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from cloud_platform.core.config import CONFIG_FILE_ENV, reset_settings_cache


def _empty_config_file() -> str:
    """Create one empty TOML file for the whole pytest process."""
    fd, path = tempfile.mkstemp(prefix="hermetic-test-config-", suffix=".toml")
    os.close(fd)
    Path(path).write_text("", encoding="utf-8")
    return path


_EMPTY_CONFIG_FILE = _empty_config_file()

# See module docstring: default-only, applied before collection imports.
os.environ.setdefault(CONFIG_FILE_ENV, _EMPTY_CONFIG_FILE)
reset_settings_cache()


@pytest.fixture(autouse=True)
def _reset_settings_cache() -> Iterator[None]:
    """The settings singleton never leaks between tests."""
    reset_settings_cache()
    yield
    reset_settings_cache()
