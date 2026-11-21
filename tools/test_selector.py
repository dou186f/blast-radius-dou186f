"""Regression tests for applying narrower coverage only to the observed dependency."""
import unittest

from selector import ALL, Selector


class CoverageSelection(unittest.TestCase):
    def setUp(self):
        tests = [{"number": n, "id": str(n), "file": "tests/test_example.py"} for n in range(2)]
        self.module = "sqlglot/example.py"
        self.fixture = "tests/example.sql"
        self.header = "    strict: bool = True,"
        self.callback = '    "CAST": lambda x: convert(x, False),'
        self.selector = Selector(tests, {
            "tests": ["0", "1"], "sets": [[], [0, 1], [1]], "nocov": 0,
            "impact": {self.module: [1, 1]}, "strs": {}, "bslash": {}, "tspans": {},
            "literal_reads": {self.module: {
                "1": [(self.header.index("True"), self.header.index("True") + 4, 2)],
                "2": [(self.callback.index("False"), self.callback.index("False") + 5, 2)],
            }},
            "opens": {self.fixture: 1}, "fixture_fields": {self.fixture: ["12", [0, 0, 2]]},
        })

    def change(self, path, line, old, new):
        patch = f"--- a/{path}\n+++ b/{path}\n@@ -{line} +{line} @@\n-{old}\n+{new}\n"
        return self.selector.select(patch)

    def test_signature(self):
        old = self.header
        self.assertEqual(self.change(self.module, 1, old, old.replace("True", "False")), {1})
        for new in [old.replace("strict", "other"), old.replace("True", "make_default()")]:
            self.assertEqual(self.change(self.module, 1, old, new), {0, 1})

    def test_callback(self):
        old = self.callback
        self.assertEqual(self.change(self.module, 2, old, old.replace("False", "True")), {1})
        self.assertEqual(self.change(self.module, 2, old, old.replace("CAST", "FORMAT")), {0, 1})

    def test_indentation(self):
        self.assertIs(self.change(self.module, 1, "        run()", "    run()"), ALL)

    def test_fixture(self):
        self.assertEqual(self.change(self.fixture, 1, "input", "changed"), {0, 1})
        self.assertEqual(self.change(self.fixture, 2, "expected", "changed"), {0})
        for new in ["changed;", "# execute: true", "-- comment", ""]:
            self.assertEqual(self.change(self.fixture, 2, "expected", new), {0, 1})


class BoolSwap(unittest.TestCase):
    def setUp(self):
        tests = [{"number": n, "id": str(n), "file": "tests/test_example.py"} for n in range(3)]
        self.line = "    if a and (b or c):"
        self.selector = Selector(tests, {
            "tests": ["0", "1", "2"], "sets": [[], [0, 1, 2], [1, 2]], "nocov": 0,
            "impact": {"sqlglot/example.py": [1, 1]}, "strs": {}, "bslash": {}, "tspans": {},
            "bool_swaps": {"sqlglot/example.py": {"1": [(self.line.index("or"), "or", 2)]}},
            "opens": {},
        })

    def change(self, new):
        patch = f"--- a/sqlglot/example.py\n+++ b/sqlglot/example.py\n@@ -1 +1 @@\n-{self.line}\n+{new}\n"
        return self.selector.select(patch)

    def test_proven_swap_drops_unaffected_tests(self):
        self.assertEqual(self.change(self.line.replace("or", "and")), {0})

    def test_other_edits_keep_line_coverage(self):
        self.assertEqual(self.change(self.line.replace("and", "or", 1)), {0, 1, 2})
        self.assertEqual(self.change(self.line.replace("or c", "or d")), {0, 1, 2})
