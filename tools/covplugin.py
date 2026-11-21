"""pytest plugin used ONLY while building the coverage map (never shipped to the selector).

1. Replaces concurrent.futures.ProcessPoolExecutor with an in-process executor so the
   work sqlglot's tests farm out to spawned processes is attributed to the right test
   by coverage's dynamic contexts. Arguments and results still round-trip through
   pickle, because Expression pickling goes through sqlglot code (serde) too.
2. Records, per test, every data file under tests/ it opens (fixtures, tpc-h data...),
   via an audit hook. Opens outside any test (collection / import time) go to "".
"""

from __future__ import annotations

import ast
import concurrent.futures
import json
import os
import pickle
import sys
from concurrent.futures import Future

_ROOT = os.path.realpath(os.environ.get("COVMAP_ROOT", os.getcwd()))
_TESTS = os.path.join(_ROOT, "tests") + os.sep
_OUT = os.environ.get("COVMAP_OUT", os.path.join(_ROOT, ".covmap"))

_current = ""
_opens: dict[str, set[str]] = {}


class InlineProcessPoolExecutor(concurrent.futures.Executor):
    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **_):
        self._initializer = initializer
        self._initargs = initargs
        self._initialized = False

    def submit(self, fn, /, *args, **kwargs):
        future: Future = Future()
        try:
            if not self._initialized:
                self._initialized = True
                if self._initializer is not None:
                    init_args = pickle.loads(pickle.dumps(self._initargs))
                    self._initializer(*init_args)
            fn2, args2, kwargs2 = pickle.loads(pickle.dumps((fn, args, kwargs)))
            result = fn2(*args2, **kwargs2)
            future.set_result(pickle.loads(pickle.dumps(result)))
        except BaseException as error:  # noqa: BLE001 - mirror executor semantics
            future.set_exception(error)
        return future

    def shutdown(self, wait=True, *, cancel_futures=False):
        pass


concurrent.futures.ProcessPoolExecutor = InlineProcessPoolExecutor
try:
    import concurrent.futures.process as _process

    _process.ProcessPoolExecutor = InlineProcessPoolExecutor
except ImportError:
    pass


def _audit(event, args):
    if event != "open" or not args:
        return
    path = args[0]
    if isinstance(path, int):
        return
    try:
        path = os.fsdecode(path)
    except TypeError:
        return
    path = os.path.normpath(os.path.join(os.getcwd(), path))
    if not path.startswith(_TESTS) or path.endswith((".py", ".pyc")):
        return
    _opens.setdefault(_current, set()).add(os.path.relpath(path, _ROOT))


sys.addaudithook(_audit)


# Expression classes each test instantiates. Class bodies (arg_types, is_var_len_args...)
# run once at import, so line coverage cannot tell which test depends on them; a test
# can only be affected by a class-level setting of a class it builds objects of.
_classes: dict[str, set] = {}
_current_classes: set = _classes.setdefault("", set())


# Default-argument use: (function, parameter) pairs whose default value a test relied on.
# Changing "flag: bool = False" can only affect calls that did not pass flag; a call whose
# value merely equals the default is counted too (we cannot tell it apart), so this errs
# on the safe side.
_defaults: dict[str, set] = {}
_current_defaults: set = _defaults.setdefault("", set())
_MISSING = object()


def _set_context(context):
    global _current, _current_classes, _current_defaults, _current_bool
    _current = context
    _current_classes = _classes.setdefault(context, set())
    _current_defaults = _defaults.setdefault(context, set())
    _current_bool = _bool_seen.setdefault(context, {})
    if _monitoring_on:
        sys.monitoring.restart_events()  # re-arm callbacks disabled during the last test


_monitoring_on = False
_TOOL = 4
_monitored: dict = {}
_lambda_calls: dict[str, set[str]] = {}


def _record_lambda_calls():
    # Start before collection imports sqlglot. A lambda called during collection
    # has an empty context and cannot be narrowed using per-test calls.
    mon = sys.monitoring
    tool = 3
    mon.use_tool_id(tool, "covmap-lambdas")

    def on_start(code, offset):
        if code.co_name == "<lambda>" and code.co_filename.startswith(_ROOT + os.sep):
            path = os.path.relpath(code.co_filename, _ROOT)
            _lambda_calls.setdefault(_current, set()).add(f"{path}|{code.co_firstlineno}")
        return mon.DISABLE

    mon.register_callback(tool, mon.events.PY_START, on_start)
    mon.set_events(tool, mon.events.PY_START)


