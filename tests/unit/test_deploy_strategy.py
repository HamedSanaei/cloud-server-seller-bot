"""Manual deployments cannot bypass production approval or immutable releases.

The shared engine's executable Docker/SSH failure, migration, locking and
single-consumer scenarios live in test_deploy_production and test_audit_cicd.
"""

from pathlib import Path

import pytest

from scripts.deploy import main


def arguments(path: Path, image: str, environment: str) -> list[str]:
    return [
        "--image",
        image,
        "--environment",
        environment,
        "--deploy-path",
        str(path),
        "--configuration-path",
        str(path / "configuration.toml"),
    ]


def test_production_without_explicit_approval_cannot_touch_target(tmp_path: Path) -> None:
    sentinel = tmp_path / "deploy.env"
    sentinel.write_text("PLATFORM_IMAGE=previous\n")
    with pytest.raises(SystemExit) as error:
        main(arguments(tmp_path, "ghcr.io/test/platform:" + "a" * 40, "production"))
    assert error.value.code == 2
    assert sentinel.read_text() == "PLATFORM_IMAGE=previous\n"
    assert set(tmp_path.iterdir()) == {sentinel}


@pytest.mark.parametrize("tag", ["latest", "staging", "a" * 12, "A" * 40])
def test_mutable_or_incomplete_release_is_rejected_before_target_changes(
    tmp_path: Path, tag: str
) -> None:
    with pytest.raises(SystemExit) as error:
        main(arguments(tmp_path, "ghcr.io/test/platform:" + tag, "staging"))
    assert error.value.code == 2
    assert list(tmp_path.iterdir()) == []
