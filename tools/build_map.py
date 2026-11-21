"""Build data/covmap.json.gz from two per-test coverage runs and sqlglot's pinned source.

Usage (repo root):
    python3 tools/build_map.py sqlglot-src sample/tests.json build/lazy build/pre data/covmap.json.gz

Everything here is derived from the pinned commit only (coverage, fixture opens, source
structure). Nothing looks at patches or answers.

For every line of every sqlglot/*.py file we precompute "the tests to run if this line
changes", so the selector in the container only does lookups. Output layout:

  tests:  [test id, ...]                       test sets below index into this
  sets:   [[test index, ...], ...]             deduplicated; sets[0] is empty
  impact: {src path: [set index for line 1, line 2, ..., one past the last line]}
  strs:   {src path: [start, end, ...]}        multi-line string tokens (line spans)
  bslash: {src path: [line, ...]}              lines ending in a backslash continuation
  opens:  {data file under tests/: set index}  tests that open it
  tspans: {test file: [start, end, set index, ...]}  test method spans
  nocov:  set index of tests no coverage context ever saw (cheap, import-sensitive)
"""

from __future__ import annotations

import ast
import glob
import gzip
import io
import json
import os
import sqlite3
import sys
import tokenize
from collections import defaultdict

FAMILY_DIRS = ("dialects", "parsers", "generators", "typing")
# A name assigned in more places than this is polymorphic (arg_types, FUNCTIONS, ...):
# its readers are generic code, so they say nothing about which tests one definition hits.
MAX_DEFINITIONS = 3
MAX_USAGE_LINES = 60
# A core setting read in more places than this is treated as unknowable (skipped here and
# left to the other rules); real flags are read in a handful of places.
MAX_READER_SITES = 300
SWEEPER_DIALECTS = 25
SWEEPER_SECONDS = 1.0


# --------------------------------------------------------------------------- coverage
def numbits_to_lines(blob: bytes):
    for byte_index, byte in enumerate(blob):
        if byte:
            for bit in range(8):
                if byte & (1 << bit):
                    yield byte_index * 8 + bit


def load_run(run_dir, index_of, by_class):
    """-> (cov {path: {line: bitmask}}, import_lines {path: set}, opens {path: bitmask})"""

    def charged(context):
        if not context:
            return None
        nodeid, _, phase = context.rpartition("|")
        nodeid = nodeid.split("[", 1)[0]
        if nodeid not in index_of:
            raise SystemExit(f"unknown test context {context!r}")
        if phase == "run":
            return 1 << index_of[nodeid]
        return by_class[nodeid.rsplit("::", 1)[0]]  # class-level setup/teardown

    db = sqlite3.connect(os.path.join(run_dir, ".coverage"))
    files = {fid: os.path.relpath(p, "/src") for fid, p in db.execute("select id, path from file")}
    contexts = {cid: charged(c) for cid, c in db.execute("select id, context from context")}
    cov = defaultdict(lambda: defaultdict(int))
    imports = defaultdict(set)
    for fid, cid, blob in db.execute("select file_id, context_id, numbits from line_bits"):
        path, mask = files[fid], contexts[cid]
        for line in numbits_to_lines(blob):
            if mask is None:
                imports[path].add(line)
            else:
                cov[path][line] |= mask

    opens = defaultdict(int)
    for log in glob.glob(os.path.join(run_dir, "opens-*.json")):
        for context, paths in json.load(open(log)).items():
            mask = charged(context) or 0
            for p in paths:
                if "__pycache__" not in p:
                    opens[p] |= mask

    # "sqlglot.expressions.aggregate:Last" -> tests that built a Last object
    built = defaultdict(int)
    for log in glob.glob(os.path.join(run_dir, "classes-*.json")):
        for context, keys in json.load(open(log)).items():
            mask = charged(context) or 0
            for key in keys:
                built[key.rsplit(":", 1)[1].rsplit(".", 1)[-1]] |= mask
                built[key] |= mask  # exact "module:qualname" as well
    return cov, imports, opens, built


