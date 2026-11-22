"""Fingerprints that prove a patch cannot change the outcome of sqlglot's two slowest tests.

    python fingerprint.py <root> < {"check": "tpcds" | "max_nodes", "patched": {...},
                                    "baseline": [...], "baseline_seconds": ...}

<root> holds sqlglot/ and the tests/ files the checks read (helpers.py, the tpc-ds
fixtures). Patched modules ({"sqlglot/x.py": "<new source>"}) are served from memory,
every other module from <root> (and its .pyc).

tpcds: tests/test_executor.py::test_execute_tpcds parses each TPC-DS query, pickles it to
a worker, runs sqlglot's executor on it (optimize -> Plan -> Python code generated per
step -> run over the tables) and compares the rows with duckdb's answer to
transpile(sql, write="duckdb"). The check walks the same path on a small slice of every
table and hashes each intermediate: the duckdb SQL, the pickle, the optimized SQL, the
plan, every piece of generated Python code and the rows.

max_nodes: tests/test_parser.py::test_max_nodes, see max_nodes() below.

With "baseline" (the pinned commit's digests, made by build_root.py) the run stops at the
first digest that differs. Prints {"match": bool, "digests": [...]} or {"match": false,
"at": i} on stdout; anything unexpected exits non-zero, which callers treat as no match.
"""

import ast
import csv
import gzip
import hashlib
import importlib.abc
import importlib.machinery
import importlib.util
import json
import os
import pickle
import re
import sys

ROWS = 60
STEP_ID = re.compile(r" \(\d+\)$", re.M)


class PatchedLoader(importlib.machinery.SourceFileLoader):
    """Loads one module from a source string; never reads or writes its .pyc."""

    def __init__(self, fullname, path, source):
        super().__init__(fullname, path)
        self.source = source

    def path_stats(self, path):
        raise OSError("patched source: no bytecode cache")

    def get_data(self, path):
        if path == self.path:
            return self.source.encode("utf-8")
        return super().get_data(path)


class PatchFinder(importlib.abc.MetaPathFinder):
    def __init__(self, patched):
        self.patched = patched  # absolute path -> source

    def find_spec(self, fullname, path, target=None):
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is None or spec.origin not in self.patched:
            return spec
        loader = PatchedLoader(fullname, spec.origin, self.patched[spec.origin])
        return importlib.util.spec_from_file_location(
            fullname, spec.origin, loader=loader,
            submodule_search_locations=spec.submodule_search_locations)


def canonical_plan(text):
    """Plan.__repr__ without the id() of each step, and with the steps listed under each
    "Dependencies:" (a set, so in id() order) sorted."""
    lines = STEP_ID.sub("", text).split("\n")

    def block(i):  # (canonical text of the line at i and everything indented below it, next i)
        depth = len(lines[i]) - len(lines[i].lstrip())
        children, j = [], i + 1
        while j < len(lines) and len(lines[j]) - len(lines[j].lstrip()) > depth:
            child, j = block(j)
            children.append(child)
        if lines[i].strip() == "Dependencies:":
            # Step.to_s indents only the first line of each dependency, which therefore
            # sits at the same depth as "Dependencies:" itself
            while j < len(lines) and len(lines[j]) - len(lines[j].lstrip()) == depth \
                    and lines[j].lstrip().startswith("- "):
                child, j = block(j)
                children.append(child)
            children.sort()
        return "\n".join([lines[i], *children]), j

    out, i = [], 0
    while i < len(lines):
        part, i = block(i)
        out.append(part)
    return "\n".join(out)


def load_tables(fixtures, schema, Table):
    """tests/test_executor.py setUpClass, cut to the first ROWS rows of every table."""
    tables = {}
    for name, columns in schema.items():
        rows, ctypes = [], []
        with gzip.open(os.path.join(fixtures, f"{name}.csv.gz"), "rt", newline="") as f:
            reader = csv.reader(f, delimiter="|")
            next(reader)
            for row in reader:
                if not ctypes:
                    for v in row:
                        try:
                            ctypes.append(type(ast.literal_eval(v)))
                        except (ValueError, SyntaxError):
                            ctypes.append(str)
                rows.append(tuple(None if (t is not str and v == "") else t(v)
                                  for t, v in zip(ctypes, row)))
                if len(rows) >= ROWS:
                    break
        tables[name] = Table(columns=columns, rows=rows)
    return tables


