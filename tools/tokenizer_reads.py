"""Follow plain boolean tokenizer settings through the core's slotted storage."""
import ast
from collections import defaultdict
from itertools import chain
from pathlib import Path


TOKENIZER = "sqlglot/tokens.py"
CORE = "sqlglot/tokenizer_core.py"


def member(cls, name):
    return next((n for n in cls.body if getattr(n, "name", None) == name), None)


def self_attr(node):
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "self":
        return node.attr
    return None


def assigned(node):
    if isinstance(node, ast.Assign) and len(node.targets) == 1:
        return node.targets[0], node.value
    if isinstance(node, ast.AnnAssign):
        return node.target, node.value
    return None, None


def copied_settings(src, trees):
    """Yield (literal, read sites) for values forwarded unchanged into core slots.

    Match the pinned Tokenizer -> TokenizerCore edge. A parameter must occur only
    in its direct slot assignment. Any other read of the public name or stored
    field is counted. Reflection on a named field or competing writes to the
    slot make the data flow ambiguous and keep ordinary line coverage.
    """
    tokenizer = member(trees[TOKENIZER], "Tokenizer")
    core = member(trees[CORE], "TokenizerCore")
    if not tokenizer or not core or core.bases or core.decorator_list or core.keywords:
        return
    if any(member(core, name) for name in ("__getattr__", "__getattribute__", "__setattr__")):
        return
    init = member(core, "__init__")
    factory = member(tokenizer, "_init_core")
    if not init or not factory or init.decorator_list or factory.decorator_list:
        return
    if len(factory.body) != 1 or not isinstance(factory.body[0], ast.Return):
        return
    call = factory.body[0].value
    if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name) or call.func.id != core.name:
        return

    slots = set()
    slot_nodes = set()
    for stmt in core.body:
        target, value = assigned(stmt)
        if isinstance(target, ast.Name) and target.id == "__slots__" and isinstance(value, ast.Tuple):
            slot_nodes.update(value.elts)
            slots.update(n.value for n in value.elts if isinstance(n, ast.Constant))
    uses, stores = defaultdict(set), defaultdict(list)
    reflective = set()
    test_trees = ((p.relative_to(src).as_posix(), ast.parse(p.read_text(encoding="utf8")))
                  for p in Path(src).joinpath("tests").rglob("*.py"))
    for path, tree in chain(trees.items(), test_trees):
        for n in ast.walk(tree):
            if isinstance(n, (ast.Attribute, ast.Name)):
                name = n.attr if isinstance(n, ast.Attribute) else n.id
                if isinstance(n.ctx, ast.Load):
                    uses[name].add((path, n.lineno))
                elif isinstance(n, ast.Attribute):
                    stores[name].append(n)
            elif isinstance(n, ast.Constant) and isinstance(n.value, str) and n not in slot_nodes:
                reflective.add(n.value)

    references = defaultdict(list)
    for n in ast.walk(init):
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
            references[n.id].append(n)
    arguments = {kw.arg: kw.value for kw in call.keywords}
    value_by_name = {}
    for stmt in init.body:
        target, value = assigned(stmt)
        field = self_attr(target)
        if field not in slots or not isinstance(value, ast.Name) or references[value.id] != [value]:
            continue
        upstream = arguments.get(value.id)
        attr = self_attr(upstream)
        if not attr or stores[field] != [target] or attr in reflective or field in reflective:
            continue
        sites = uses[attr] - {(TOKENIZER, upstream.lineno)}
        sites |= uses[field] - {(CORE, value.lineno)}
        if sites:
            value_by_name[attr] = sites

    for stmt in tokenizer.body:
        target, literal = assigned(stmt)
        if (isinstance(target, ast.Name) and target.id in value_by_name
                and isinstance(literal, ast.Constant) and type(literal.value) is bool):
            yield literal, value_by_name[target.id]



def numeric_escape_values(src, trees):
    """Find value reads of the tokenizer's numeric escape specifications.

    Membership and truth tests of the dict can run while dialects import. They
    observe its keys and size. As long as those and the tuple structure stay
    fixed, the values are observed by lookups in the core.
    """
    public_name = "NUMERIC_ESCAPES"
    slot = "numeric_escapes"

    core = member(trees[CORE], "TokenizerCore")
    init = member(core, "__init__") if core else None
    if not init:
        return [], set()
    usages = [n for n in ast.walk(init) if isinstance(n, ast.Name) and n.id == slot]
    if len(usages) != 1 or not any(
        self_attr(target) == slot and value is usages[0]
        for target, value in (assigned(n) for n in init.body)
    ):
        return [], set()

    def shape_only(node, parents):
        parent = parents.get(node)
        if isinstance(parent, ast.BoolOp):
            return shape_only(parent, parents)
        if isinstance(parent, (ast.If, ast.IfExp, ast.While)):
            return node is parent.test
        if isinstance(parent, ast.UnaryOp):
            return isinstance(parent.op, ast.Not)
        if isinstance(parent, ast.Compare):
            return (len(parent.ops) == 1 and isinstance(parent.ops[0], (ast.In, ast.NotIn))
                    and node is parent.comparators[0])
        return (isinstance(parent, ast.Call) and isinstance(parent.func, ast.Name)
                and parent.func.id == "bool" and parent.args == [node] and not parent.keywords)

    for path in Path(src).joinpath("tests").rglob("*.py"):
        text = path.read_text(encoding="utf8")
        # Direct test introspection is not tracked by the source coverage masks.
        if public_name in text or slot in text:
            return [], set()

    reads, literals = set(), []
    for path, tree in trees.items():
        parents = {child: n for n in ast.walk(tree) for child in ast.iter_child_nodes(n)}
        for node in ast.walk(tree):
            target, value = assigned(node)
            if isinstance(target, ast.Name) and target.id == public_name and isinstance(value, ast.Dict):
                for k, spec in zip(value.keys, value.values):
                    if not (isinstance(k, ast.Constant) and type(k.value) is str
                            and isinstance(spec, ast.Tuple) and len(spec.elts) == 4):
                        continue
                    if all(isinstance(n, ast.Constant) and type(n.value) is int for n in spec.elts):
                        literals.extend((path, n) for n in spec.elts)
            if not isinstance(node, ast.Attribute) or not isinstance(node.ctx, ast.Load):
                continue
            if node.attr not in (public_name, slot) or shape_only(node, parents):
                continue
            parent = parents[node]
            if (node.attr == public_name and path == TOKENIZER and isinstance(parent, ast.keyword)
                    and parent.arg == slot and self_attr(node) == public_name):
                call = parents[parent]
                if isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id == "TokenizerCore":
                    continue
            if isinstance(parent, ast.Assign) and parent.value is node and len(parent.targets) == 1:
                name = parent.targets[0]
                fn = parent
                while fn in parents and not isinstance(fn, ast.FunctionDef):
                    fn = parents[fn]
                if isinstance(name, ast.Name) and isinstance(fn, ast.FunctionDef):
                    aliases = [n for n in ast.walk(fn) if isinstance(n, ast.Name) and n.id == name.id]
                    if all(n is name or (isinstance(n.ctx, ast.Load) and shape_only(n, parents))
                           for n in aliases):
                        continue
            if (path == CORE and node.attr == slot and isinstance(parent, ast.Attribute)
                    and parent.attr == "get" and isinstance(parents.get(parent), ast.Call)
                    and parents[parent].func is parent):
                reads.add((path, node.lineno))
                continue
            return [], set()
    return literals, reads
