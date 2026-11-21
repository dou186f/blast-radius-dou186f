"""EVALUATION ONLY - never read by build_map.py or selector.py, never shipped.

Generates synthetic patches against the pinned sqlglot commit, shaped like the sample
set (single-token code mutations, inserted comments, corrupted fixture lines, and
combinations), so tools/label.py can find out which tests really break and
tools/eval_sample.py can measure the selector's misses and cost on many more cases.

    python3 tools/mutate.py sqlglot-src build/val 240 [seed]

Writes build/val/commits/<id>.patch and build/val/kinds.json.
"""

from __future__ import annotations

import ast
import difflib
import io
import json
import os
import random
import sys
import tokenize

COMMENTS = [
    "# Cheap check first",
    "# Kept for backwards compatibility",
    "# Keep in sync with the generator",
    "# Intentionally not cached",
    "# Mirrors the behavior of the reference implementation",
    "# TODO: revisit",
    "# Fast path",
]
SWAPS = {
    "and": "or", "or": "and", "True": "False", "False": "True",
    "==": "!=", "!=": "==", "<": "<=", "<=": "<", ">": ">=", ">=": ">",
    "is": "==", "in": "not in",
}
FIXTURE_TOKENS = ["42", "0", "x", "NULL", "1", "a"]


def read(root, rel):
    with open(os.path.join(root, rel), encoding="utf-8") as f:
        return f.read()


def code_mutation(rng, root, files, targets=None):
    """Swap one operator/keyword/number token so the file still parses but means something else.
    With targets ({path: [line, ...]}) only those lines are mutated."""
    for _ in range(500):
        rel = rng.choice(sorted(targets) if targets else files)
        text = read(root, rel)
        lines = text.splitlines(keepends=True)
        try:
            tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
        except (tokenize.TokenError, SyntaxError):
            continue
        candidates = []
        allowed = set(targets[rel]) if targets else None
        for i, tok in enumerate(tokens):
            if tok.start[0] != tok.end[0]:
                continue
            if allowed is not None and tok.start[0] not in allowed:
                continue
            if tok.type == tokenize.NAME and tok.string in SWAPS:
                candidates.append((i, SWAPS[tok.string]))
            elif tok.type == tokenize.OP and tok.string in SWAPS:
                candidates.append((i, SWAPS[tok.string]))
            elif tok.type == tokenize.NUMBER and tok.string.isdigit() and len(tok.string) < 6:
                n = int(tok.string)
                candidates.append((i, str(n + 1 if n == 0 or rng.random() < 0.5 else n - 1)))
            elif tok.type == tokenize.NAME and tok.string == "not":
                nxt = tokens[i + 1] if i + 1 < len(tokens) else None
                if nxt is not None and nxt.string != "in":
                    candidates.append((i, ""))
        if not candidates:
            continue
        i, replacement = rng.choice(candidates)
        tok = tokens[i]
        row, (c0, c1) = tok.start[0] - 1, (tok.start[1], tok.end[1])
        line = lines[row]
        if replacement == "":  # drop "not " together with the following space
            new_line = line[:c0] + line[c1:].lstrip(" ")
        else:
            new_line = line[:c0] + replacement + line[c1:]
        new_text = "".join(lines[:row] + [new_line] + lines[row + 1:])
        try:
            if ast.dump(ast.parse(new_text)) == ast.dump(ast.parse(text)):
                continue
        except SyntaxError:
            continue
        return {rel: (text, new_text)}
    raise RuntimeError("no code mutation found")


def comment_mutation(rng, root, files):
    lines = []
    while not lines:  # skip empty files (some __init__.py)
        rel = rng.choice(files)
        text = read(root, rel)
        lines = text.splitlines(keepends=True)
    row = rng.randrange(len(lines))
    ref = next((l for l in lines[row:] if l.strip()), "")
    indent = ref[: len(ref) - len(ref.lstrip())]
    new_lines = lines[:row] + [f"{indent}{rng.choice(COMMENTS)}\n"] + lines[row:]
    return {rel: (text, "".join(new_lines))}


