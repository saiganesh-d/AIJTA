"""Stand-in for the Copilot CLI in tests: replays scripted responses per agent, applies file edits,
prints a usage tail like the real CLI, and logs every invocation (argv, cwd) for assertions."""
import json
import os
import sys
from pathlib import Path

argv = sys.argv[1:]
if "--version" in argv:
    print("fake-copilot 0.0.0")
    sys.exit(0)
prompt = argv[argv.index("-p") + 1]
agent = argv[argv.index("--agent") + 1] if "--agent" in argv else ("global" if "AGENT.md" in prompt else "baseline")
scenario = Path(os.environ["FAKE_COPILOT_SCENARIO"])
state = scenario.with_suffix(".state.json")
counts = json.loads(state.read_text()) if state.exists() else {}
n = counts.get(agent, 0)
counts[agent] = n + 1
state.write_text(json.dumps(counts))
with scenario.with_suffix(".calls.jsonl").open("a") as fh:
    fh.write(json.dumps({"agent": agent, "argv": argv, "cwd": os.getcwd(),
                         "forge_files": sorted(p.name for p in Path(".forge").glob("*")) if Path(".forge").exists() else []}) + "\n")
responses = json.loads(scenario.read_text()).get(agent, [])
if not responses:
    print("no scripted response", file=sys.stderr)
    sys.exit(1)
r = responses[min(n, len(responses) - 1)]
for rel, content in (r.get("edits") or {}).items():
    p = Path(rel)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
if "json" in r:
    print("Working on it.\n```json\n" + json.dumps(r["json"], indent=1) + "\n```")
else:
    print(r.get("stdout", ""))
print(f"\nTotal usage: input {r.get('input', '4.2k')} tokens, output {r.get('output', 350)} tokens, cached 1k")
sys.exit(r.get("exit", 0))
