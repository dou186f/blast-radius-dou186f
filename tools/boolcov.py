"""Condition coverage for and/or: which truth values each test saw in a boolean condition.

Used only while building the map (never shipped). A "root" is a maximal and/or tree whose
value is only ever truth-tested: the test of if / while / ternary / assert / comprehension
filters, or the operand of `not`. Its leaves are the operands that are not themselves
and/or. During the coverage run every root is rewritten so that each evaluation records
the truth value of every leaf it evaluated ('T', 'F', or '-' when short-circuited away);
the program's behaviour is unchanged (each leaf is evaluated once, in the same order, and
its value is passed through). Swapping one `and`/`or` cannot change a test whose recorded
evaluations all give the same truth value under the swapped tree, with every leaf the
swapped tree needs already known: same control flow, same program.
"""

from __future__ import annotations

import ast

_TRUTH_PARENTS = (ast.If, ast.While, ast.IfExp, ast.Assert)
_SUSPEND = (ast.Yield, ast.YieldFrom, ast.Await, ast.Lambda)


def _strip_not(node):
    while isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        node = node.operand
    return node


def truth_roots(tree):
    """Root and/or trees of a module, outermost first, in source order."""
    candidates = []
    for node in ast.walk(tree):
        if isinstance(node, _TRUTH_PARENTS):
            candidates.append(node.test)
        elif isinstance(node, ast.comprehension):
            candidates.extend(node.ifs)
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            candidates.append(node.operand)
    roots, seen = [], set()
    for c in candidates:
        r = _strip_not(c)
        if isinstance(r, ast.BoolOp) and id(r) not in seen:
            if any(isinstance(n, _SUSPEND) for n in ast.walk(r)):
                continue  # suspension or a new scope inside: keep it simple and skip
            seen.add(id(r))
            roots.append(r)
    roots.sort(key=lambda r: (r.lineno, r.col_offset))
    return roots


def leaves(root):
    """Operands of the and/or tree, left to right (evaluation order)."""
    out = []

    def visit(n):
        if isinstance(n, ast.BoolOp):
            for v in n.values:
                visit(v)
        else:
            out.append(n)

    visit(root)
    return out


class Unknown(Exception):
    pass


def truth(node, values, index):
    """Truth value of an and/or tree, given the recorded truth of each leaf."""
    if isinstance(node, ast.BoolOp):
        want = isinstance(node.op, ast.Or)  # Or stops at the first true, And at the first false
        for v in node.values:
            if truth(v, values, index) == want:
                return want
        return not want
    value = values[index[id(node)]]
    if value == "-":
        raise Unknown
    return value == "T"


# ---------------------------------------------------------------- rewriting (coverage run)
class _Rewriter:
    def __init__(self, tree, rel):
        self.rel = rel
        self.roots = {id(r): r for r in truth_roots(tree)}

    def wrap(self, root):
        key = f"{self.rel}|{root.lineno}|{root.col_offset}"
        leaf_list = leaves(root)
        position = {id(leaf): i for i, leaf in enumerate(leaf_list)}

        def copy(n):
            if isinstance(n, ast.BoolOp):
                return ast.copy_location(ast.BoolOp(op=n.op, values=[copy(v) for v in n.values]), n)
            call = ast.Call(func=ast.Name("__bcov_leaf__", ast.Load()),
                            args=[ast.Constant(key), ast.Constant(position[id(n)]), self.visit(n)],
                            keywords=[])
            return ast.copy_location(call, n)

        begin = ast.Call(func=ast.Name("__bcov_begin__", ast.Load()),
                         args=[ast.Constant(key), ast.Constant(len(leaf_list))], keywords=[])
        end = ast.Call(func=ast.Name("__bcov_end__", ast.Load()),
                       args=[ast.Constant(key), begin, copy(root)], keywords=[])
        return ast.copy_location(end, root)

    def visit(self, node):
        """Rewrite every root below node (leaves may hold further roots)."""
        if id(node) in self.roots:
            return self.wrap(node)
        for field, value in ast.iter_fields(node):
            if isinstance(value, list):
                setattr(node, field, [self.visit(v) if isinstance(v, ast.AST) else v for v in value])
            elif isinstance(value, ast.AST):
                setattr(node, field, self.visit(value))
        return node


def rewrite(source, filename, rel):
    tree = ast.parse(source, filename)
    tree = _Rewriter(tree, rel).visit(tree)
    ast.fix_missing_locations(tree)
    return tree


# ---------------------------------------------------------------- swaps (map build)
def swaps(text, tree):
    """For every and/or keyword inside a root: (line, col, old, new, root, mutant tree), the
    mutant being the root re-parsed with that one keyword swapped. Swaps that change the
    leaves themselves (a keyword inside a leaf, a regrouping that splits a leaf) are left
    out, so they keep the ordinary line-coverage selection."""
    import io
    import tokenize

    lines = text.splitlines(keepends=True)
    keywords = [(t.start, t.string) for t in tokenize.generate_tokens(io.StringIO(text).readline)
                if t.type == tokenize.NAME and t.string in ("and", "or")]
    out = []
    for root in truth_roots(tree):
        start, end = (root.lineno, root.col_offset), (root.end_lineno, root.end_col_offset)
        if any(len(lines[n - 1]) != len(lines[n - 1].encode()) for n in range(start[0], end[0] + 1)):
            continue  # col offsets are bytes; keep to plain ASCII lines
        original = [ast.dump(leaf) for leaf in leaves(root)]
        for (line, col), word in keywords:
            if not (start <= (line, col) < end):
                continue
            other = "or" if word == "and" else "and"
            seg = []
            for n in range(start[0], end[0] + 1):
                s = lines[n - 1]
                if n == line:
                    s = s[:col] + other + s[col + len(word):]
                lo = start[1] if n == start[0] else 0
                hi = end[1] + (len(other) - len(word) if n == line else 0) if n == end[0] else len(s)
                seg.append(s[lo:hi])
            try:
                mutant = ast.parse("(" + "".join(seg) + "\n)", mode="eval").body
            except SyntaxError:
                continue
            mutant_leaves = leaves(mutant) if isinstance(mutant, ast.BoolOp) else []
            if [ast.dump(leaf) for leaf in mutant_leaves] != original:
                continue
            out.append((line, col, word, other, root, mutant))
    return out


def same_truth(root, mutant, observed):
    """True when every recorded evaluation gives the mutant the same truth value."""
    index = {id(leaf): i for i, leaf in enumerate(leaves(root))}
    mindex = {id(leaf): i for i, leaf in enumerate(leaves(mutant))}
    for values in observed:
        try:
            if truth(root, values, index) != truth(mutant, values, mindex):
                return False
        except Unknown:
            return False
    return True
