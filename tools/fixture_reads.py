"""Derive field readers for SQL fixture pairs materialized by a unittest class setup.

The pinned load_sql_fixture_pairs yields (metadata, input SQL, expected SQL). Trace
direct calls on the test instance and simple tuple unpacking. Escaping the list or
passing the test instance to unknown code conservatively charges the whole fixture.
"""

import ast
from pathlib import Path


FIXTURE_LIST = object()
SQL_FIELDS = {1, 2}


def setup_pair(node):
    if not isinstance(node, ast.Assign) or len(node.targets) != 1:
        return None
    target = node.targets[0]
    if not isinstance(target, ast.Attribute) or ast.unparse(target.value) != "cls":
        return None
    call = node.value
    if not isinstance(call, ast.Call) or ast.unparse(call.func) != "list" or len(call.args) != 1:
        return None
    loader = call.args[0]
    if not isinstance(loader, ast.Call) or ast.unparse(loader.func) != "load_sql_fixture_pairs":
        return None
    if (len(loader.args) == 1 and isinstance(loader.args[0], ast.Constant)
            and isinstance(loader.args[0].value, str) and not loader.keywords):
        return target.attr, "tests/fixtures/" + loader.args[0].value
    return None


def fields_read(methods, method, attribute, args=None, stack=()):
    if method in stack:
        return SQL_FIELDS.copy()
    fn = methods[method]
    env = args or {}
    loaded = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    fields = set()
    for n in ast.walk(fn):
        if isinstance(n, ast.Name) and n.id in env and isinstance(n.ctx, ast.Store):
            return SQL_FIELDS.copy()
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in ("locals", "vars", "eval", "exec"):
            return SQL_FIELDS.copy()

    def value(n):
        if isinstance(n, ast.Constant) and type(n.value) is bool:
            return n.value
        if isinstance(n, ast.Name):
            return env.get(n.id)
        if isinstance(n, ast.Attribute) and ast.unparse(n.value) in ("self", "cls") and n.attr == attribute:
            return FIXTURE_LIST
        return None

    def unpack(target):
        if not isinstance(target, ast.Tuple) or len(target.elts) != 3:
            fields.update((1, 2))
            return
        for i, item in enumerate(target.elts):
            if not isinstance(item, ast.Name) or item.id in loaded:
                fields.add(i)

    def visit(n):
        if isinstance(n, ast.IfExp):
            condition = value(n.test)
            if type(condition) is bool:
                visit(n.body if condition else n.orelse)
                return
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and ast.unparse(n.func.value) == "self":
            callee = n.func.attr
            if callee in methods:
                child = methods[callee]
                params = child.args.args[1:]
                if child.args.vararg or child.args.kwarg or any(k.arg is None for k in n.keywords):
                    fields.update((1, 2))
                    return
                bound = {}
                arguments = [(p.arg, a) for p, a in zip(params, n.args)]
                arguments.extend((k.arg, k.value) for k in n.keywords)
                for key, arg in arguments:
                    v = value(arg)
                    if v is FIXTURE_LIST:
                        bound[key] = FIXTURE_LIST
                    elif type(v) is bool:
                        bound[key] = v
                    else:
                        visit(arg)
                fields.update(fields_read(methods, callee, attribute, bound, stack + (method,)))
                return
            if callee != "subTest" and not callee.startswith("assert"):
                fields.update((1, 2))
                return
            for arg in [*n.args, *(k.value for k in n.keywords)]:
                visit(arg)
            return
        if isinstance(n, ast.comprehension) and value(n.iter) is FIXTURE_LIST:
            unpack(n.target)
            for condition in n.ifs:
                visit(condition)
            return
        if isinstance(n, ast.Assign) and len(n.targets) == 1:
            rhs = n.value
            if isinstance(rhs, ast.IfExp) and type(value(rhs.test)) is bool:
                rhs = rhs.body if value(rhs.test) else rhs.orelse
            if isinstance(rhs, ast.Subscript) and value(rhs.value) is FIXTURE_LIST:
                unpack(n.targets[0])
                visit(rhs.slice)
                return
        if value(n) is FIXTURE_LIST or (isinstance(n, ast.Name) and n.id in ("self", "cls")):
            fields.update((1, 2))
            return
        if isinstance(n, ast.Attribute) and ast.unparse(n.value) in ("self", "cls"):
            if n.attr in methods:  # a bound method escaping the tracked call graph
                fields.update((1, 2))
            return
        for child in ast.iter_child_nodes(n):
            visit(child)

    for stmt in fn.body:
        visit(stmt)
    return fields


def pair_readers(tree, rel, index_of, src):
    result = []
    if not any(isinstance(n, ast.ImportFrom) and n.module == "tests.helpers"
               and any(a.name == "load_sql_fixture_pairs" and a.asname is None for a in n.names)
               for n in tree.body):
        return result
    for cls in tree.body:
        if not isinstance(cls, ast.ClassDef) or [ast.unparse(b) for b in cls.bases] != ["unittest.TestCase"]:
            continue
        methods = {f.name: f for f in cls.body if isinstance(f, ast.FunctionDef)}
        setup = methods.get("setUpClass")
        if not setup or "setUp" in methods or "tearDown" in methods:
            continue
        bindings = []
        for stmt in reversed(setup.body):
            pair = setup_pair(stmt)
            if not pair:
                break
            bindings.append(pair)
        for attr, path in bindings:
            if sum(other_path == path for _, other_path in bindings) != 1:
                continue
            if any(isinstance(n, ast.Attribute) and n.attr == attr for stmt in setup.body[:-len(bindings)]
                   for n in ast.walk(stmt)):
                continue
            fields = [0, 0, 0]
            for name, method in methods.items():
                tid = f"{rel}::{cls.name}::{name}"
                if tid not in index_of or len(method.body) != 1:
                    continue
                # Limit the proof to simple forwarding test methods. All other readers
                # retain their ordinary open-file dependency.
                stmt = method.body[0]
                if not isinstance(stmt, ast.Expr) or not isinstance(stmt.value, ast.Call):
                    continue
                called = stmt.value.func
                if not isinstance(called, ast.Attribute) or called.attr not in methods:
                    continue
                reads = fields_read(methods, name, attr)
                if "tearDownClass" in methods:
                    reads |= fields_read(methods, "tearDownClass", attr)
                for i in (1, 2):
                    if i not in reads:
                        fields[i] |= 1 << index_of[tid]
            if any(fields):
                roles = []
                halves = 0
                for line in (Path(src) / path).read_text().splitlines():
                    if line and not line.startswith("--"):
                        roles.append(1 + halves % 2)
                        halves += line.count(";")
                    else:
                        roles.append(0)
                result.append((path, roles, fields))
    return result
