"""Migration compatibility gate (M12-003).

Acceptance: **deploy blocks unsafe migration patterns.**

A migration is *unsafe for a rolling deploy* when, after it has been applied,
either the OLD code (still running on not-yet-updated replicas) or the NEW
code (against a not-yet-migrated database) would break. This gate statically
analyses every alembic revision's ``upgrade()`` body (AST - no imports, no
execution) and blocks the closed set of patterns that have that property:

UNSAFE (blocking, overridable):
  drop_column        old code still SELECT/INSERTs the column -> errors
  drop_table         old code still queries the table -> errors
  rename_table       old code references the old name
  rename column      alter_column(new_column_name=...) - same
  add_column NN      NOT NULL column on an existing table without a
                     server_default -> old code's INSERTs fail
  tighten NN         alter_column(nullable=False) without a server_default

WARNINGS (non-blocking by default):
  unique index/constraint on existing rows (may fail on duplicates)
  op.execute() in upgrade (raw DDL the gate cannot analyse)
  missing or empty downgrade() (operability)

An unsafe pattern may be overridden in the revision file with a comment:

    # compat-override: <reason>

The override must name a reason; the gate reports it in the output so the
decision stays auditable, and exit code 0 means "deploy may proceed".

Usage:
    uv run python scripts/check_migrations.py            # alembic/versions
    uv run python scripts/check_migrations.py <dir>
    uv run python scripts/check_migrations.py --strict   # warnings fail too
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path

OVERRIDE_RE = re.compile(r"#\s*compat-override:\s*(?P<reason>\S.*)$")

#: op.* methods that are unsafe on the upgrade path, and what we look for.
DANGEROUS_METHODS = {
    "drop_column": "dropping a column",
    "drop_table": "dropping a table",
    "rename_table": "renaming a table",
}


@dataclass(frozen=True, slots=True)
class Finding:
    file: str
    line: int
    kind: str  # unsafe | warning | overridden
    detail: str


def _call_name(node: ast.expr) -> str | None:
    """'drop_column' for ``op.drop_column(...)``, else None."""
    if not isinstance(node, ast.Call):
        return None
    target = node.func
    while isinstance(target, ast.Attribute):
        if isinstance(target.value, ast.Name) and target.value.id in ("op", "alembic"):
            return target.attr
        target = target.value
    return None


def _kw_value(call: ast.Call, name: str) -> ast.expr | None:
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    return None


def _is_false(node: ast.expr | None) -> bool:
    return isinstance(node, ast.Constant) and node.value is False


def _has_server_default(call: ast.Call) -> bool:
    return _kw_value(call, "server_default") is not None


def _first_arg_str(call: ast.Call) -> str:
    if call.args and isinstance(call.args[0], ast.Constant):
        return str(call.args[0].value)
    return "?"


def _column_name(call: ast.Call) -> str:
    """The column name for add_column(table, sa.Column('name', ...))."""
    if len(call.args) >= 2 and isinstance(call.args[1], ast.Call):
        col = call.args[1]
        if col.args and isinstance(col.args[0], ast.Constant):
            return str(col.args[0].value)
    return "?"


def _upgrade_func(tree: ast.Module) -> ast.FunctionDef | None:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "upgrade":
            return node
    return None


def _downgrade_func(tree: ast.Module) -> ast.FunctionDef | None:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "downgrade":
            return node
    return None


def _has_body(func: ast.FunctionDef | None) -> bool:
    if func is None:
        return False
    return any(not isinstance(s, ast.Pass | ast.Expr) for s in func.body) or any(
        isinstance(s, ast.Expr) and isinstance(s.value, ast.Call) for s in func.body
    )


def check_source(source: str, filename: str) -> list[Finding]:
    """Analyse one migration file; returns findings (empty = clean)."""
    findings: list[Finding] = []
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [Finding(filename, exc.lineno or 0, "unsafe", f"file does not parse: {exc.msg}")]

    upgrade = _upgrade_func(tree)
    if upgrade is None:
        findings.append(Finding(filename, 0, "unsafe", "no upgrade() function found"))
        return findings

    overrides: list[tuple[int, str]] = []
    for i, line in enumerate(source.splitlines(), 1):
        match = OVERRIDE_RE.search(line)
        if match:
            overrides.append((i, match.group("reason").strip()))

    def report(line: int, kind: str, detail: str) -> None:
        # the nearest override above the finding claims it
        claimed = [reason for ov_line, reason in overrides if ov_line < line]
        if kind == "unsafe" and claimed:
            findings.append(
                Finding(filename, line, "overridden", f"{detail} | override: {claimed[-1]}")
            )
        else:
            findings.append(Finding(filename, line, kind, detail))

    for node in ast.walk(upgrade):
        if not isinstance(node, ast.Call):
            continue
        method = _call_name(node)
        if method is None:
            continue
        if method in DANGEROUS_METHODS:
            target = _first_arg_str(node)
            report(
                node.lineno,
                "unsafe",
                f"op.{method}({target!r}): {DANGEROUS_METHODS[method]} breaks old code",
            )
        elif method == "alter_column":
            if _kw_value(node, "new_column_name") is not None:
                col_name = _first_arg_str(node)
                report(
                    node.lineno,
                    "unsafe",
                    f"op.alter_column renames column {col_name!r}: old code uses the old name",
                )
            elif _is_false(_kw_value(node, "nullable")) and not _has_server_default(node):
                col_name = _first_arg_str(node)
                report(
                    node.lineno,
                    "unsafe",
                    f"op.alter_column tightens {col_name!r} to NOT NULL (no server_default)",
                )
        elif method == "add_column":
            # nullable= / server_default= ride on the sa.Column(...) argument
            col_call = (
                node.args[1] if len(node.args) >= 2 and isinstance(node.args[1], ast.Call) else None
            )
            lookup = col_call if col_call is not None else node
            nullable_kw = _kw_value(lookup, "nullable")
            if _is_false(nullable_kw) and not _has_server_default(lookup):
                table = _first_arg_str(node)
                col = _column_name(node)
                report(
                    node.lineno,
                    "unsafe",
                    f"op.add_column({table!r}, {col!r}) NOT NULL without a server_default: "
                    "old code's INSERTs would fail",
                )
        elif method in ("create_index", "create_unique_constraint"):
            if (
                method == "create_index"
                and _kw_value(node, "unique") is not None
                and (
                    not isinstance(_kw_value(node, "unique"), ast.Constant)
                    or _kw_value(node, "unique").value is True
                )
            ):
                report(
                    node.lineno,
                    "warning",
                    "unique index on existing rows: fails if duplicates exist",
                )
            elif method == "create_unique_constraint":
                report(
                    node.lineno,
                    "warning",
                    "unique constraint on existing rows: fails if duplicates exist",
                )
        elif method == "execute":
            report(node.lineno, "warning", "op.execute() raw SQL: not analysable by the gate")

    if not _has_body(_downgrade_func(tree)):
        findings.append(Finding(filename, 0, "warning", "downgrade() is missing or empty"))

    return findings


def check_directory(directory: Path) -> list[Finding]:
    files = sorted(directory.glob("*.py"))
    files = [f for f in files if f.name != "__init__.py"]
    if not files:
        raise SystemExit(f"no migration files in {directory}")
    findings: list[Finding] = []
    for path in files:
        findings.extend(check_source(path.read_text(encoding="utf-8"), path.name))
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="check_migrations")
    parser.add_argument("directory", nargs="?", default=None, help="alembic versions dir")
    parser.add_argument("--strict", action="store_true", help="treat warnings as failures")
    args = parser.parse_args(argv)

    if args.directory is not None:
        directory = Path(args.directory)
    else:
        directory = Path(__file__).resolve().parents[1] / "alembic" / "versions"

    findings = check_directory(directory)
    unsafe = [f for f in findings if f.kind == "unsafe"]
    overridden = [f for f in findings if f.kind == "overridden"]
    warnings = [f for f in findings if f.kind == "warning"]

    for f in sorted(findings, key=lambda x: (x.file, x.line)):
        prefix = {"unsafe": "BLOCK", "warning": "warn ", "overridden": "ok    "}
        print(f"[{prefix[f.kind]}] {f.file}:{f.line} {f.detail}")

    checked = len(list(directory.glob("*.py"))) - 1
    print(
        f"checked {checked} revision(s): "
        f"{len(unsafe)} unsafe, {len(overridden)} overridden, {len(warnings)} warning(s)"
    )

    if unsafe:
        print(
            "migration gate FAILED: fix the patterns above or add '# compat-override: <reason>'",
            file=sys.stderr,
        )
        return 1
    if args.strict and warnings:
        print("migration gate FAILED (strict): warnings present", file=sys.stderr)
        return 1
    print("migration gate passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