# --------------------------------------------------------------------------- source
class Unit:
    """The code a changed line belongs to, and how a change there reaches tests."""

    __slots__ = ("start", "end", "kind", "node", "func", "classes", "parent")

    def __init__(self, start, end, kind, node, func, classes, parent):
        self.start, self.end, self.kind = start, end, kind
        self.node, self.func, self.classes, self.parent = node, func, classes, parent


def first_line(node):
    decorators = getattr(node, "decorator_list", None) or []
    return min([node.lineno] + [d.lineno for d in decorators])


def build_units(tree, n_lines):
    owner = [None] * (n_lines + 2)

    def visit(stmts, func, classes, parent):
        for s in stmts:
            start, end = first_line(s), s.end_lineno
            body = getattr(s, "body", None)
            is_def = isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef))
            is_class = isinstance(s, ast.ClassDef)
            runtime = func is not None
            if isinstance(body, list) and body and isinstance(body[0], ast.AST):
                header_end = max(s.lineno, body[0].lineno - 1)
                kind = "def" if is_def else "class" if is_class else ("rt" if runtime else "imp")
                unit = Unit(start, header_end, kind, s, func, classes, parent)
                for line in range(start, end + 1):  # else:, comments, blanks -> header
                    owner[line] = unit
                inner_func = s if is_def else func
                inner_classes = classes + [s.name] if is_class else classes
                for field in ("body", "orelse", "finalbody"):
                    visit(getattr(s, field, None) or [], inner_func, inner_classes, unit)
                # except clauses and match cases: their own header, then their body
                for clause in (getattr(s, "handlers", None) or []) + (getattr(s, "cases", None) or []):
                    clause_start = clause.lineno if hasattr(clause, "lineno") else clause.pattern.lineno
                    clause_unit = Unit(clause_start, max(clause_start, clause.body[0].lineno - 1),
                                       "rt" if inner_func is not None else "imp",
                                       clause, inner_func, inner_classes, unit)
                    for line in range(clause_start, clause.body[-1].end_lineno + 1):
                        owner[line] = clause_unit
                    visit(clause.body, inner_func, inner_classes, clause_unit)
            else:
                unit = Unit(start, end, "rt" if runtime else "imp", s, func, classes, parent)
                for line in range(start, end + 1):
                    owner[line] = unit

    visit(tree.body, None, [], None)
    return owner


def string_spans_and_backslashes(source):
    spans, bslash = [], []
    lines = source.splitlines()
    tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    starts = {}
    for tok in tokens:
        name = tokenize.tok_name[tok.type]
        if name == "STRING" and tok.end[0] > tok.start[0]:
            spans += [tok.start[0], tok.end[0]]
        elif name == "FSTRING_START":
            starts.setdefault("f", []).append(tok.start[0])
        elif name == "FSTRING_END":
            s = starts["f"].pop()
            if tok.end[0] > s:
                spans += [s, tok.end[0]]
    in_string = set()
    for s, e in zip(spans[::2], spans[1::2]):
        in_string.update(range(s + 1, e + 1))
    for i, text in enumerate(lines, start=1):
        if text.rstrip().endswith("\\") and i not in in_string:
            bslash.append(i)
    return spans, bslash


def loaded_names(node):
    names = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name):
            names.add(sub.id)
        elif isinstance(sub, ast.Attribute):
            names.add(sub.attr)
    return names


def header_names(node):
    """Names a statement loads outside its nested body (for compound statements)."""
    names = set()
    for field, value in ast.iter_fields(node):
        if field in ("body", "orelse", "finalbody", "handlers", "cases"):
            continue
        for item in value if isinstance(value, list) else [value]:
            if isinstance(item, ast.AST):
                names |= loaded_names(item)
    return names


