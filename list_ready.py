"""List ready tasks with their dependencies (diagnostic helper)."""

import yaml

with open("docs/roadmap/TASKS.yaml") as f:
    data = yaml.safe_load(f)

ready = [t for t in data["tasks"] if t.get("status") == "ready"]
for t in sorted(ready, key=lambda x: x["id"]):
    deps = t.get("depends_on", [])
    print(f"{t['id']} ({t['milestone']}, {t['priority']}): {t.get('title', '')} deps={deps}")
print(f"\nTotal ready: {len(ready)}")
