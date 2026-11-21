"""pytest plugin used ONLY while building the coverage map (never shipped to the selector).

1. Replaces concurrent.futures.ProcessPoolExecutor with an in-process executor so the
   work sqlglot's tests farm out to spawned processes is attributed to the right test
   by coverage's dynamic contexts. Arguments and results still round-trip through
   pickle, because Expression pickling goes through sqlglot code (serde) too.
2. Records, per test, every data file under tests/ it opens (fixtures, tpc-h data...),
   via an audit hook. Opens outside any test (collection / import time) go to "".
"""

from __future__ import annotations

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


def _set_context(context):
    global _current, _current_classes
    _current = context
    _current_classes = _classes.setdefault(context, set())


def _record_expression_classes():
    from sqlglot.expressions.core import Expr

    original = Expr.__init__

    def __init__(self, **args):
        _current_classes.add(self.__class__)
        original(self, **args)

    Expr.__init__ = __init__


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
    with open(os.path.join(_OUT, f"opens-{worker}.json"), "w") as f:
        json.dump({k: sorted(v) for k, v in _opens.items()}, f)
    with open(os.path.join(_OUT, f"classes-{worker}.json"), "w") as f:
        json.dump({k: sorted(f"{c.__module__}:{c.__qualname__}" for c in v)
                   for k, v in _classes.items() if v}, f)
