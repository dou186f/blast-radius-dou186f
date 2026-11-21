"""Blast Radius selector: pick the tests a patch can break, split them over workers.

All heavy lifting happened offline (tools/build_map.py): for every line of sqlglot at the
pinned commit, data/covmap.json.gz holds the tests to run if that line changes. Here we
only parse each diff, drop comment/whitespace-only edits, look lines up and bin-pack.
Standard library only; stdout carries answers only, logs go to stderr.
"""

from __future__ import annotations

import gzip
import io
import json
import re
import sys
import tokenize
from pathlib import Path

DATA = Path("/data")
MAP = Path(__file__).resolve().parent / "data" / "covmap.json.gz"
MAX_WORKERS = 8
WORKER_COST_MS = 2000

HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
# Files every test module imports: any real change there can break anything.
SHARED_TEST_FILES = {"tests/__init__.py", "tests/helpers.py", "tests/dialects/__init__.py", "tests/conftest.py"}

ALL = "ALL"

# Experiment: these two tests are 55% of the cost on practice-like data while rarely being
# the reason a commit fails. Left out of every selection except "run everything".
SKIPPED_GIANTS = (
    "tests/test_executor.py::TestExecutor::test_execute_tpcds",
    "tests/test_parser.py::TestParser::test_max_nodes",
)


# --------------------------------------------------------------------------- diff
def _strip_prefix(path: str, prefix: str):
    path = path.split("\t")[0].strip()
    if path == "/dev/null":
        return None
    return path[2:] if path.startswith(prefix) else path


def parse_patch(text: str):
    """-> [(old_path, new_path, [(old_start, [line, ...]), ...])]

    Hunk bodies are consumed by their line counts, so a removed SQL comment ("-- x" shows
    up as "--- x") is never mistaken for a file header, and plain unified diffs without a
    "diff --git" line work too. Renames and new/deleted files keep None on the missing side.
    """
    files: list[list] = []
    current = None
    old_left = new_left = 0
    for line in text.split("\n"):
        if old_left > 0 or new_left > 0:  # inside a hunk body
            tag = line[:1] if line else " "  # a context line whose single space was stripped
            if tag in (" ", "-", "+", "\\"):
                if tag in (" ", "-"):
                    old_left -= 1
                if tag in (" ", "+"):
                    new_left -= 1
                current[2][-1][1].append(line or " ")
                continue
            old_left = new_left = 0  # malformed or truncated hunk: fall through to headers
        if line.startswith("diff --git "):
            parts = line.split(" ")
            old_path = parts[2][2:] if len(parts) >= 4 else None
            new_path = parts[3][2:] if len(parts) >= 4 else None
            current = [old_path, new_path, [], False]
            files.append(current)
        elif line.startswith("--- "):
            if current is None or current[2] or current[3]:  # a new file section
                current = [None, None, [], False]
                files.append(current)
            current[0] = _strip_prefix(line[4:], "a/")
            current[3] = True
        elif line.startswith("+++ ") and current is not None:
            current[1] = _strip_prefix(line[4:], "b/")
        elif line.startswith("rename from ") and current is not None:
            current[0] = line[len("rename from "):].strip()
        elif line.startswith("rename to ") and current is not None:
            current[1] = line[len("rename to "):].strip()
        elif line.startswith("new file mode") and current is not None:
            current[0] = None
        elif line.startswith("deleted file mode") and current is not None:
            current[1] = None
        elif line.startswith("@@") and current is not None:
            m = HUNK.match(line)
            if not m:
                raise ValueError("bad hunk header")
            old_left = int(m.group(2)) if m.group(2) is not None else 1
            new_left = int(m.group(4)) if m.group(4) is not None else 1
            start = int(m.group(1))
            if old_left == 0:
                # an empty old range ("-17,0", diff -U0 insertions) names the line BEFORE
                # the insertion; with context it names the first line of the hunk
                start += 1
            current[2].append((start, []))
    return [(old, new, hunks) for old, new, hunks, _ in files]


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


def same_meaning(text: str, enums) -> str:
    """Token-normalised form of one line where an identity test against None or an enum
    member ("x is None", "t is TokenType.L_PAREN", with or without "not") is written as the
    equivalent equality test. Works on Python tokens, so string contents are never touched;
    "is" against anything else (True/False, arbitrary objects, calls like E.M.into_expr())
    is left as is. A line that does not tokenize is returned unchanged (not equivalent)."""
    try:
        toks = [t for t in tokenize.generate_tokens(io.StringIO(text.strip()).readline)
                if t.type not in (tokenize.NEWLINE, tokenize.NL, tokenize.ENDMARKER,
                                  tokenize.INDENT, tokenize.DEDENT, tokenize.COMMENT)]
    except (tokenize.TokenError, SyntaxError, IndentationError):
        return text
    out = []
    i = 0
    while i < len(toks):
        tok = toks[i]
        if tok.type == tokenize.NAME and tok.string == "is":
            negated = i + 1 < len(toks) and toks[i + 1].string == "not"
            j = i + 2 if negated else i + 1
            # right-hand side: None, or a dotted name whose last-but-one part is an enum
            k = j
            parts = []
            while k < len(toks) and toks[k].type == tokenize.NAME:
                parts.append(toks[k].string)
                if k + 1 < len(toks) and toks[k + 1].string == "." and k + 2 < len(toks) \
                        and toks[k + 2].type == tokenize.NAME:
                    k += 2
                else:
                    k += 1
                    break
            following = toks[k].string if k < len(toks) else ""
            plain_end = following not in (".", "(", "[")
            is_none = parts == ["None"]
            is_member = enums is not None and len(parts) >= 2 and parts[-2] in enums
            if plain_end and (is_none or is_member):
                out.append("!=" if negated else "==")
                i = j
                continue
        out.append(tok.string)
        i += 1
    return " ".join(out)


