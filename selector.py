"""Blast Radius selector: pick the tests a patch can break, split them over workers.

All heavy lifting happened offline (tools/build_map.py): for every line of sqlglot at the
pinned commit, data/covmap.json.gz holds the tests to run if that line changes. Here we
only parse each diff, drop comment/whitespace-only edits, look lines up and bin-pack.
Standard library only; stdout carries answers only, logs go to stderr.
"""

from __future__ import annotations

import ast
import gzip
import io
import json
import os
import re
import subprocess
import sys
import time
import tokenize
from pathlib import Path

DATA = Path("/data")
MAP = Path(__file__).resolve().parent / "data" / "covmap.json.gz"
MAX_WORKERS = 8
# tpc-ds fingerprint (fingerprint.py, built by build_root.py): only present in the image
APP = Path(__file__).resolve().parent
FP_ROOT = APP / "root"
FP_BASELINE = APP / "data" / "fp_baseline.json"
TPCDS = "tests/test_executor.py::TestExecutor::test_execute_tpcds"
MAX_NODES = "tests/test_parser.py::TestParser::test_max_nodes"
# check name in fingerprint.py -> (test it can prove unaffected, least saving worth its
# CPU, seconds before it is killed: a normal run takes up to ~9 s and ~4 s)
CHECKS = {"tpcds": (TPCDS, 5000, 15.0), "max_nodes": (MAX_NODES, 2000, 6.0)}
# Executor and planner edits go through the fingerprint too: it runs the executor on a
# slice of every table, and on 39 labelled executor/planner mutants it never matched
# when the test failed (17 caught, 19 proven, 3 kept needlessly).
FP_EXCLUDED = ()
FP_PARALLEL = 2
FP_LAUNCH_BY = 110.0  # seconds after start: no new fingerprint is started later
FP_KILL_AT = 150.0    # seconds after start: unfinished fingerprints keep the test
WORKER_COST_MS = 2000

HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
# Files every test module imports: any real change there can break anything.
SHARED_TEST_FILES = {"tests/__init__.py", "tests/helpers.py", "tests/dialects/__init__.py", "tests/conftest.py"}

ALL = "ALL"


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


def apply_hunks(source: str, hunks) -> str:
    """The file after the patch; raises ValueError when a context or removed line differs."""
    old = source.split("\n")
    new: list[str] = []
    pos = 0
    for start, lines in hunks:
        target = start - 1
        if target < pos or target > len(old):
            raise ValueError("hunk out of order")
        new.extend(old[pos:target])
        pos = target
        for line in lines:
            tag, text = line[:1], line[1:]
            if tag == "\\":
                continue
            if tag in (" ", "-"):
                if pos >= len(old) or old[pos] != text:
                    raise ValueError("patch does not apply")
                if tag == " ":
                    new.append(text)
                pos += 1
            else:
                new.append(text)
    new.extend(old[pos:])
    return "\n".join(new)


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


def reindented(removed, added):
    """A moved statement can run on a new path or prevent the module from importing."""
    old = [line for _, line in removed if code_part(line).strip()]
    new = [line for line in added if code_part(line).strip()]
    return len(old) == len(new) and any(
        a[:len(a) - len(a.lstrip())] != b[:len(b) - len(b.lstrip())]
        for a, b in zip(old, new)
    )


