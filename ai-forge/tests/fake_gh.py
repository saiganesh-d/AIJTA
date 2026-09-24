"""Stand-in for the GitHub CLI: pr create / pr view / auth status."""
import json
import os
import sys
from pathlib import Path

a = sys.argv[1:]
log = Path(os.environ["FAKE_GH_DIR"]) / "gh.calls.jsonl"
with log.open("a") as fh:
    fh.write(json.dumps(a) + "\n")
if a[:2] == ["auth", "status"]:
    sys.exit(0)
if a[:2] == ["pr", "create"]:
    print("https://github.com/org/app/pull/7")
    sys.exit(0)
if a[:2] == ["pr", "view"]:
    st = Path(os.environ["FAKE_GH_DIR"]) / "pr_state.json"
    if "state,mergeCommit" in a:
        print(st.read_text() if st.exists() else json.dumps({"state": "OPEN", "mergeCommit": None}))
        sys.exit(0)
    sys.exit(1)
sys.exit(1)
