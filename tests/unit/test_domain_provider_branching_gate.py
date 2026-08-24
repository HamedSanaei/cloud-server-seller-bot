"""Tests for the zero-provider-branching gate (M15-007).

Acceptance: no ``if provider == ...`` in domain modules. The gate
``scripts/check_domain_provider_branching.py`` must pass on the real
codebase AND must catch violations when they are introduced.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "check_domain_provider_branching.py"


@pytest.fixture(scope="module")
def gate():
    spec = importlib.util.spec_from_file_location("check_domain_provider_branching", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_domain_provider_branching"] = module
    spec.loader.exec_module(module)
    return module


class TestGateOnRealCodebase:
    def test_no_provider_branching_in_any_domain_module(self, gate) -> None:
        """The acceptance criterion itself, run on the real source tree."""
        violations = gate.check_domain_root()
        assert violations == [], [v.render() for v in violations]

    def test_domain_root_is_the_modules_package(self, gate) -> None:
        assert gate.DOMAIN_ROOT.name == "modules"
        assert gate.DOMAIN_ROOT.parent.name == "cloud_platform"
        files = list(gate.DOMAIN_ROOT.rglob("*.py"))
        assert len(files) > 40  # sanity: we are scanning the real domain

    def test_provider_keys_are_the_known_set(self, gate) -> None:
        assert "hetzner" in gate.PROVIDER_KEYS
        assert "arvancloud" in gate.PROVIDER_KEYS


class TestGateCatchesViolations:
    def test_provider_comparison_in_domain_is_flagged(self, gate, tmp_path: Path) -> None:
        bad = tmp_path / "module_a.py"
        bad.write_text(
            "def pick(provider_key: str) -> str:\n"
            "    if provider_key == 'hetzner':\n"
            "        return 'a'\n"
            "    return 'b'\n",
            encoding="utf-8",
        )
        violations = gate.check_domain_module(bad)
        assert len(violations) == 1
        assert "hetzner" in violations[0].message

    def test_arvancloud_comparison_is_flagged(self, gate, tmp_path: Path) -> None:
        bad = tmp_path / "module_b.py"
        bad.write_text(
            "def pick(provider_key: str) -> bool:\n    return provider_key != 'arvancloud'\n",
            encoding="utf-8",
        )
        violations = gate.check_domain_module(bad)
        assert len(violations) == 1
        assert "arvancloud" in violations[0].message

    def test_registry_get_lookup_is_flagged(self, gate, tmp_path: Path) -> None:
        bad = tmp_path / "module_c.py"
        bad.write_text(
            "def resolve(registry):\n    return registry.get('hetzner')\n",
            encoding="utf-8",
        )
        violations = gate.check_domain_module(bad)
        assert len(violations) == 1

    def test_adapter_import_is_flagged(self, gate, tmp_path: Path) -> None:
        bad = tmp_path / "module_d.py"
        bad.write_text(
            "from cloud_platform.providers.hetzner.client import HetznerCloudProvider\n"
            "from cloud_platform.providers.arvancloud.client import ArvanCloudProvider\n",
            encoding="utf-8",
        )
        violations = gate.check_domain_module(bad)
        assert len(violations) == 2
        messages = " ".join(v.message for v in violations)
        assert "hetzner" in messages and "arvancloud" in messages

    def test_port_imports_are_allowed(self, gate, tmp_path: Path) -> None:
        good = tmp_path / "module_e.py"
        good.write_text(
            "from cloud_platform.providers.base import CloudProvider, Capability\n"
            "from cloud_platform.providers.registry import ProviderRegistry\n"
            "from cloud_platform.providers.errors import ProviderError\n"
            "from cloud_platform.providers.retry import RetryExecutor\n"
            "from cloud_platform.providers.credentials import CredentialHolder\n"
            "\n"
            "def x(provider: CloudProvider) -> Capability:\n"
            "    return Capability.COMPUTE\n",
            encoding="utf-8",
        )
        assert gate.check_domain_module(good) == []

    def test_provider_key_as_setting_attribute_name_is_not_flagged(
        self, gate, tmp_path: Path
    ) -> None:
        """``settings.hetzner_api_token`` uses the name as an attribute, not a
        compared value - that is configuration plumbing, not branching."""
        good = tmp_path / "module_f.py"
        good.write_text(
            "def token(settings) -> str:\n    return settings.hetzner_api_token\n",
            encoding="utf-8",
        )
        assert gate.check_domain_module(good) == []

    def test_in_comparison_is_flagged(self, gate, tmp_path: Path) -> None:
        bad = tmp_path / "module_g.py"
        bad.write_text(
            "def check(provider_key: str) -> bool:\n"
            "    return provider_key in ('hetzner', 'arvancloud')\n",
            encoding="utf-8",
        )
        violations = gate.check_domain_module(bad)
        # both keys are in a Compare context
        assert len(violations) == 2


class TestMainExitCodes:
    def test_main_returns_zero_on_clean_tree(self, gate, capsys: pytest.CaptureFixture) -> None:
        code = gate.main()
        out = capsys.readouterr().out
        assert code == 0
        assert "passed" in out

    def test_main_returns_one_on_violation(self, gate, tmp_path: Path, monkeypatch) -> None:
        bad = tmp_path / "modules" / "bad.py"
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.write_text(
            "def x(p: str) -> bool:\n    return p == 'hetzner'\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(gate, "DOMAIN_ROOT", tmp_path / "modules")
        code = gate.main()
        assert code == 1