def literal_mask(spans, old, new):
    for start, end, mask in spans:
        suffix = old[end:]
        if not new.startswith(old[:start]) or not new.endswith(suffix):
            continue
        value = new[start:len(new) - len(suffix)]
        # One literal token only. Expressions, comments and changes to the signature
        # must use the ordinary line map. Check token boundaries in the resulting line.
        if not value or value == old[start:end]:
            continue
        try:
            before = ast.literal_eval(old[start:end])
            after = ast.parse(value, mode="eval").body
            if isinstance(after, ast.Constant) and type(after.value) is type(before):
                if ast.get_source_segment(value, after) == value:
                    return mask
        except (SyntaxError, ValueError):
            pass
    return None


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
        self.literal_reads = cmap.get("literal_reads", {})
        self.bool_swaps = cmap.get("bool_swaps", {})
        self.strs = cmap["strs"]
        self.bslash = {p: set(v) for p, v in cmap["bslash"].items()}
        self.opens = cmap["opens"]
        self.fixture_fields = cmap.get("fixture_fields", {})
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
        files = parse_patch(patch_text)
        if len(files) == 1:
            old, new, hunks = files[0]
            rows = self.literal_reads.get(old)
            if old == new and rows:
                changes = [seg for start, lines in hunks for seg in segments(start, lines)]
                if len(changes) == 1:
                    removed, added, _ = changes[0]
                    if len(removed) == len(added) == 1:
                        n, original = removed[0]
                        mask = literal_mask(rows.get(str(n), ()), original, added[0])
                        if mask is not None:
                            # Both this and the ordinary selection hold every test the
                            # edit can reach, so their intersection does too.
                            readers = set(self.sets[mask]).union(self.unmapped)
                            ordinary = self.select_file(old, new, hunks)
                            return readers if ordinary is ALL else readers & set(ordinary)
        safe = self.swap_unaffected(files)
        for old_path, new_path, hunks in files:
            result = self.select_file(old_path, new_path, hunks)
            if result is ALL:
                if not safe:
                    return ALL
                result = [t["number"] for t in self.tests]
            chosen.update(result)
        return chosen - safe if safe else chosen

    def swap_unaffected(self, files):
        """One `and` swapped for `or` (or back) in a condition: the tests the map proved
        cannot notice (every evaluation they made keeps its truth value). Empty otherwise."""
        if len(files) != 1:
            return set()
        old, new, hunks = files[0]
        rows = self.bool_swaps.get(old)
        if old != new or not rows:
            return set()
        changes = [seg for start, lines in hunks for seg in segments(start, lines)]
        if len(changes) != 1:
            return set()
        removed, added, _ = changes[0]
        if len(removed) != 1 or len(added) != 1:
            return set()
        n, original = removed[0]
        for col, word, index in rows.get(str(n), ()):
            other = "or" if word == "and" else "and"
            if original[col:col + len(word)] == word and \
                    added[0] == original[:col] + other + original[col + len(word):]:
                return set(self.sets[index])
        return set()

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
        if in_suite and path.endswith(".py"):
            if any(reindented(r, a) for start, lines in hunks for r, a, _ in segments(start, lines)):
                return ALL
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
        if index is None:
            return ()
        readers = self.sets[index]
        if path not in self.fixture_fields or not hunks:
            return readers
        roles, omitted = self.fixture_fields[path]
        unused = set(readers)
        for start, lines in hunks:
            for removed, added, _ in segments(start, lines):
                if not removed or len(removed) != len(added):
                    return readers
                for (n, old), new in zip(removed, added):
                    # Preserve the loader's statement delimiters, comment filtering
                    # and metadata prefix. Only replace ordinary SQL within a field.
                    if not 1 <= n <= len(roles) or roles[n - 1] == "0":
                        return readers
                    if any(not t.strip() or t.lstrip().startswith(("#", "--")) or ";" in t
                           or t.splitlines() != [t] or "\x00" in t for t in (old, new)):
                        return readers
                    unused.intersection_update(self.sets[omitted[int(roles[n - 1])]])
        return set(readers) - unused

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


def cost_ms(numbers, duration_ms) -> int:
    workers = pack(numbers, duration_ms)
    if not workers:
        return 0
    return max(sum(duration_ms[n] for n in w) for w in workers) + WORKER_COST_MS * len(workers)


def fingerprint_request(text: str):
    """{path: patched source} when the tpc-ds fingerprint can prove the patch harmless to
    test_execute_tpcds: only existing sqlglot modules edited in place. None otherwise."""
    patched = {}
    for old, new, hunks in parse_patch(text):
        if old is None or old != new or not old.startswith("sqlglot/") or not old.endswith(".py"):
            return None
        if (FP_EXCLUDED and old.startswith(FP_EXCLUDED)) or old in patched:
            return None
        source = (FP_ROOT / old).read_text(encoding="utf-8")
        patched[old] = apply_hunks(source, hunks)
    return patched or None


