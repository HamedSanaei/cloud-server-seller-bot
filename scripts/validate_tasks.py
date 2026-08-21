from pathlib import Path
import re

path = Path("docs/roadmap/TASKS.yaml")
text = path.read_text(encoding="utf-8")
ids = re.findall(r"^  - id: (M\d{2}-\d{3})$", text, re.M)
deps = re.findall(r"^      - (M\d{2}-\d{3})$", text, re.M)
missing = sorted(set(deps) - set(ids))
if missing:
    raise SystemExit(f"Unknown task dependencies: {missing}")
if len(ids) != len(set(ids)):
    raise SystemExit("Duplicate task IDs found")
print(f"OK: {len(ids)} tasks; all dependency IDs resolve")
