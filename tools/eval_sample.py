"""Run selector.py locally (no Docker) on a data dir and explain the result per commit.

    python3 tools/eval_sample.py [sample|build/val] [--all]

Answers are only read here, for evaluation; the selector never sees them. With more
than 40 commits only misses and the costliest commits are listed unless --all.
"""

import contextlib
import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import selector  # noqa: E402

args = [a for a in sys.argv[1:] if not a.startswith("--")]
data = Path(args[0] if args else "sample")
selector.DATA = data
out = io.StringIO()
with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
    selector.main()

tests = {t["number"]: t for t in json.loads((data / "tests.json").read_text())["tests"]}
answers = json.loads((data / "answers.json").read_text())["commits"]
ms = {n: int(t["seconds"] * 1000 + 0.5) for n, t in tests.items()}


def cost_of(workers):
    workers = [w for w in workers if w]
    return (max(sum(ms[n] for n in w) for w in workers) + 2000 * len(workers)) if workers else 0


rows = []
for line in out.getvalue().splitlines():
    row = json.loads(line)
    if row["commit"] not in answers:
        continue
    answer = answers[row["commit"]]
    chosen = {n for w in row["workers"] for n in w}
    missed = [n for n in answer["failing"] if n not in chosen]
    rows.append((row["commit"], answer["kind"], len(chosen), cost_of(row["workers"]),
                 cost_of(selector.pack(answer["failing"], ms)), missed))

show_all = "--all" in sys.argv or len(rows) <= 40
costliest = {r[0] for r in sorted(rows, key=lambda r: r[4] - r[3])[:10]}
print(f"{'commit':6} {'kind':8} {'sel':>5} {'cost':>7} {'ideal':>6}  missed")
for cid, kind, n, cost, ideal, missed in rows:
    if show_all or missed or cid in costliest:
        names = [tests[m]["id"].split("::")[-1] for m in missed]
        print(f"{cid:6} {kind:8} {n:5} {cost / 1000:7.1f} {ideal / 1000:6.1f}  {names}")
total = sum(r[3] for r in rows) + 300_000 * sum(bool(r[5]) for r in rows)
ideal = sum(r[4] for r in rows)
print(f"commits={len(rows)}  Simulated: {total / 1000:.3f} s   Misses: {sum(bool(r[5]) for r in rows)}"
      f"   Ideal: {ideal / 1000:.3f} s")
