"""Enforce the source-line coverage floor using exact artifact counts, not rounding."""

from __future__ import annotations

import sys
from pathlib import Path
from xml.etree import ElementTree

MINIMUM_PERCENT = 88


def main(path: Path) -> int:
    root = ElementTree.parse(path).getroot()
    covered = int(root.attrib["lines-covered"])
    total = int(root.attrib["lines-valid"])
    if total <= 0 or not 0 <= covered <= total:
        print("Coverage artifact must contain a valid, non-empty source-line inventory")
        return 1
    passed = covered * 100 >= MINIMUM_PERCENT * total
    print(
        f"Exact coverage: {covered}/{total} lines ({covered * 100 / total:.6f}%); "
        f"required >= {MINIMUM_PERCENT}%: {'PASS' if passed else 'FAIL'}"
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main(Path(sys.argv[1])))
