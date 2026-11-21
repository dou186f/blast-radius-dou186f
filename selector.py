"""Blast Radius selector: pick the tests a patch can break, split them over workers.

All heavy lifting happened offline (tools/build_map.py): for every line of sqlglot at the
pinned commit, data/covmap.json.gz holds the tests to run if that line changes. Here we
only parse each diff, drop comment/whitespace-only edits, look lines up and bin-pack.
Standard library only; stdout carries answers only, logs go to stderr.
"""

from __future__ import annotations

import gzip
import json
import re
import sys
from pathlib import Path

DATA = Path("/data")
MAP = Path(__file__).resolve().parent / "data" / "covmap.json.gz"
MAX_WORKERS = 8
WORKER_COST_MS = 2000

HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
# Files every test module imports: any real change there can break anything.
SHARED_TEST_FILES = {"tests/__init__.py", "tests/helpers.py", "tests/dialects/__init__.py", "tests/conftest.py"}

ALL = "ALL"


# --------------------------------------------------------------------------- diff
def parse_patch(text: str):
    """-> [(old_path, new_path, [(old_start, [line, ...]), ...])]"""
    files = []
    current = None
    old_path = new_path = None
    for line in text.split("\n"):
        if line.startswith("diff --git "):
            current = None
            old_path = new_path = None
            parts = line.split(" ")
            if len(parts) >= 4:
                old_path, new_path = parts[2][2:], parts[3][2:]
            files.append([old_path, new_path, []])
            current = files[-1]
        elif current is not None and line.startswith("--- ") and not current[2]:
            p = line[4:].split("\t")[0]
            current[0] = None if p == "/dev/null" else p[2:] if p.startswith("a/") else p
        elif current is not None and line.startswith("+++ ") and not current[2]:
            p = line[4:].split("\t")[0]
            current[1] = None if p == "/dev/null" else p[2:] if p.startswith("b/") else p
        elif current is not None and line.startswith("@@"):
            m = HUNK.match(line)
            if not m:
                raise ValueError("bad hunk header")
            current[2].append((int(m.group(1)), []))
        elif current is not None and current[2] and line[:1] in (" ", "-", "+", "\\"):
            current[2][-1][1].append(line)
        elif current is not None and current[2] and line == "":
            current[2][-1][1].append(" ")  # a context line whose single space was stripped
    return files


def segments(hunk_start: int, lines: list[str]):
    """Yield (removed [(old_line, text)], added [text], gap) per run of changed lines.
    gap = the old line after which a pure insertion sits."""
    old = hunk_start
    removed, added, gap = [], [], hunk_start - 1
    for line in lines:
        tag = line[:1]
        if tag == "\\":
            continue
        if tag == " ":
            if removed or added:
                yield removed, added, gap
                removed, added = [], []
            old += 1
            gap = old - 1
        elif tag == "-":
            removed.append((old, line[1:]))
            old += 1
        else:
            added.append(line[1:])
    if removed or added:
        yield removed, added, gap


def code_part(text: str) -> str:
    """The line without its comment (quote-aware on one line) and trailing blanks."""
    if "#" not in text:
        return text.rstrip()
    quote = None
    i = 0
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == "\\":
                i += 1
            elif ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch == "#":
            return text[:i].rstrip()
        i += 1
    return text.rstrip()


def in_string(spans, line: int) -> bool:
    """True when `line` starts inside a multi-line string token."""
    for i in range(0, len(spans), 2):
        if spans[i] < line <= spans[i + 1]:
            return True
    return False


def is_trivial(removed, added, gap, spans, bslash) -> bool:
    """Only comments / blank lines changed, outside strings, not after a backslash."""
    if any(in_string(spans, n) for n, _ in removed):
        return False
    first = removed[0][0] if removed else gap + 1
    if in_string(spans, first) or (first - 1) in bslash:
        return False
    old_code = [c for c in (code_part(t) for _, t in removed) if c.strip()]
    new_code = [c for c in (code_part(t) for t in added) if c.strip()]
    return old_code == new_code


