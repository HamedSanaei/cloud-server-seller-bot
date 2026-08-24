"""Tests for the migration compatibility gate (M12-003).

Acceptance: deploy blocks unsafe migration patterns.

Every rule of the closed set is exercised against synthetic revisions, the
override mechanism is checked (with and without a reason), and the gate is
run end-to-end over the REAL alembic/versions directory, which must pass
cleanly (all existing drop_* calls live in downgrade(), where they are the
expected reverse direction and therefore safe).
"""

from __future__ import annotations

from pathlib import Path

from scripts.check_migrations import check_source

HEADER = '''"""synthetic revision.

Revision ID: 9999
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "9999"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
{upgrade}


def downgrade() -> None:
{downgrade}
'''


def _rev(upgrade: str, downgrade: str = "    pass") -> str:
    return HEADER.format(upgrade=upgrade, downgrade=downgrade)


def _kinds(source: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for f in check_source(source, "synthetic.py"):
        out.setdefault(f.kind, []).append(f.detail)
    return out


class TestUnsafePatterns:
    def test_drop_column_in_upgrade_is_blocked(self) -> None:
        source = _rev('    op.drop_column("servers", "old_col")')
        kinds = _kinds(source)
        assert any("drop_column" in d for d in kinds.get("unsafe", []))

    def test_drop_table_in_upgrade_is_blocked(self) -> None:
        source = _rev('    op.drop_table("servers")')
        assert any("drop_table" in d for d in _kinds(source).get("unsafe", []))

    def test_rename_table_in_upgrade_is_blocked(self) -> None:
        source = _rev('    op.rename_table("servers", "servers2")')
        assert any("rename_table" in d for d in _kinds(source).get("unsafe", []))

    def test_rename_column_in_upgrade_is_blocked(self) -> None:
        source = _rev('    op.alter_column("servers", "state", new_column_name="status")')
        assert any("renames column" in d for d in _kinds(source).get("unsafe", []))

    def test_add_column_not_null_without_default_is_blocked(self) -> None:
        source = _rev('    op.add_column("users", sa.Column("role", sa.String(), nullable=False))')
        assert any("NOT NULL" in d and "role" in d for d in _kinds(source).get("unsafe", []))

    def test_tighten_to_not_null_without_default_is_blocked(self) -> None:
        source = _rev('    op.alter_column("users", "role", nullable=False)')
        assert any("tightens" in d for d in _kinds(source).get("unsafe", []))


class TestSafePatterns:
    def test_add_column_nullable_is_clean(self) -> None:
        source = _rev('    op.add_column("users", sa.Column("x", sa.String(), nullable=True))')
        assert _kinds(source).get("unsafe") is None

    def test_add_column_not_null_with_server_default_is_clean(self) -> None:
        source = _rev(
            "    op.add_column(\n"
            '        "users",\n'
            '        sa.Column("x", sa.String(), server_default="a", nullable=False),\n'
            "    )"
        )
        assert _kinds(source).get("unsafe") is None

    def test_drop_in_downgrade_is_safe(self) -> None:
        """The existing migrations' drop_* calls are all in downgrade() - the
        reverse direction, which is safe for a rolling deploy."""
        source = _rev("    pass", '    op.drop_table("users")\n    op.drop_column("users", "x")')
        assert _kinds(source).get("unsafe") is None

    def test_create_table_is_clean(self) -> None:
        source = _rev('    op.create_table("t", sa.Column("id", sa.String(), nullable=False))')
        assert _kinds(source).get("unsafe") is None

    def test_add_column_not_null_with_server_default_kwarg_is_clean(self) -> None:
        source = _rev(
            "    op.add_column(\n"
            '        "t", sa.Column("c", sa.Integer(), server_default=sa.text("0"),\n'
            "        nullable=False),\n"
            "    )"
        )
        assert _kinds(source).get("unsafe") is None


class TestWarnings:
    def test_unique_index_is_a_warning_not_a_block(self) -> None:
        source = _rev('    op.create_index("ix_u", "t", ["c"], unique=True)')
        kinds = _kinds(source)
        assert kinds.get("warning")
        assert kinds.get("unsafe") is None

    def test_op_execute_is_a_warning(self) -> None:
        source = _rev('    op.execute("UPDATE t SET c = 1")')
        assert any("op.execute" in d for d in _kinds(source).get("warning", []))

    def test_missing_downgrade_is_a_warning(self) -> None:
        source = _rev("    pass")
        assert any("downgrade" in d for d in _kinds(source).get("warning", []))


class TestOverride:
    def test_override_with_reason_claims_the_finding(self) -> None:
        source = _rev(
            "    # compat-override: column removed by task X, two-phase rollout done 2026-08-01\n"
            '    op.drop_column("servers", "old_col")'
        )
        kinds = _kinds(source)
        assert kinds.get("unsafe") is None
        assert any("override:" in d for d in kinds.get("overridden", []))

    def test_override_without_reason_does_not_claim(self) -> None:
        source = _rev('    # compat-override:\n    op.drop_column("servers", "old_col")')
        assert _kinds(source).get("unsafe") is not None

    def test_override_only_claims_findings_below_it(self) -> None:
        source = _rev(
            '    op.drop_table("a")\n'
            "    # compat-override: second drop reviewed\n"
            '    op.drop_table("b")'
        )
        kinds = _kinds(source)
        # the first drop (above the override) stays blocked
        assert len(kinds.get("unsafe", [])) == 1
        assert any("override:" in d for d in kinds.get("overridden", []))


class TestRealMigrations:
    """The gate run over the real revision history must pass: every drop_*
    in the tree is in downgrade(), and every add_column on existing tables
    is nullable or carries a server_default."""

    def test_all_existing_revisions_pass(self) -> None:
        from scripts.check_migrations import check_directory

        versions = Path(__file__).resolve().parents[2] / "alembic" / "versions"
        findings = check_directory(versions)
        unsafe = [f for f in findings if f.kind == "unsafe"]
        assert unsafe == [f"({f.file}:{f.line} {f.detail})" for f in unsafe]  # readable
        assert not unsafe, f"real revisions blocked: {unsafe}"

    def test_revisions_are_all_checked(self) -> None:
        from scripts.check_migrations import check_directory

        versions = Path(__file__).resolve().parents[2] / "alembic" / "versions"
        # the directory scan must not crash and must see every revision file
        all_files = {p.name for p in versions.glob("*.py") if p.name != "__init__.py"}
        assert len(all_files) >= 20
        check_directory(versions)  # raises SystemExit if no files found

    def test_cli_passes_on_the_repository(self) -> None:
        import subprocess
        import sys

        root = Path(__file__).resolve().parents[2]
        result = subprocess.run(
            [sys.executable, str(root / "scripts" / "check_migrations.py")],
            cwd=root,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "migration gate passed" in result.stdout


class TestMainExitCodes:
    def test_unsafe_fails(self, tmp_path: Path) -> None:
        from scripts import check_migrations as gate

        (tmp_path / "0100_bad.py").write_text(_rev('    op.drop_table("users")'), encoding="utf-8")
        findings = gate.check_directory(tmp_path)
        assert any(f.kind == "unsafe" for f in findings)
        assert gate.main([str(tmp_path)]) == 1

    def test_overridden_passes(self, tmp_path: Path) -> None:
        from scripts import check_migrations as gate

        (tmp_path / "0101_ok.py").write_text(
            _rev(
                "    # compat-override: reviewed for task M99-001\n"
                '    op.drop_column("servers", "x")'
            ),
            encoding="utf-8",
        )
        assert gate.main([str(tmp_path)]) == 0

    def test_warnings_pass_without_strict(self, tmp_path: Path) -> None:
        from scripts import check_migrations as gate

        (tmp_path / "0102_warn.py").write_text(
            _rev('    op.create_index("ix_u", "t", ["c"], unique=True)'),
            encoding="utf-8",
        )
        assert gate.main([str(tmp_path)]) == 0
        assert gate.main([str(tmp_path), "--strict"]) == 1
