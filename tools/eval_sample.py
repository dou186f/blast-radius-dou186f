"""Run selector.py locally (no Docker) on a data dir and explain the result per commit.

    python3 tools/eval_sample.py [sample]

Answers are only read here, for evaluation; the selector never sees them.
"""

import contextlib
import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import selector  # noqa: E402

data = Path(sys.argv[1] if len(sys.argv) > 1 else "sample")
selector.DATA = data
out = io.StringIO()
with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
    selector.main()

tests = {t["number"]: t for t in json.loads((data / "tests.json").read_text())["tests"]}
answers = json.loads((data / "answers.json").read_text())["commits"]
ms = {n: int(t["seconds"] * 1000 + 0.5) for n, t in tests.items()}
total = 0
misses = 0
print(f"{'commit':6} {'kind':8} {'sel':>5} {'cost':>7} {'ideal':>6}  missed")
for line in out.getvalue().splitlines():
    row = json.loads(line)
    workers = [w for w in row["workers"] if w]
    chosen = {n for w in workers for n in w}
    cost = (max(sum(ms[n] for n in w) for w in workers) + 2000 * len(workers)) if workers else 0
    answer = answers[row["commit"]]
    missed = [n for n in answer["failing"] if n not in chosen]
    ideal = selector.pack(answer["failing"], ms)
    ideal_cost = (max(sum(ms[n] for n in w) for w in ideal) + 2000 * len(ideal)) if ideal else 0
    total += cost + (300_000 if missed else 0)
    misses += bool(missed)
    names = [tests[n]["id"].split("::")[-1] for n in missed]
    print(f"{row['commit']:6} {answer['kind']:8} {len(chosen):5} {cost / 1000:7.1f} {ideal_cost / 1000:6.1f}  {names}")
print(f"Simulated: {total / 1000:.3f} s   Misses: {misses}")
