"""One-shot supervisor helper: flip blocked->ready where deps are done."""

import yaml

TASKS_FILE = "docs/roadmap/TASKS.yaml"

with open(TASKS_FILE, encoding="utf-8") as f:
    data = yaml.safe_load(f)

tasks = data["tasks"]
by_id = {t["id"]: t for t in tasks}

flipped = []
for t in tasks:
    if t.get("status") == "blocked":
        deps = t.get("depends_on") or []
        if all(by_id.get(d, {}).get("status") == "done" for d in deps):
            t["status"] = "ready"
            flipped.append(t["id"])

output = yaml.dump(
    data,
    Dumper=yaml.SafeDumper,
    default_flow_style=False,
    sort_keys=False,
    allow_unicode=True,
    width=120,
    indent=2,
)
with open(TASKS_FILE, "w", encoding="utf-8") as f:
    f.write(output)

print(f"Flipped {len(flipped)} tasks to ready:")
for tid in flipped:
    print(f"  {tid}: {by_id[tid].get('title', '')}")
