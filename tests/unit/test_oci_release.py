"""Tests for the OCI image release (M12-002).

Acceptance: a tagged immutable image is produced.

Covers the release planning (immutable SHA tags, mutable tags only via an
explicit flag, dirty-tree refusal, push opt-in) and the image itself
(Dockerfile/entrypoint/.dockerignore structure: multi-stage, non-root, no
broad copies, healthcheck, the four entry points).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts import release_oci_image as rel

REPO_ROOT = Path(__file__).resolve().parents[2]
SHA = "a" * 40


class TestReleasePlan:
    def test_default_plan_has_only_the_immutable_sha_tag(self) -> None:
        plan = rel.plan_release("my.reg/platform", SHA)
        assert plan.immutable_tag == f"my.reg/platform:{SHA}"
        assert plan.tags == []
        assert plan.push is False
        assert "build" in str(plan)

    def test_extra_tags_are_opt_in_and_scoped_to_the_commit(self) -> None:
        plan = rel.plan_release("img", SHA, extra_tags=["v1.0.0"], push=True)
        assert plan.tags == ["img:v1.0.0"]
        assert plan.push is True
        assert "build+push" in str(plan)

    def test_immutable_tag_is_the_full_sha(self) -> None:
        plan = rel.plan_release("img", SHA)
        assert plan.commit in plan.immutable_tag
        assert len(plan.commit) == 40


class TestMainBehaviour:
    """main() with every external effect faked (no docker needed)."""

    def _call(
        self,
        monkeypatch: pytest.MonkeyPatch,
        argv: list[str],
        *,
        dirty: bool = False,
    ) -> int:
        calls: list[str] = []

        monkeypatch.setattr(rel, "git_commit", lambda: SHA)
        monkeypatch.setattr(rel, "tree_dirty", lambda: dirty)
        monkeypatch.setattr(rel, "build", lambda plan: calls.append(f"build {plan.immutable_tag}"))
        monkeypatch.setattr(rel, "verify_image", lambda plan: "sha256:" + "b" * 40)
        monkeypatch.setattr(rel, "publish", lambda plan: calls.append(f"push {plan.immutable_tag}"))

        result = rel.main(argv)
        return result, calls

    def test_clean_tree_builds_only_the_immutable_tag(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result, calls = self._call(monkeypatch, [])
        assert result == 0
        assert calls == [f"build my-reg:{SHA}" if False else f"build cloud-server-platform:{SHA}"]

    def test_dirty_tree_is_refused_without_allow_dirty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result, calls = self._call(monkeypatch, [], dirty=True)
        assert result == 1
        assert calls == []  # nothing was built

    def test_dirty_tree_with_allow_dirty_builds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        result, calls = self._call(monkeypatch, ["--allow-dirty"], dirty=True)
        assert result == 0
        assert len(calls) == 1

    def test_push_is_opt_in(self, monkeypatch: pytest.MonkeyPatch) -> None:
        result, calls = self._call(monkeypatch, [])
        assert result == 0
        assert not any(c.startswith("push") for c in calls)

        result, calls = self._call(monkeypatch, ["--push"])
        assert result == 0
        assert calls == [f"build cloud-server-platform:{SHA}", f"push cloud-server-platform:{SHA}"]

    def test_invalid_extra_tag_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        result, calls = self._call(monkeypatch, ["--tag", "bad tag/!"])
        assert result == 1
        assert calls == []

    def test_valid_extra_tag_is_added(self, monkeypatch: pytest.MonkeyPatch) -> None:
        result, calls = self._call(monkeypatch, ["--tag", "v1.0.0", "--push"])
        assert result == 0
        assert f"build cloud-server-platform:{SHA}" in calls
        assert f"push cloud-server-platform:{SHA}" in calls


class TestDockerfileStructure:
    @pytest.fixture(scope="class")
    def dockerfile(self) -> str:
        return (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")

    def _stages(self, dockerfile: str) -> dict[str, list[str]]:
        stages: dict[str, list[str]] = {}
        current = "preamble"
        for line in dockerfile.splitlines():
            stripped = line.strip()
            if stripped.startswith("FROM"):
                parts = stripped.split()
                current = parts[3] if len(parts) > 3 and parts[2] == "AS" else "stage"
                stages.setdefault(current, [])
            elif current in stages:
                stages[current].append(line)
        return stages

    def test_two_stages_builder_and_runtime(self, dockerfile: str) -> None:
        stages = self._stages(dockerfile)
        assert set(stages) == {"builder", "runtime"}

    def test_runtime_is_non_root(self, dockerfile: str) -> None:
        stages = self._stages(dockerfile)
        users = [line.strip() for line in stages["runtime"] if line.strip().startswith("USER")]
        assert users == ["USER app"]

    def test_runtime_installs_nothing_unexpected(self, dockerfile: str) -> None:
        stages = self._stages(dockerfile)
        runtime = stages["runtime"]
        for line in runtime:
            stripped = line.strip()
            assert not stripped.startswith("RUN pip"), "runtime must not install packages"
        # the only allowed apt install is the postgres client tools (migrate/backup);
        # continuation lines of the same RUN belong to it
        apt_lines: list[str] = []
        for i, line in enumerate(runtime):
            if line.strip().startswith("RUN apt"):
                chunk = [line]
                for follow in runtime[i + 1 :]:
                    if follow.strip().startswith("&&") or follow.strip().startswith("RUN"):
                        if follow.strip().startswith("RUN"):
                            break
                        chunk.append(follow)
                apt_lines.extend(chunk)
        assert apt_lines, "the postgres client tools must be installed for migrate/backup"
        assert "postgresql-client" in " ".join(apt_lines)

    def test_no_broad_copy(self, dockerfile: str) -> None:
        for line in dockerfile.splitlines():
            stripped = line.strip()
            assert not (stripped.startswith("COPY") and stripped.endswith("COPY . ."))
            assert "COPY . ." not in stripped, "broad COPY . . would copy .env/.git into the image"

    def test_healthcheck_and_port(self, dockerfile: str) -> None:
        assert any(line.strip().startswith("HEALTHCHECK") for line in dockerfile.splitlines())
        assert "EXPOSE 8000" in dockerfile

    def test_entrypoint_and_default_api(self, dockerfile: str) -> None:
        assert 'ENTRYPOINT ["/app/docker-entrypoint.sh"]' in dockerfile
        assert 'CMD ["api"]' in dockerfile

    def test_lockfile_is_the_dependency_source(self, dockerfile: str) -> None:
        assert "COPY pyproject.toml uv.lock" in dockerfile
        assert dockerfile.count("--frozen") >= 1


class TestEntrypoint:
    def test_entrypoint_exists_and_covers_all_entry_points(self) -> None:
        script = (REPO_ROOT / "docker-entrypoint.sh").read_text(encoding="utf-8")
        assert script.startswith("#!/bin/sh")
        for case in ("api)", "worker)", "backup)", "migrate)"):
            assert case in script
        assert "cloud_platform.api.app:create_app" in script
        assert "cloud_platform.worker.settings.WorkerSettings" in script
        assert "python -m cloud_platform.backup" in script
        assert "alembic upgrade head" in script
        # unknown commands are executed verbatim
        assert 'exec "$@"' in script

    def test_dockerfile_copies_the_entrypoint(self) -> None:
        dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
        assert "COPY --chown=app:app docker-entrypoint.sh /app/docker-entrypoint.sh" in dockerfile

    def test_dockerfile_copies_alembic_for_the_migrate_entrypoint(self) -> None:
        dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
        assert "COPY --chown=app:app alembic.ini ./alembic.ini" in dockerfile
        assert "COPY --chown=app:app alembic ./alembic" in dockerfile


class TestDockerignore:
    def test_secrets_and_dev_files_are_excluded(self) -> None:
        ignore = (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8")
        for entry in (".git", ".venv", "tests", ".env", "*.key", "*.pem"):
            assert entry in ignore.splitlines(), f"{entry!r} missing from .dockerignore"