# Condition coverage (COVMAP_BOOLCOV=1, a separate run): see boolcov.py. Every sqlglot
# module is compiled from a rewritten AST that records, per evaluation of an and/or
# condition, the truth value of each operand it evaluated.
_BOOLCOV = os.environ.get("COVMAP_BOOLCOV") == "1"
_bool_seen: dict[str, dict[str, set]] = {}
_current_bool: dict[str, set] = _bool_seen.setdefault("", {})
_bool_stack: dict[str, list] = {}
_bool_keys: set[str] = set()       # every instrumented condition
_bool_broken: set[str] = set()     # conditions whose recording lost track (an exception)
_bool_tainted: set[str] = set()    # contexts that ran code we cannot see (process, thread)


def __bcov_begin__(key, n):
    values = ["-"] * n
    _bool_stack.setdefault(key, []).append(values)
    return values


def __bcov_leaf__(key, i, value):
    stack = _bool_stack.get(key)
    if stack:
        stack[-1][i] = "T" if value else "F"
    return value


def __bcov_end__(key, values, result):
    stack = _bool_stack[key]
    if stack and stack[-1] is values:
        stack.pop()
    else:  # a leaf raised inside a nested evaluation of the same condition
        _bool_broken.add(key)
        if values in stack:
            stack.remove(values)
    seen = _current_bool.get(key)
    if seen is None:
        seen = _current_bool[key] = set()
    seen.add("".join(values))
    return result


def _install_boolcov():
    import builtins
    import importlib.machinery

    import boolcov

    builtins.__bcov_begin__ = __bcov_begin__
    builtins.__bcov_leaf__ = __bcov_leaf__
    builtins.__bcov_end__ = __bcov_end__
    package = os.path.join(_ROOT, "sqlglot") + os.sep

    class Loader(importlib.machinery.SourceFileLoader):
        def source_to_code(self, data, path, *, _optimize=-1):
            rel = os.path.relpath(path, _ROOT)
            text = importlib.util.decode_source(data)
            tree = boolcov.rewrite(text, path, rel)
            _bool_keys.update(f"{rel}|{r.lineno}|{r.col_offset}"
                              for r in boolcov.truth_roots(ast.parse(text)))
            return compile(tree, path, "exec", dont_inherit=True)

    class Finder:
        @staticmethod
        def find_spec(name, path=None, target=None):
            if name != "sqlglot" and not name.startswith("sqlglot."):
                return None
            spec = importlib.machinery.PathFinder.find_spec(name, path, target)
            if spec and spec.origin and spec.origin.endswith(".py") and \
                    os.path.realpath(spec.origin).startswith(package):
                spec.loader = Loader(name, spec.origin)
            return spec

    import importlib.util
    sys.meta_path.insert(0, Finder)


def _taint(event, args):
    if _current and event in ("subprocess.Popen", "os.posix_spawn", "os.fork", "os.exec",
                              "_thread.start_new_thread", "os.system"):
        _bool_tainted.add(_current)


if _BOOLCOV:
    _install_boolcov()
    sys.addaudithook(_taint)
elif os.environ.get("COVMAP_PREIMPORT") == "1":
    _record_lambda_calls()


def _record_default_use():
    """Watch the entry of every sqlglot function that has simple literal defaults."""
    global _monitoring_on
    import inspect
    import types

    specs = {}

    def add(func):
        func = getattr(func, "__func__", func)
        if not isinstance(func, types.FunctionType):
            return
        code = func.__code__
        if code in specs or not code.co_filename.startswith(_ROOT):
            return
        params = []
        try:
            signature = inspect.signature(func)
        except (TypeError, ValueError):
            return
        for p in signature.parameters.values():
            d = p.default
            if d is not inspect.Parameter.empty and (d is None or isinstance(d, (bool, int, float, str))):
                params.append((p.name, d))
        if params:
            key = f"{os.path.relpath(code.co_filename, _ROOT)}|{code.co_name}|{code.co_firstlineno}"
            specs[code] = (key, params)

    def walk(obj, seen):
        for value in list(vars(obj).values()):
            if isinstance(value, property):
                for f in (value.fget, value.fset):
                    if f:
                        add(f)
            elif isinstance(value, type) and value.__module__.startswith("sqlglot") and value not in seen:
                seen.add(value)
                walk(value, seen)
            else:
                add(value)

    seen: set = set()
    for name, module in list(sys.modules.items()):
        if name == "sqlglot" or name.startswith("sqlglot."):
            walk(module, seen)

    mon = sys.monitoring
    mon.use_tool_id(_TOOL, "covmap-defaults")

    def on_start(code, offset):
        key, params = specs[code]
        local = sys._getframe(1).f_locals
        done = True
        for name, default in params:
            entry = (key, name)
            if entry in _current_defaults:
                continue
            value = local.get(name, _MISSING)
            if value is default or (type(value) is type(default) and value == default):
                _current_defaults.add(entry)
            else:
                done = False
        return mon.DISABLE if done else None

    _monitored.update(specs)
    mon.register_callback(_TOOL, mon.events.PY_START, on_start)
    for code in specs:
        mon.set_local_events(_TOOL, code, mon.events.PY_START)
    _monitoring_on = True