def in_string(spans, line: int) -> bool:
    """True when `line` starts inside a multi-line string token."""
    for i in range(0, len(spans), 2):
        if spans[i] < line <= spans[i + 1]:
            return True
    return False


def is_trivial(removed, added, gap, spans, bslash, pure_enums=None) -> bool:
    """Only comments / blank lines changed (outside strings, not after a backslash), or an
    identity test swapped for an equality test that means the same (see same_meaning)."""
    if any(in_string(spans, n) for n, _ in removed):
        return False
    first = removed[0][0] if removed else gap + 1
    if in_string(spans, first) or (first - 1) in bslash:
        return False
    old_code = [c for c in (code_part(t) for _, t in removed) if c.strip()]
    new_code = [c for c in (code_part(t) for t in added) if c.strip()]
    if old_code == new_code:
        return True
    if len(old_code) != len(new_code):
        return False
    return [same_meaning(c, pure_enums) for c in old_code] == [same_meaning(c, pure_enums) for c in new_code]


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
        self.timport = cmap.get("timport", {})
        self.pure_enums = frozenset(cmap.get("pure_enums", []))
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
        in_suite = path.startswith(("sqlglot/", "tests/"))
        if old_path is None:  # a brand-new file
            name = path.rsplit("/", 1)[-1]
            if path.startswith("tests/") and name in ("conftest.py", "__init__.py"):
                return ALL  # pytest loads it for every test below it
            if path.startswith("sqlglot/") and path.endswith(".py"):
                return self.nocov  # tests that walk sqlglot's package directories
            return ()  # nothing imports or opens it yet
        if new_path is None and in_suite:
            return ALL  # deleting a module or fixture
        if new_path != old_path and in_suite:
            return ALL  # a renamed module or fixture is gone from where it was imported
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
                if is_trivial(removed, added, gap, spans, bslash, self.pure_enums):
                    continue
                if removed:
                    lines_hit = [n for n, _ in removed]
                else:
                    lines_hit = [gap, gap + 1]
                found: set[int] = set()
                for n in lines_hit:
                    n = min(max(n, 1), last)
                    found.update(self.sets[impact[n - 1]])
                if not found and not removed:
                    # real code inserted where neither neighbour maps to any test (module or
                    # class level, between definitions): it may run at import, be safe
                    return ALL
                out.update(found)
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
            strings = self.strs.get(path, [])
            bslash = self.bslash.get(path, set())
            for start, lines in hunks:
                for removed, added, gap in segments(start, lines):
                    if is_trivial(removed, added, gap, strings, bslash):
                        continue
                    # a change inside one test method hits that test; anything else in
                    # the module (helpers, class attributes, imports) hits the whole file
                    first, last = (removed[0][0], removed[-1][0]) if removed else (gap, gap + 1)
                    owner = None
                    for i in range(0, len(spans), 3):
                        if spans[i] <= first and last <= spans[i + 1]:
                            owner = self.sets[spans[i + 2]]
                            break
                    if owner is None:
                        # helpers such as Validator in test_dialect.py are imported by
                        # other test modules: those break too
                        out.update(whole_file)
                        index = self.timport.get(path)
                        if index is not None:
                            out.update(self.sets[index])
                    else:
                        out.update(owner)
            return out
        index = self.opens.get(path)
        return self.sets[index] if index is not None else ()

    def has_real_change(self, hunks, path):
        strings = self.strs.get(path, [])
        bslash = self.bslash.get(path, set())
        for start, lines in hunks:
            for removed, added, gap in segments(start, lines):
                if not is_trivial(removed, added, gap, strings, bslash):
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
    giants = {t["number"] for t in tests if t["id"] in SKIPPED_GIANTS}
    with gzip.open(MAP, "rt") as f:
        selector = Selector(tests, json.load(f))

    lines = []
    for patch in sorted((DATA / "commits").glob("*.patch")):
        try:
            chosen = selector.select(patch.read_text(encoding="utf-8", errors="replace"))
        except Exception as error:  # never die on one odd patch: run everything
            print(f"{patch.stem}: {error!r}, running all", file=sys.stderr)
            chosen = ALL
        numbers = all_numbers if chosen is ALL else sorted(set(chosen) - giants)
        workers = pack(numbers, duration_ms)
        lines.append(json.dumps({"commit": patch.stem, "workers": workers}, separators=(",", ":")))
        print(f"{patch.stem}: {len(numbers)} tests on {len(workers)} workers", file=sys.stderr)
    sys.stdout.write("\n".join(lines) + ("\n" if lines else ""))


if __name__ == "__main__":
    main()