def assigned_names(node):
    names = set()
    targets = []
    if isinstance(node, ast.Assign):
        targets = node.targets
    elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
        targets = [node.target]
    elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        names.add(node.name)
    elif isinstance(node, (ast.Import, ast.ImportFrom)):
        names.update((a.asname or a.name).split(".")[0] for a in node.names)
    for t in targets:
        for sub in ast.walk(t):
            if isinstance(sub, ast.Name):
                names.add(sub.id)
            elif isinstance(sub, ast.Attribute):
                names.add(sub.attr)
    return names


# --------------------------------------------------------------------------- main
def main(src, tests_json, lazy_dir, pre_dir, out, serial_dir=None):
    tests = json.load(open(tests_json))["tests"]
    test_ids = [t["id"] for t in tests]
    index_of = {tid: i for i, tid in enumerate(test_ids)}
    by_class = defaultdict(int)
    by_file = defaultdict(int)
    for i, t in enumerate(tests):
        by_class[t["id"].rsplit("::", 1)[0]] |= 1 << i
        by_file[t["file"]] |= 1 << i
    all_tests = (1 << len(tests)) - 1

    cov_lazy, imp_lazy, opens_lazy, built_lazy = load_run(lazy_dir, index_of, by_class)
    cov_pre, imp_pre, opens_pre, built_pre = load_run(pre_dir, index_of, by_class)
    built = defaultdict(int)
    for d in (built_lazy, built_pre):
        for name, mask in d.items():
            built[name] |= mask

    # Runtime coverage: the pre-imported run is clean; the lazy run adds what lazy
    # imports exercise, minus its noise on lines that really run at import time.
    cov = defaultdict(dict)
    for path in set(cov_lazy) | set(cov_pre):
        imp = imp_pre.get(path, set())
        lines = set(cov_lazy.get(path, ())) | set(cov_pre.get(path, ()))
        for line in lines:
            mask = cov_pre.get(path, {}).get(line, 0)
            if line not in imp:
                mask |= cov_lazy.get(path, {}).get(line, 0)
            if mask:
                cov[path][line] = mask
    # Order-dependent lines: run-once code behind a cache (generator._DISPATCH_CACHE,
    # Dialect._classes ...) is charged only to whichever test got there first, so its test
    # set changes with the schedule. A serial run vs the 16-worker run exposes it.
    order_dependent = defaultdict(set)
    if serial_dir:
        cov_serial, imp_serial, _, _ = load_run(serial_dir, index_of, by_class)
        for path in set(cov_pre) | set(cov_serial):
            imp = imp_pre.get(path, set()) | imp_serial.get(path, set())
            a, b = cov_pre.get(path, {}), cov_serial.get(path, {})
            for line in set(a) | set(b):
                if line not in imp and a.get(line, 0) != b.get(line, 0):
                    order_dependent[path].add(line)
                    # the serial run saw it too: keep its tests as well
                    cov.setdefault(path, {})[line] = cov.get(path, {}).get(line, 0) | b.get(line, 0)
        print(f"order-dependent lines: {sum(map(len, order_dependent.values()))} in "
              f"{len(order_dependent)} files", file=sys.stderr)

    covered_any = 0
    for lines in cov.values():
        for m in lines.values():
            covered_any |= m
    nocov = all_tests & ~covered_any

    # ---- parse every source file once
    sources = {}
    for path in sorted(glob.glob(os.path.join(src, "sqlglot", "**", "*.py"), recursive=True)):
        rel = os.path.relpath(path, src)
        sources[rel] = open(path, encoding="utf-8").read()
    trees = {rel: ast.parse(text) for rel, text in sources.items()}

    # name -> definition count, and name -> [(path, line)] load sites
    definitions = defaultdict(int)
    usages = defaultdict(list)
    for rel, tree in trees.items():
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                definitions[node.name] += 1
            elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                for n in assigned_names(node):
                    definitions[n] += 1
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                usages[node.id].append((rel, node.lineno))
            elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
                usages[node.attr].append((rel, node.lineno))

    def lines_cov(path, start, end):
        m = 0
        c = cov.get(path, {})
        for line in range(start, end + 1):
            m |= c.get(line, 0)
        return m

    file_cov = {rel: lines_cov(rel, 1, len(text.splitlines())) for rel, text in sources.items()}

    # ---- dialect families: a dialect plus every dialect that inherits from it
    dialect_names = {
        os.path.basename(p)[:-3]
        for p in sources
        if p.startswith("sqlglot/dialects/") and not p.endswith(("__init__.py", "dialect.py"))
    }
    class_to_dialect = {}
    bases_of = {}
    for name in dialect_names:
        tree = trees.get(f"sqlglot/dialects/{name}.py")
        for node in tree.body if tree else []:
            if isinstance(node, ast.ClassDef):
                class_to_dialect[node.name] = name
                bases_of[node.name] = [b.id if isinstance(b, ast.Name) else getattr(b, "attr", "") for b in node.bases]
    children = defaultdict(set)
    for cls, bases in bases_of.items():
        for b in bases:
            if b in class_to_dialect and class_to_dialect[b] != class_to_dialect[cls]:
                children[class_to_dialect[b]].add(class_to_dialect[cls])

    def descendants(name, graph=children):
        seen, todo = {name}, [name]
        while todo:
            for c in graph[todo.pop()]:
                if c not in seen:
                    seen.add(c)
                    todo.append(c)
        return seen

    # ---- every class in sqlglot -> its subclasses (by name), for "who built this class"
    subclasses = defaultdict(set)
    for tree in trees.values():
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                for b in node.bases:
                    base = b.id if isinstance(b, ast.Name) else getattr(b, "attr", None)
                    if base:
                        subclasses[base].add(node.name)
    built_cache = {}
    expr_classes = descendants("Expr", subclasses)

    def built_tests(class_name):
        """Tests that built an object of this class or of any subclass of it."""
        if class_name not in built_cache:
            m = 0
            for c in descendants(class_name, subclasses):
                m |= built.get(c, 0)
            built_cache[class_name] = m
        return built_cache[class_name]

    def family_tests(dialect):
        m = 0
        for d in descendants(dialect):
            for folder in FAMILY_DIRS:
                m |= file_cov.get(f"sqlglot/{folder}/{d}.py", 0)
            m |= by_file.get(f"tests/dialects/test_{d}.py", 0)
            # tests that used this dialect at all, e.g. only as a validate_all write
            # target through shared Generator code (Drill.TYPED_DIVISION -> test_mysql)
            for folder in ("dialects", "parsers", "generators"):
                tree = trees.get(f"sqlglot/{folder}/{d}.py")
                for node in tree.body if tree else []:
                    if isinstance(node, ast.ClassDef):
                        m |= built.get(f"sqlglot.{folder}.{d}:{node.name}", 0) & ~sweepers
        return m

    # Tests that merely sweep every dialect (test_generator_memory builds 1000 generators
    # per dialect and measures memory; it never parses or generates SQL) would otherwise be
    # charged to every dialect setting. Expensive sweepers keep only the coverage-based
    # family rule; cheap ones (test_enum, 0.03 s) stay in, they cost nothing.
    dialect_users = defaultdict(set)
    for key, mask in built.items():
        module, _, _ = key.partition(":")
        parts = module.split(".")
        if len(parts) == 3 and parts[1] in ("dialects", "parsers", "generators") and parts[2] in dialect_names:
            for i in range(len(tests)):
                if mask >> i & 1:
                    dialect_users[i].add(parts[2])
    sweepers = 0
    for i, used in dialect_users.items():
        if len(used) >= SWEEPER_DIALECTS and tests[i]["seconds"] > SWEEPER_SECONDS:
            sweepers |= 1 << i
    print(f"dialect sweepers left to the coverage rule: "
          f"{[tests[i]['id'].rsplit('::', 1)[-1] for i in range(len(tests)) if sweepers >> i & 1]}",
          file=sys.stderr)

    family_cache = {}

    def dialect_of(rel):
        parts = rel.split("/")
        if len(parts) == 3 and parts[1] in FAMILY_DIRS and parts[2][:-3] in dialect_names:
            return parts[2][:-3]
        return None

    def name_match_tests(rel):
        base = os.path.basename(rel)[:-3]
        return by_file.get(f"tests/dialects/test_{base}.py", 0) | by_file.get(f"tests/test_{base}.py", 0)

    def usage_tests(names, skip_path, skip_start, skip_end):
        m = 0
        for n in names:
            if definitions.get(n, 0) > MAX_DEFINITIONS:
                continue
            sites = usages.get(n, ())
            if len(sites) > MAX_USAGE_LINES:
                continue
            for path, line in sites:
                if path == skip_path and skip_start <= line <= skip_end:
                    continue
                m |= cov.get(path, {}).get(line, 0)
        return m

    def import_time_tests(rel, unit, names):
        dialect = dialect_of(rel)
        m = nocov | name_match_tests(rel)
        if dialect:
            if dialect not in family_cache:
                family_cache[dialect] = family_tests(dialect)
            m |= family_cache[dialect]
        m |= usage_tests(names | set(unit.classes[-1:]), rel, unit.start, unit.end)
        # a class-level setting (arg_types, is_var_len_args, ...) or a class header can only
        # reach tests that build objects of that class or of one of its subclasses
        # (expression classes only: dialect classes are covered by the dialect-usage rule
        #  above, and their nested "class Tokenizer:" would match every tokenizer by name)
        owners = list(unit.classes[-1:])
        if isinstance(unit.node, ast.ClassDef):
            owners.append(unit.node.name)
        if rel.startswith("sqlglot/expressions/"):
            for owner in owners:
                m |= built_tests(owner)
        if not dialect and not (m & ~nocov):
            # nothing specific found: everything that runs the enclosing class or file
            m |= file_cov.get(rel, 0)
        return m

    reader_cache = {}

    def reader_tests(rel, names, skip_start, skip_end):
        """For a setting defined in a core (non-dialect) module: every place that reads it.
        A read at run time charges the tests that execute that line; a read while modules
        load (metaclasses, __init_subclass__, other class bodies) bakes the value into
        classes every test may use, so it charges every test."""
        m = 0
        for n in names:
            key = (rel, n, skip_start)
            if key not in reader_cache:
                r = 0
                sites = usages.get(n, ())
                if len(sites) <= MAX_READER_SITES:
                    for path, line in sites:
                        if path == rel and skip_start <= line <= skip_end:
                            continue
                        if line in imp_pre.get(path, ()):
                            r = all_tests
                            break
                        r |= cov.get(path, {}).get(line, 0)
                reader_cache[key] = r
            m |= reader_cache[key]
        return m

    import_exec_cache = {}

    def import_exec_tests(rel, func):
        """Tests that can depend on what `func` computed while modules were loading. In a
        core module that is everything (it may run for every class or template sqlglot
        builds on import); in a dialect module, that dialect's family and its callers."""
        key = (rel, func.name if func is not None else None)
        if key in import_exec_cache:
            return import_exec_cache[key]
        if not dialect_of(rel):
            import_exec_cache[key] = all_tests
            return all_tests
        files = {rel}
        if func is not None and definitions.get(func.name, 0) <= MAX_DEFINITIONS:
            for path, line in usages.get(func.name, ()):
                if line in imp_pre.get(path, ()):
                    files.add(path)
        m = nocov
        for path in files:
            m |= file_cov.get(path, 0) | name_match_tests(path)
            dialect = dialect_of(path)
            if dialect:
                if dialect not in family_cache:
                    family_cache[dialect] = family_tests(dialect)
                m |= family_cache[dialect]
        import_exec_cache[key] = m
        return m

    functions_of = {}

    def function_at(path, line):
        """Innermost def containing `line` in `path`, or None."""
        if path not in functions_of:
            functions_of[path] = sorted(
                (n for n in ast.walk(trees[path]) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))),
                key=lambda n: n.end_lineno - n.lineno,
            ) if path in trees else []
        for fn in functions_of[path]:
            if fn.lineno <= line <= fn.end_lineno:
                return fn
        return None

    escalate_cache = {}

    def escalate(path, func, depth=0):
        """Everyone who relies on what an order-dependent (cached, run-once) piece of `func`
        computed: the tests running func's stable lines, i.e. calling it every time. When
        func itself only runs on a cache miss (_build_dispatch), climb to its callers."""
        key = (path, func.lineno)
        if key in escalate_cache:
            return escalate_cache[key]
        escalate_cache[key] = all_tests  # recursion guard: be safe on cycles
        od = order_dependent.get(path, set())
        c = cov.get(path, {})
        m = 0
        # If func's own first statement is order-dependent, func itself only runs on a
        # cache miss (_build_dispatch, Dialect._try_load): its other stable lines are rare
        # side branches (plugin loading), so climb to the callers instead.
        if func.body[0].lineno not in od:
            for line in range(func.body[0].lineno, func.end_lineno + 1):
                if line not in od:
                    m |= c.get(line, 0)
        if not m:
            if depth >= 3 or definitions.get(func.name, 0) > MAX_DEFINITIONS:
                m = file_cov.get(path, 0) or all_tests
            else:
                for site_path, site_line in usages.get(func.name, ()):
                    caller = function_at(site_path, site_line)
                    m |= escalate(site_path, caller, depth + 1) if caller else file_cov.get(site_path, 0)
                m = m or file_cov.get(path, 0) or all_tests
        escalate_cache[key] = m
        return m

    impact = {}
    strs = {}
    bslash = {}
    for rel, text in sources.items():
        n_lines = len(text.splitlines())
        owner = build_units(trees[rel], n_lines)
        spans, bs = string_spans_and_backslashes(text)
        if spans:
            strs[rel] = spans
        if bs:
            bslash[rel] = bs
        cache = {}

        def unit_tests(unit):
            if unit is None:
                return 0
            key = id(unit)
            if key in cache:
                return cache[key]
            node = unit.node
            if unit.kind == "rt":
                m = lines_cov(rel, unit.start, unit.end)
                if not m:
                    # unreached here; fall back to whoever reaches the enclosing block
                    parent = unit.parent
                    while parent is not None and not m:
                        if parent.kind == "def":
                            m = lines_cov(rel, parent.node.body[0].lineno, parent.node.end_lineno)
                            if not m and unit.func is not None and _runs_at_import(rel, unit.func):
                                m = import_time_tests(rel, unit, {unit.func.name})
                            break
                        m = lines_cov(rel, parent.start, parent.end)
                        parent = parent.parent
                # A function body that also runs while modules load (__init_subclass__,
                # metaclass __new__, builders called from class bodies) shapes state every
                # later test relies on, which no test's coverage records.
                imp = imp_pre.get(rel, set())
                if any(line in imp for line in range(unit.start, unit.end + 1)):
                    m |= import_exec_tests(rel, unit.func)
                # cached / run-once code: charge everyone who uses the cached result
                od = order_dependent.get(rel)
                if od and unit.func is not None and any(
                        line in od for line in range(unit.start, unit.end + 1)):
                    m |= escalate(rel, unit.func)
            elif unit.kind == "def":
                m = lines_cov(rel, unit.start, node.end_lineno)
                if _runs_at_import(rel, node):
                    m |= import_exec_tests(rel, node)
            elif unit.kind == "class":
                m = lines_cov(rel, unit.start, node.end_lineno) | import_time_tests(rel, unit, {node.name})
            else:  # "imp": module level or class body, evaluated at import
                m = import_time_tests(rel, unit, assigned_names(node))
            cache[key] = m
            return m

        element_cache = {}

        def elements(unit):
            """[(start, end, node)] of dict entries / list items / call args in an import-time
            statement, so a change to one entry only charges what that entry reaches."""
            key = id(unit)
            if key not in element_cache:
                found = []
                for sub in ast.walk(unit.node):
                    if isinstance(sub, ast.Dict):
                        for k, v in zip(sub.keys, sub.values):
                            found.append(((k or v).lineno, v.end_lineno, [n for n in (k, v) if n]))
                    elif isinstance(sub, (ast.List, ast.Tuple, ast.Set)):
                        found += [(e.lineno, e.end_lineno, [e]) for e in sub.elts]
                    elif isinstance(sub, ast.Call):
                        found += [(a.lineno, a.end_lineno, [a]) for a in sub.args]
                        found += [(k.value.lineno, k.value.end_lineno, [k.value]) for k in sub.keywords]
                element_cache[key] = found
            return element_cache[key]

        def import_line_tests(unit, line):
            """Impact of changing one line of an import-time statement."""
            node = unit.node
            # reader_tests is for core settings (Generator/Tokenizer/Dialect flags, module
            # constants). Expression classes (arg_types, is_var_len_args...) are read by
            # __init_subclass__ for every class; built_tests already pins them down exactly.
            # (matched by file too: generator.py's Generator is not the GENERATOR() expression)
            expr_body = (rel.startswith("sqlglot/expressions/") and bool(unit.classes)
                         and unit.classes[-1] in expr_classes)
            core = not dialect_of(rel) and not expr_body
            if unit.end == unit.start or isinstance(node, (ast.Import, ast.ImportFrom)):
                names = header_names(node)
                m = unit_tests(unit) | lines_cov(rel, unit.start, unit.end) | usage_tests(
                    names, rel, unit.start, unit.end)
                if core:  # a core setting such as Generator.CAST_TO_INT_ROUNDS = False
                    m |= reader_tests(rel, assigned_names(node), unit.start, unit.end)
                return m
            containing = [e for e in elements(unit) if e[0] <= line <= e[1]]
            if containing:
                # one entry of a dict/list/call: only what that entry reaches, including the
                # tests that build any expression class it names ("ARRAY": ... exp.Array(...))
                start, end, nodes = min(containing, key=lambda e: e[1] - e[0])
                names = set()
                for n in nodes:
                    names |= loaded_names(n)
                m = unit_tests(unit) | lines_cov(rel, start, end) | usage_tests(names, rel, unit.start, unit.end)
                for n in names:
                    m |= built_tests(n)
                if core and not any(isinstance(sub, (ast.Lambda, ast.Call))
                                    for n in nodes for sub in ast.walk(n)):
                    # a plain value ({exp.Union: True, ...}): reaches tests via its readers
                    m |= reader_tests(rel, assigned_names(node), unit.start, unit.end)
                return m
            # the statement's own frame: "X = {", "}", or a multi-line setting
            m = unit_tests(unit) | lines_cov(rel, line, line) | usage_tests(
                header_names(node), rel, unit.start, unit.end)
            if core:
                m |= reader_tests(rel, assigned_names(node), unit.start, unit.end)
            return m

        def _runs_at_import(path, func):
            imp = imp_pre.get(path, set())
            return any(line in imp for line in range(func.body[0].lineno, func.end_lineno + 1))

        per_line = []
        for line in range(1, n_lines + 2):
            unit = owner[line] if line <= n_lines else None
            if unit is not None and unit.kind == "imp":
                per_line.append(import_line_tests(unit, line))
            else:
                per_line.append(unit_tests(unit))
        impact[rel] = per_line

    # ---- data files under tests/ and test modules
    opens = defaultdict(int)
    for d in (opens_lazy, opens_pre):
        for p, m in d.items():
            opens[p] |= m

    # shared test modules (helpers.py, __init__.py...) need string spans as well
    for path in sorted(glob.glob(os.path.join(src, "tests", "**", "*.py"), recursive=True)):
        if not os.path.basename(path).startswith("test_"):
            spans, bs = string_spans_and_backslashes(open(path, encoding="utf-8").read())
            rel = os.path.relpath(path, src)
            if spans:
                strs[rel] = spans
            if bs:
                bslash[rel] = bs

    importers = defaultdict(set)  # test module path -> test files importing it
    for path in sorted(glob.glob(os.path.join(src, "tests", "**", "*.py"), recursive=True)):
        rel = os.path.relpath(path, src)
        for node in ast.walk(ast.parse(open(path, encoding="utf-8").read())):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("tests."):
                target = node.module.replace(".", "/") + ".py"
                importers[target].add(rel)
    changed = True
    while changed:  # transitive: importers of importers
        changed = False
        for target, files in list(importers.items()):
            extra = set().union(*(importers.get(f, set()) for f in files)) - files
            if extra:
                files |= extra
                changed = True
    timport = {}
    for target, files in importers.items():
        mask = 0
        for f in files:
            mask |= by_file.get(f, 0)
        if mask:
            timport[target] = mask

    tspans = {}
    for path in sorted(glob.glob(os.path.join(src, "tests", "**", "test_*.py"), recursive=True)):
        rel = os.path.relpath(path, src)
        test_source = open(path, encoding="utf-8").read()
        tree = ast.parse(test_source)
        # test modules are full of multi-line SQL strings: a "# ..." line inside one is
        # not a comment, so the selector needs their spans too
        spans, bs = string_spans_and_backslashes(test_source)
        if spans:
            strs[rel] = spans
        if bs:
            bslash[rel] = bs
        flat = []
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        tid = f"{rel}::{node.name}::{item.name}"
                        if tid in index_of:
                            flat.append((first_line(item), item.end_lineno, 1 << index_of[tid]))
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                tid = f"{rel}::{node.name}"
                if tid in index_of:
                    flat.append((first_line(node), node.end_lineno, 1 << index_of[tid]))
        tspans[rel] = flat

    # ---- intern sets
    sets, set_index = [[]], {0: 0}

    def intern(mask):
        if mask not in set_index:
            set_index[mask] = len(sets)
            sets.append([i for i in range(len(tests)) if mask >> i & 1])
        return set_index[mask]

    # Enums whose members compare by identity only (no str/int mixin): for them
    # "x is E.M" and "x == E.M" mean the same, which the selector uses to drop such edits.
    class_bases = defaultdict(list)
    for tree in trees.values():
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                class_bases[node.name].append(
                    [b.id if isinstance(b, ast.Name) else getattr(b, "attr", "?") for b in node.bases])

    def enum_kind(name, seen=()):
        if name in ("Enum", "Flag"):
            return "pure"
        if name in ("IntEnum", "StrEnum", "IntFlag", "str", "int", "float", "bytes"):
            return "mixed"
        if name not in class_bases or name in seen:
            return None
        kinds = []
        for bases in class_bases[name]:
            ks = [enum_kind(b, seen + (name,)) for b in bases]
            if "pure" in ks or "mixed" in ks:
                kinds.append("mixed" if "mixed" in ks or None in ks else "pure")
        if not kinds:
            return None
        return "mixed" if "mixed" in kinds else "pure"

    pure_enums = sorted(n for n in class_bases if enum_kind(n) in ("pure", "mixed")
                        and n not in ("Enum", "Flag"))

    result = {
        "pure_enums": pure_enums,
        "tests": test_ids,
        "sets": sets,
        "impact": {p: [intern(m) for m in v] for p, v in impact.items()},
        "strs": strs,
        "bslash": bslash,
        "opens": {p: intern(m) for p, m in sorted(opens.items())},
        "tspans": {p: [x for s, e, m in v for x in (s, e, intern(m))] for p, v in tspans.items()},
        "timport": {p: intern(m) for p, m in sorted(timport.items())},
        "nocov": intern(nocov),
    }
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with gzip.open(out, "wt", compresslevel=9) as f:
        json.dump(result, f, separators=(",", ":"))
    print(f"files={len(impact)} sets={len(sets)} opens={len(opens)} "
          f"nocov={bin(nocov).count('1')} size={os.path.getsize(out)}B", file=sys.stderr)


if __name__ == "__main__":
    main(*sys.argv[1:7])
