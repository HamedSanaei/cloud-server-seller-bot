import re
from pathlib import Path

path = Path("docs/roadmap/TASKS.yaml")
text = path.read_text(encoding="utf-8")

# Match task IDs at the start of a line (handles both "  - id:" and "- id:" formats)
ids = re.findall(r"^(?:\s*)- id: (M\d{2}-\d{3})$", text, re.M)

# Match dependency references (handles various indentation levels)
deps = re.findall(r"^(?:\s*)- (M\d{2}-\d{3})$", text, re.M)

missing = sorted(set(deps) - set(ids))
if missing:
    raise SystemExit(f"Unknown task dependencies: {missing}")
if len(ids) != len(set(ids)):
    raise SystemExit("Duplicate task IDs found")
print(f"OK: {len(ids)} tasks; all dependency IDs resolve")
