"""Fingerprint of everything sqlglot computes for tests/test_executor.py::test_execute_tpcds.

    python fingerprint.py <root> < {"patched": {"sqlglot/x.py": "<new source>", ...}}

<root> holds sqlglot/ and the tests/ files the test reads (helpers.py, the tpc-ds fixtures).
Patched modules are served from memory, every other module from <root> (and its .pyc).

The test parses each TPC-DS query, pickles it to a worker, runs sqlglot's executor on it
(optimize -> Plan -> Python code generated per step -> run over the tables) and compares
the rows with duckdb's answer to transpile(sql, write="duckdb"). This script walks the same
path on a small slice of every table and hashes each intermediate: the duckdb SQL, the
pickle, the optimized tree (with types), the plan, every piece of generated Python code
and the rows. When the digest equals the one of the pinned commit, the patched sqlglot
fed the test exactly the same queries, plans and code as the unpatched one.

With "baseline" (the pinned commit's per-query digests) in the request it stops at the
first query that differs. Prints {"match": bool, "digests": [...]} or {"match": false,
"at": i} on stdout; anything unexpected exits non-zero.
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


def main():
    root = os.path.abspath(sys.argv[1])
    request = json.load(sys.stdin)
    patched = {os.path.join(root, rel): src for rel, src in request.get("patched", {}).items()}
    sys.meta_path.insert(0, PatchFinder(patched))
    sys.path.insert(0, root)

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
    digests = []
    for meta, sql, _ in load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql"):
        if not meta.get("execute"):
            continue
        record.clear()
        compiled.clear()
        record.append(transpile(sql, write="duckdb")[0])
        expression = parse_one(sql)
        data = pickle.dumps(expression)
        record.append(hashlib.sha256(data).hexdigest())
        expression = pickle.loads(data)
        used = {t.name: tables[t.name] for t in find_tables(expression)}
        record.append(repr(sorted(used)))
        try:
            result = execute(expression, schema=TPCDS_SCHEMA, tables=used)
            record.append(repr(result.columns))
            record.append(repr(result.rows))
        except Exception as error:  # the same error both sides is the same behaviour
            record.append(f"{type(error).__name__}: {error}")
        record.extend(sorted(compiled))
        digest = hashlib.sha256()
        for item in record:
            digest.update(item.encode("utf-8", "surrogatepass"))
            digest.update(b"\x00")
        digests.append(digest.hexdigest())
        if baseline is not None and (len(digests) > len(baseline)
                                     or digests[-1] != baseline[len(digests) - 1]):
            print(json.dumps({"match": False, "at": len(digests) - 1}))
            return
    match = baseline is not None and digests == baseline
    print(json.dumps({"match": match, "digests": digests}))


if __name__ == "__main__":
    main()