def run_fingerprints(jobs, started):
    """jobs: [(key, check, patched)] -> {key: True when the check's fingerprint equals the
    pinned one}.
    At most FP_PARALLEL at a time; anything that fails, times out or runs past the
    deadlines counts as not proven."""
    baseline = json.loads(FP_BASELINE.read_text())
    env = dict(os.environ, PYTHONHASHSEED="0", PYTHONDONTWRITEBYTECODE="1")
    script = str(APP / "fingerprint.py")
    proven, running, queue = {}, [], list(jobs)
    while queue or running:
        now = time.monotonic() - started
        while queue and len(running) < FP_PARALLEL and now < FP_LAUNCH_BY:
            key, check, patched = queue.pop(0)
            proc = subprocess.Popen([sys.executable, script, str(FP_ROOT)],
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, env=env, text=True)
            pinned = baseline[check]
            proc.stdin.write(json.dumps({"patched": patched, "check": check,
                                         "baseline": pinned["digests"],
                                         "baseline_rows": pinned.get("rows"),
                                         "baseline_seconds": pinned.get("seconds")}))
            proc.stdin.close()
            running.append((key, proc, time.monotonic() + CHECKS[check][2]))
        if now >= FP_LAUNCH_BY:
            queue.clear()
        still = []
        for key, proc, deadline in running:
            if proc.poll() is None:
                if time.monotonic() > deadline or time.monotonic() - started > FP_KILL_AT:
                    proc.kill()
                    proc.wait()
                    print(f"{key}: fingerprint timed out", file=sys.stderr)
                else:
                    still.append((key, proc, deadline))
                continue
            out = proc.stdout.read()
            try:
                answer = json.loads(out)
                # AGGRESSIVE (experiment): tpc-ds counts as proven when every query gives
                # the same rows on the slice, even through a different plan or code
                proven[key] = proc.returncode == 0 and (answer.get("match") is True
                                                        or answer.get("rows_match") is True)
            except ValueError:
                proven[key] = False
        running = still
        if running:
            time.sleep(0.02)
    return proven


def main() -> None:
    started = time.monotonic()
    tests = json.loads((DATA / "tests.json").read_text())["tests"]
    duration_ms = {t["number"]: int(t["seconds"] * 1000 + 0.5) for t in tests}
    all_numbers = [t["number"] for t in tests]
    number_of = {t["id"]: t["number"] for t in tests}
    checks = {name: (number_of[test], least) for name, (test, least, _) in CHECKS.items()
              if test in number_of}
    fingerprint = FP_ROOT.is_dir() and FP_BASELINE.is_file() and bool(checks)
    with gzip.open(MAP, "rt") as f:
        selector = Selector(tests, json.load(f))

    results, jobs = [], []
    for patch in sorted((DATA / "commits").glob("*.patch")):
        text = patch.read_text(encoding="utf-8", errors="replace")
        try:
            chosen = selector.select(text)
        except Exception as error:  # never die on one odd patch: run everything
            print(f"{patch.stem}: {error!r}, running all", file=sys.stderr)
            chosen = ALL
        numbers = all_numbers if chosen is ALL else sorted(chosen)
        results.append((patch.stem, numbers))
        present = {name: n for name, (n, _) in checks.items() if n in set(numbers)} \
            if fingerprint else {}
        if not present:
            continue
        try:
            patched = fingerprint_request(text)
        except Exception as error:  # a patch we cannot apply: keep the tests
            print(f"{patch.stem}: {error!r}, no fingerprint", file=sys.stderr)
            patched = None
        if not patched:
            continue
        # each check is worth running when dropping its test saves enough even with
        # the other candidates already gone
        none = cost_ms([n for n in numbers if n not in present.values()], duration_ms)
        for name, n in present.items():
            others = {m for m in present.values() if m != n}
            saving = cost_ms([m for m in numbers if m not in others], duration_ms) - none
            if saving >= checks[name][1]:
                jobs.append((saving, (patch.stem, name), name, patched))
    jobs.sort(key=lambda job: -job[0])  # the biggest savings first, in case time runs out
    proven = run_fingerprints([(key, name, patched) for _, key, name, patched in jobs],
                              started) if jobs else {}

    lines = []
    for stem, numbers in results:
        dropped = {checks[name][0] for (key, name), ok in proven.items() if key == stem and ok}
        if dropped:
            numbers = [n for n in numbers if n not in dropped]
            names = ", ".join(t["id"].split("::")[-1] for t in tests if t["number"] in dropped)
            print(f"{stem}: fingerprint unchanged, dropped {names}", file=sys.stderr)
        workers = pack(numbers, duration_ms)
        lines.append(json.dumps({"commit": stem, "workers": workers}, separators=(",", ":")))
        print(f"{stem}: {len(numbers)} tests on {len(workers)} workers", file=sys.stderr)
    sys.stdout.write("\n".join(lines) + ("\n" if lines else ""))


if __name__ == "__main__":
    main()
