"""EVALUATION ONLY - pytest plugin for tools/label.py; never shipped.

Records every test that fails the way the organisers count it: a failed or errored
phase, a failed subtest (pytest 9 shows the test PASSED next to SUBFAILED), a timeout,
or a test module that cannot be imported. Writes {"failed": [...], "broken_files": [...]}
to $FAILPLUGIN_OUT from the controlling process.
"""

import json
import os

_failed = set()
_broken_files = set()
_crashed = set()  # a worker died (e.g. killed for memory): this run's labels can't be trusted


def pytest_runtest_logreport(report):
    if report.failed:
        _failed.add(report.nodeid.split("[", 1)[0])
        if "crashed while running" in str(report.longrepr):
            _crashed.add(report.nodeid)


def pytest_collectreport(report):
    if report.failed:
        _broken_files.add(report.nodeid.split("::", 1)[0])


def pytest_sessionfinish(session, exitstatus):
    if os.environ.get("PYTEST_XDIST_WORKER"):
        return
    with open(os.environ["FAILPLUGIN_OUT"], "w") as f:
        json.dump({"failed": sorted(_failed), "broken_files": sorted(_broken_files),
                   "crashed": sorted(_crashed), "exitstatus": int(exitstatus)}, f)
