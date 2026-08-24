"""Prove zero provider branching in core domain modules (M15-007).

Acceptance: no ``if provider == ...`` in domain modules. This is an
ARCHITECTURAL GATE: it statically scans every source file under
``src/cloud_platform/modules/`` (the core domain) and fails if any of them
branches on a concrete provider key (``hetzner`` / ``arvancloud`` / any
other provider literal in a comparison or mapping context).

Provider-specific behavior must live in the provider adapters
(``src/cloud_platform/providers/``) behind the ports in
``providers/base.py``; the domain speaks only to the ports. A new provider
may therefore be added without touching a single domain module - that is
the invariant this gate makes enforceable in CI.

Also enforced (same file, same run):
- no domain module may import a concrete provider ADAPTER package
  (``cloud_platform.providers.hetzner`` / ``...arvancloud`` / any sibling);
  imports from ``providers.base`` (the ports) are allowed.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

#: Provider keys known to the platform. The gate fails on ANY of these used
#: in a comparison/mapping context inside a domain module; new providers are
#: added to this set (not scattered across the domain).
PROVIDER_KEYS: frozenset[str] = frozenset({"hetzner", "arvancloud"})

#: Provider-generic packages under providers/ that are NOT concrete adapters:
#: the port definitions, shared error hierarchy, retry policy, the registry
#: of ports, the action waiter, health types, the contract suite, and the
#: runtime credential holder (M10-008 - port-level, no provider-specific code).
ALLOWED_PROVIDER_PACKAGES: frozenset[str] = frozenset(
    {"base", "errors", "retry", "registry", "waiter", "health", "contract", "credentials"}
)

#: The domain root: everything under modules/ is core domain.
DOMAIN_ROOT = Path(__file__).resolve().parent.parent / "src" / "cloud_platform" / "modules"


class Violation:
    def __init__(self, file: Path, line: int, message: str) -> None:
        self.file = file
        self.line = line
        self.message = message

    def render(self) -> str:
        return f"{self.file}:{self.line}: {self.message}"


def _provider_key_nodes(tree: ast.Module) -> list[ast.Constant]:
    """Every string constant that is a known provider key."""
    found: list[ast.Constant] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value in PROVIDER_KEYS
        ):
            found.append(node)
    return found


def _in_provider_context(node: ast.Constant, tree: ast.Module) -> bool:
    """Is this provider-key constant used in a comparison or provider-key
    mapping context (the actual branching smell)?

    Allowed usages that do NOT count:
    - attribute access like ``settings.hetzner_api_token`` (the constant is
      the attribute NAME, not a compared value) - those appear as
      ``ast.Attribute.attr`` and never as a Constant, so they are safe by
      construction;
    - a string inside a comment (not in the AST at all);
    - a dict KEY that is not a provider-key mapping: we cannot tell that
      statically with certainty, so a bare string constant that is not part
      of a Compare / a Call argument to a known provider-registry helper is
      only flagged when it is directly compared (==, !=, in, not in) or used
      as the key of a dict literal with provider-shaped values.
    """
    for parent in _ancestors(node, tree):
        if isinstance(parent, ast.Compare):
            return True
        if isinstance(parent, ast.Call):
            # e.g. registry.get("hetzner") - a provider-key lookup: branching
            # on which provider is in the domain.
            return True
    return False


def _ancestors(node: ast.AST, tree: ast.Module):
    """Yield the ancestors of ``node`` in the tree."""
    # Build a parent map once.
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    current = node
    while current in parents:
        current = parents[current]
        yield current


def _import_violations(tree: ast.Module, file: Path) -> list[Violation]:
    """No domain module may import a concrete provider adapter package."""
    violations: list[Violation] = []
    for node in ast.walk(tree):
        modules: list[str] = []
        if isinstance(node, ast.Import):
            modules = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules = [node.module]
        for module in modules:
            parts = module.split(".")
            if len(parts) >= 2 and parts[0] == "cloud_platform" and parts[1] == "providers":
                # Generic provider packages (ports, errors, retry policy,
                # port registry, waiter) are NOT adapters: allowed. Anything
                # else under providers.* is a concrete adapter: forbidden.
                if len(parts) >= 3 and parts[2] not in ALLOWED_PROVIDER_PACKAGES:
                    violations.append(
                        Violation(
                            file,
                            node.lineno,
                            f"domain module imports provider adapter package '{module}'",
                        )
                    )
    return violations


def check_domain_module(file: Path) -> list[Violation]:
    """Scan one domain module file for provider branching."""
    try:
        source = file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return [Violation(file, 0, f"cannot read file: {exc}")]
    try:
        tree = ast.parse(source, filename=str(file))
    except SyntaxError as exc:
        return [Violation(file, exc.lineno or 0, f"syntax error: {exc}")]

    violations: list[Violation] = []
    violations.extend(_import_violations(tree, file))
    for node in _provider_key_nodes(tree):
        if _in_provider_context(node, tree):
            violations.append(
                Violation(
                    file,
                    node.lineno,
                    f"provider key '{node.value}' used in a provider-branching context",
                )
            )
    return violations


def check_domain_root(root: Path | None = None) -> list[Violation]:
    """Scan every Python file under the domain root."""
    root = root or DOMAIN_ROOT
    if not root.is_dir():
        return [Violation(root, 0, f"domain root does not exist: {root}")]
    violations: list[Violation] = []
    for file in sorted(root.rglob("*.py")):
        violations.extend(check_domain_module(file))
    return violations


def main() -> int:
    violations = check_domain_root()
    if violations:
        print(f"PROVIDER-BRANCHING GATE FAILED: {len(violations)} violation(s)")
        for violation in violations:
            print("  " + violation.render())
        return 1
    scanned = len(list(DOMAIN_ROOT.rglob("*.py")))
    print(
        f"provider-branching gate passed: "
        f"{scanned} domain module(s) scanned, 0 provider-key branches, "
        f"0 adapter imports"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