# --------------------------------------------------------------------------- select
class Selector:
    def __init__(self, tests: list[dict], cmap: dict):
        self.tests = tests
        number_of = {t["id"]: t["number"] for t in tests}
        to_number = [number_of.get(tid) for tid in cmap["tests"]]
        self.sets = [[to_number[i] for i in s if to_number[i] is not None] for s in cmap["sets"]]
        self.impact = cmap["impact"]
        self.strs = cmap["strs"]
        self.bslash = {p: set(v) for p, v in cmap["bslash"].items()}
        self.opens = cmap["opens"]
        self.tspans = cmap["tspans"]
        self.nocov = self.sets[cmap["nocov"]]
        self.by_file: dict[str, list[int]] = {}
        for t in tests:
            self.by_file.setdefault(t["file"], []).append(t["number"])
        # tests.json may list tests the map has never seen: treat them like nocov ones
        mapped = {n for n in to_number if n is not None}
        self.unmapped = [t["number"] for t in tests if t["number"] not in mapped]

    def select(self, patch_text: str):
        chosen: set[int] = set()
        for old_path, new_path, hunks in parse_patch(patch_text):
            result = self.select_file(old_path, new_path, hunks)
            if result is ALL:
                return ALL
            chosen.update(result)
        return chosen

    def select_file(self, old_path, new_path, hunks):
        path = old_path or new_path
        if path is None:
            return ALL
        if old_path is None:  # a brand-new file: nothing imports or opens it yet
            return ()
        if new_path is None and path.startswith(("sqlglot/", "tests/")):
            return ALL  # deleting a module or fixture
        if path.endswith(".py") and path.startswith("sqlglot/"):
            return self.select_source(path, hunks)
        if path.startswith("tests/"):
            return self.select_tests_dir(path, hunks)
        if path.startswith("sqlglotc/"):
            return self.by_file.get("tests/test_sqlglotc.py", [])
        return ()  # docs, CI, packaging: not run by the suite

    def select_source(self, path, hunks):
        impact = self.impact.get(path)
        if impact is None:
            return ALL  # an existing module we never saw: be safe
        spans = self.strs.get(path, [])
        bslash = self.bslash.get(path, set())
        out: set[int] = set()
        last = len(impact)
        for start, lines in hunks:
            for removed, added, gap in segments(start, lines):
                if is_trivial(removed, added, gap, spans, bslash):
                    continue
                if removed:
                    lines_hit = [n for n, _ in removed]
                else:
                    lines_hit = [gap, gap + 1]
                for n in lines_hit:
                    n = min(max(n, 1), last)
                    out.update(self.sets[impact[n - 1]])
                out.update(self.unmapped)
        return out

    def select_tests_dir(self, path, hunks):
        if path.endswith(".py"):
            if path in SHARED_TEST_FILES:
                return ALL if self.has_real_change(hunks, path) else ()
            spans = self.tspans.get(path)
            if spans is None:
                return ()  # not a collected test module (gen_fixtures.py, ...)
            out: set[int] = set()
            whole_file = self.by_file.get(path, [])
            for start, lines in hunks:
                for removed, added, gap in segments(start, lines):
                    if is_trivial(removed, added, gap, [], set()):
                        continue
                    # a change inside one test method hits that test; anything else in
                    # the module (helpers, class attributes, imports) hits the whole file
                    first, last = (removed[0][0], removed[-1][0]) if removed else (gap, gap + 1)
                    owner = None
                    for i in range(0, len(spans), 3):
                        if spans[i] <= first and last <= spans[i + 1]:
                            owner = self.sets[spans[i + 2]]
                            break
                    out.update(whole_file if owner is None else owner)
            return out
        index = self.opens.get(path)
        return self.sets[index] if index is not None else ()

    @staticmethod
    def has_real_change(hunks, path):
        for start, lines in hunks:
            for removed, added, gap in segments(start, lines):
                if not is_trivial(removed, added, gap, [], set()):
                    return True
        return False


# --------------------------------------------------------------------------- packing
def pack(numbers, duration_ms):
    """LPT for k = 1..8; keep the k with the smallest busiest-worker + 2 s per worker."""
    if not numbers:
        return []
    order = sorted(numbers, key=lambda n: (-duration_ms[n], n))
    best_cost, best = None, None
    for k in range(1, min(MAX_WORKERS, len(order)) + 1):
        loads = [0] * k
        bins: list[list[int]] = [[] for _ in range(k)]
        for n in order:
            i = loads.index(min(loads))
            loads[i] += duration_ms[n]
            bins[i].append(n)
        cost = max(loads) + WORKER_COST_MS * k
        if best_cost is None or cost < best_cost:
            best_cost, best = cost, bins
    return best


def main() -> None:
    tests = json.loads((DATA / "tests.json").read_text())["tests"]
    duration_ms = {t["number"]: int(t["seconds"] * 1000 + 0.5) for t in tests}
    all_numbers = [t["number"] for t in tests]
    with gzip.open(MAP, "rt") as f:
        selector = Selector(tests, json.load(f))

    lines = []
    for patch in sorted((DATA / "commits").glob("*.patch")):
        try:
            chosen = selector.select(patch.read_text(encoding="utf-8", errors="replace"))
        except Exception as error:  # never die on one odd patch: run everything
            print(f"{patch.stem}: {error!r}, running all", file=sys.stderr)
            chosen = ALL
        numbers = all_numbers if chosen is ALL else sorted(chosen)
        workers = pack(numbers, duration_ms)
        lines.append(json.dumps({"commit": patch.stem, "workers": workers}, separators=(",", ":")))
        print(f"{patch.stem}: {len(numbers)} tests on {len(workers)} workers", file=sys.stderr)
    sys.stdout.write("\n".join(lines) + ("\n" if lines else ""))


if __name__ == "__main__":
    main()