def tpcds(request):
    import sqlglot.executor as executor_module
    import sqlglot.executor.python as python_module
    from sqlglot import find_tables, parse_one, transpile
    from sqlglot.executor import execute
    from sqlglot.executor.table import Table
    from tests.helpers import FIXTURES_DIR, TPCDS_SCHEMA, load_sql_fixture_pairs

    record = []
    compiled = []
    original_optimize = executor_module.optimize
    original_plan = executor_module.Plan

    def optimize(*args, **kwargs):
        expression = original_optimize(*args, **kwargs)
        record.append(expression.sql())
        return expression

    def plan(expression):
        result = original_plan(expression)
        record.append(canonical_plan(repr(result)))
        return result

    def compile_(source, filename, mode, **kwargs):
        compiled.append(source)  # steps run in an id()-dependent order: kept as a multiset
        return compile(source, filename, mode, **kwargs)

    executor_module.optimize = optimize
    executor_module.Plan = plan
    python_module.compile = compile_

    tables = load_tables(os.path.join(FIXTURES_DIR, "optimizer", "tpc-ds"), TPCDS_SCHEMA, Table)
    baseline = request.get("baseline")
    # AGGRESSIVE (experiment): with "baseline_rows" only the duckdb SQL and the rows (or the
    # error) of every query must stay the same, the queries, plans and code may change
    baseline_rows = request.get("baseline_rows")
    digests, rows = [], []
    full_ok = rows_ok = True
    for meta, sql, _ in load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql"):
        if not meta.get("execute"):
            continue
        record.clear()
        compiled.clear()
        duckdb_sql = transpile(sql, write="duckdb")[0]
        record.append(duckdb_sql)
        expression = parse_one(sql)
        data = pickle.dumps(expression)
        record.append(hashlib.sha256(data).hexdigest())
        expression = pickle.loads(data)
        used = {t.name: tables[t.name] for t in find_tables(expression)}
        record.append(repr(sorted(used)))
        try:
            result = execute(expression, schema=TPCDS_SCHEMA, tables=used)
            outcome = [repr(result.columns), repr(result.rows)]
        except Exception as error:  # the same error both sides is the same behaviour
            outcome = [f"{type(error).__name__}: {error}"]
        record.extend(outcome)
        record.extend(sorted(compiled))
        digests.append(_digest(record))
        rows.append(_digest([duckdb_sql, *outcome]))
        i = len(digests) - 1
        if baseline is not None and (i >= len(baseline) or digests[i] != baseline[i]):
            full_ok = False
        if baseline_rows is not None and (i >= len(baseline_rows) or rows[i] != baseline_rows[i]):
            rows_ok = False
        if not full_ok and (baseline_rows is None or not rows_ok):
            print(json.dumps({"match": False, "rows_match": False, "at": i}))
            return
    match = baseline is not None and full_ok and len(digests) == len(baseline)
    rows_match = baseline_rows is not None and rows_ok and len(rows) == len(baseline_rows)
    print(json.dumps({"match": match, "rows_match": rows_match, "digests": digests,
                      "rows": rows}))


def _digest(items):
    digest = hashlib.sha256()
    for item in items:
        digest.update(item.encode("utf-8", "surrogatepass"))
        digest.update(b"\x00")
    return digest.hexdigest()


def max_nodes(request):
    """tests/test_parser.py::TestParser::test_max_nodes with its big query cut from 10000
    columns to WIDTH: every cheap step exactly as the test runs it, and for the big query
    the tokens, the tree, the parser's node count and the max_nodes errors at two limits
    below its size. The test parses the same column 10000 times; these show the patched
    parser treats that column, its count and the limit exactly as the pinned one does.
    A patch that slows the parse down more than SLOWDOWN times is never proven."""
    import time

    from sqlglot import exp, parse_one
    from sqlglot.dialects.dialect import Dialect
    from sqlglot.errors import ParseError

    baseline = request.get("baseline")
    digests = []
    started = time.process_time()

    def step(fn):
        try:
            value = repr(fn())
        except Exception as error:
            value = f"{type(error).__name__}: {error}"
        digests.append(hashlib.sha256(value.encode("utf-8", "surrogatepass")).hexdigest())
        if baseline is not None and (len(digests) > len(baseline)
                                     or digests[-1] != baseline[len(digests) - 1]):
            print(json.dumps({"match": False, "at": len(digests) - 1}))
            sys.exit(0)

    step(lambda: isinstance(parse_one("SELECT 1", max_nodes=1_000_000), exp.Select))
    cols = [f"col{i}" for i in range(500)]
    step(lambda: parse_one(f"SELECT {','.join(cols)} FROM t", max_nodes=1_000_000))

    sql = "SELECT " + ",".join("x" + "=x" * 100 for _ in range(WIDTH))
    dialect = Dialect.get_or_raise(None)
    step(lambda: [(t.token_type, t.text, t.line, t.col, t.start, t.end) for t in dialect.tokenize(sql)])

    def count():
        parser = dialect.parser(max_nodes=10**9)
        tree = parser.parse(dialect.tokenize(sql), sql)
        return parser._node_count, tree

    step(count)
    def limited(limit):
        try:
            return "no error", parse_one(sql, max_nodes=limit)
        except ParseError as error:  # what the test asserts, and the whole error
            message = str(error)
            return ("Maximum number of AST nodes" in message, f"({limit})" in message,
                    message, error.errors)

    for limit in LIMITS:
        step(lambda: limited(limit))

    step(lambda: parse_one("SELECT " + ",".join(f"x={i}" for i in range(100)), max_nodes=5))
    step(lambda: isinstance(parse_one("SELECT 1, 2, 3"), exp.Select))
    seconds = time.process_time() - started
    limit = request.get("baseline_seconds")
    match = baseline is not None and digests == baseline and \
        (limit is None or seconds <= SLOWDOWN * limit + 0.5)
    print(json.dumps({"match": match, "digests": digests, "seconds": seconds}))


WIDTH = 150                # columns of the big query (the test uses 10000)
LIMITS = (10_000, 20_000)  # both below the cut query's ~45k nodes, like the test's 50000
SLOWDOWN = 4.0


def main():
    root = os.path.abspath(sys.argv[1])
    request = json.load(sys.stdin)
    patched = {os.path.join(root, rel): src for rel, src in request.get("patched", {}).items()}
    sys.meta_path.insert(0, PatchFinder(patched))
    sys.path.insert(0, root)
    {"tpcds": tpcds, "max_nodes": max_nodes}[request.get("check", "tpcds")](request)


if __name__ == "__main__":
    main()