def _record_expression_classes():
    """Record, per test, the classes it builds objects of: expressions, plus the dialect,
    tokenizer, parser and generator classes (sqlglot creates a fresh one per call, so this
    tells exactly which dialects a test uses, even as a write target only)."""
    from sqlglot.dialects.dialect import Dialect
    from sqlglot.expressions.core import Expr
    from sqlglot.generator import Generator
    from sqlglot.parser import Parser
    from sqlglot.tokens import Tokenizer

    def wrap(base):
        original = base.__init__

        def __init__(self, *args, **kwargs):
            _current_classes.add(self.__class__)
            original(self, *args, **kwargs)

        base.__init__ = __init__

    for base in (Expr, Dialect, Tokenizer, Parser, Generator):
        wrap(base)


def pytest_collection(session):
    """With COVMAP_PREIMPORT=1, import every sqlglot module before any test runs, so
    import-time lines (class bodies, module constants) land in the empty context instead
    of being charged to whichever test happened to lazy-load a dialect first."""
    if os.environ.get("COVMAP_PREIMPORT") == "1":
        import importlib
        import pkgutil

        import sqlglot

        for info in pkgutil.walk_packages(sqlglot.__path__, prefix="sqlglot."):
            if info.name == "sqlglot.__main__":
                continue
            try:
                importlib.import_module(info.name)
            except Exception:  # noqa: BLE001 - optional modules
                pass
    _record_expression_classes()
    if os.environ.get("COVMAP_PREIMPORT") == "1" and not _BOOLCOV:
        _record_default_use()  # needs every module loaded first
    return None


def pytest_runtest_setup(item):
    _set_context(item.nodeid + "|setup")


def pytest_runtest_call(item):
    _set_context(item.nodeid + "|run")


def pytest_runtest_teardown(item):
    _set_context(item.nodeid + "|teardown")


def pytest_runtest_logfinish(nodeid, location):
    _set_context("")


def pytest_sessionfinish(session):
    os.makedirs(_OUT, exist_ok=True)
    worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
    if _BOOLCOV:
        with open(os.path.join(_OUT, f"bool-{worker}.json"), "w") as f:
            json.dump({"seen": {c: {k: sorted(v) for k, v in d.items()} for c, d in _bool_seen.items() if d},
                       "keys": sorted(_bool_keys), "broken": sorted(_bool_broken),
                       "tainted": sorted(_bool_tainted)}, f)
        return
    if os.environ.get("COVMAP_PREIMPORT") == "1":
        with open(os.path.join(_OUT, f"lambdas-{worker}.json"), "w") as f:
            json.dump({k: sorted(v) for k, v in _lambda_calls.items()}, f)
    with open(os.path.join(_OUT, f"opens-{worker}.json"), "w") as f:
        json.dump({k: sorted(v) for k, v in _opens.items()}, f)
    with open(os.path.join(_OUT, f"monitored-{worker}.json"), "w") as f:
        json.dump(sorted(f"{key}|{name}" for key, params in _monitored.values() for name, _ in params), f)
    with open(os.path.join(_OUT, f"defaults-{worker}.json"), "w") as f:
        json.dump({k: sorted(f"{key}|{name}" for key, name in v) for k, v in _defaults.items() if v}, f)
    with open(os.path.join(_OUT, f"classes-{worker}.json"), "w") as f:
        json.dump({k: sorted(f"{c.__module__}:{c.__qualname__}" for c in v)
                   for k, v in _classes.items() if v}, f)
