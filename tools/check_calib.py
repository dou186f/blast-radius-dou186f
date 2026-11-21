"""EVALUATION ONLY - compare our own labels (tools/label.py) with the organisers' answers.

    python3 tools/check_calib.py            # build/calib vs sample/answers.json

Shows how many commits are done and, for each finished one, whether the tests we saw
fail are exactly the ones the organisers list.
"""

import json
import sys
from pathlib import Path

calib = Path(sys.argv[1] if len(sys.argv) > 1 else "build/calib")
answers = json.loads(Path("sample/answers.json").read_text())["commits"]
tests = json.loads(Path("sample/tests.json").read_text())["tests"]
number_of = {t["id"]: t["number"] for t in tests}
by_file = {}
for t in tests:
    by_file.setdefault(t["file"], []).append(t["number"])
name = {t["number"]: t["id"].split("::")[-1] for t in tests}

done = sorted((calib / "results").glob("*.json"))
print(f"Biten: {len(done)}/{len(answers)}\n")
same = 0
for path in done:
    cid = path.stem
    result = json.loads(path.read_text())
    ours = {number_of[n] for n in result["failed"] if n in number_of}
    for f in result["broken_files"]:
        ours.update(by_file.get(f, []))
    theirs = set(answers[cid]["failing"])
    if ours == theirs:
        same += 1
        print(f"  {cid}  AYNI    ({len(theirs)} bozulan test)")
    else:
        only_ours = sorted(name[n] for n in ours - theirs)
        only_theirs = sorted(name[n] for n in theirs - ours)
        print(f"  {cid}  FARKLI  sadece bizde: {only_ours}  sadece onlarda: {only_theirs}")
print(f"\nAyni: {same}/{len(done)}")
