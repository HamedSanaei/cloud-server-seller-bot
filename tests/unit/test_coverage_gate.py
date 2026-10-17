"""A rounded percentage must never authorize an under-covered release."""

from pathlib import Path

import pytest

from scripts.check_coverage import main


@pytest.mark.parametrize(
    "covered,total,expected",
    [(879999, 1000000, 1), (88, 100, 0), (0, 0, 1)],
)
def test_source_line_coverage_floor_is_exact(
    tmp_path: Path, covered: int, total: int, expected: int
) -> None:
    artifact = tmp_path / "coverage.xml"
    artifact.write_text(
        f'<coverage lines-covered="{covered}" lines-valid="{total}" />', encoding="utf-8"
    )
    assert main(artifact) == expected