def fixture_mutation(rng, root, fixtures):
    for _ in range(200):
        rel = rng.choice(fixtures)
        text = read(root, rel)
        lines = text.splitlines(keepends=True)
        rows = [i for i, l in enumerate(lines) if l.strip() and not l.lstrip().startswith(("#", "--"))]
        if not rows:
            continue
        row = rng.choice(rows)
        line = lines[row].rstrip("\n")
        words = line.split(" ")
        how = rng.random()
        if how < 0.5:  # insert a stray token between words
            k = rng.randrange(len(words) + 1)
            words.insert(k, rng.choice(FIXTURE_TOKENS))
            new = " ".join(words)
        elif how < 0.75 and len(words) > 1:  # drop a word
            del words[rng.randrange(len(words))]
            new = " ".join(words)
        else:  # drop one character
            k = rng.randrange(len(line))
            new = line[:k] + line[k + 1:]
        if new == line:
            continue
        lines[row] = new + "\n"
        return {rel: (text, "".join(lines))}
    raise RuntimeError("no fixture mutation found")


def to_patch(changes):
    out = []
    for rel, (old, new) in sorted(changes.items()):
        diff = difflib.unified_diff(
            old.splitlines(keepends=True), new.splitlines(keepends=True),
            fromfile=f"a/{rel}", tofile=f"b/{rel}", n=3,
        )
        out.append(f"diff --git a/{rel} b/{rel}\n")
        out.extend(diff)
    return "".join(out)


def main(root, out_dir, count, seed=0, targets_json=None, prefix="m"):
    """With targets_json ({path: [line, ...]}), writes `count` code mutations on those
    lines only (targeted probing of one suspected weakness)."""
    rng = random.Random(int(seed))
    count = int(count)
    if targets_json:
        targets = json.load(open(targets_json))
        os.makedirs(os.path.join(out_dir, "commits"), exist_ok=True)
        kinds, seen = {}, set()
        while len(kinds) < count:
            changes = code_mutation(rng, root, [], targets)
            patch = to_patch(changes)
            if patch in seen:
                continue
            seen.add(patch)
            cid = f"{prefix}{len(kinds):03d}"
            with open(os.path.join(out_dir, "commits", f"{cid}.patch"), "w") as f:
                f.write(patch)
            kinds[cid] = "code"
        existing = os.path.join(out_dir, "kinds.json")
        if os.path.exists(existing):
            kinds = {**json.load(open(existing)), **kinds}
        with open(existing, "w") as f:
            json.dump(kinds, f, indent=1)
        print(f"wrote {count} targeted patches to {out_dir}", file=sys.stderr)
        return
    files = sorted(
        os.path.relpath(os.path.join(d, f), root)
        for d, _, fs in os.walk(os.path.join(root, "sqlglot"))
        for f in fs
        if f.endswith(".py") and f != "__main__.py"
    )
    fixtures = sorted(
        os.path.relpath(os.path.join(d, f), root)
        for d, _, fs in os.walk(os.path.join(root, "tests", "fixtures"))
        for f in fs
        if f.endswith(".sql")
    )
    plan = (["code"] * round(count * 0.58) + ["fixture"] * round(count * 0.17)
            + ["comment"] * round(count * 0.10))
    plan += ["multi"] * (count - len(plan))
    rng.shuffle(plan)

    os.makedirs(os.path.join(out_dir, "commits"), exist_ok=True)
    kinds = {}
    for n, kind in enumerate(plan):
        if kind == "code":
            changes = code_mutation(rng, root, files)
        elif kind == "fixture":
            changes = fixture_mutation(rng, root, fixtures)
        elif kind == "comment":
            changes = comment_mutation(rng, root, files)
        else:
            changes = {}
            core = code_mutation if rng.random() < 0.5 else fixture_mutation
            changes.update(core(rng, root, files if core is code_mutation else fixtures))
            for _ in range(rng.randint(1, 2)):
                extra = comment_mutation(rng, root, files)
                if not set(extra) & set(changes):
                    changes.update(extra)
        cid = f"m{n:03d}"
        with open(os.path.join(out_dir, "commits", f"{cid}.patch"), "w") as f:
            f.write(to_patch(changes))
        kinds[cid] = kind
    with open(os.path.join(out_dir, "kinds.json"), "w") as f:
        json.dump(kinds, f, indent=1)
    print(f"wrote {len(plan)} patches to {out_dir}", file=sys.stderr)


if __name__ == "__main__":
    main(*sys.argv[1:])
