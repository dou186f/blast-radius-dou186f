"""EVALUATION ONLY - never read by build_map.py or selector.py, never shipped.

For every patch in <val>/commits, builds a hardlinked copy of sqlglot-src with the patch
applied (sqlglot-src itself is never touched), runs the full suite in the organisers'
image, and writes <val>/answers.json + <val>/tests.json in the sample's format, so
tools/eval_sample.py <val> can score the selector on it.

    python3 tools/label.py sqlglot-src sample/tests.json build/val [parallel]
    (needs docker access: run under  sg docker -c "...")
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

IMAGE = "python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90"
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def apply_patch(src, work, patch_path):
    """Hardlink-copy src into work, then rewrite only the patched files (unlinking first,
    so the original inode in sqlglot-src stays untouched)."""
    shutil.copytree(src, work, copy_function=os.link, ignore=shutil.ignore_patterns(".git"))
    patch_path = os.path.abspath(patch_path)
    for line in open(patch_path, encoding="utf-8"):
        if line.startswith("+++ b/"):
            target = os.path.join(work, line[6:].strip())
            private = target + ".private"
            shutil.copyfile(target, private)  # a real copy, not a link to sqlglot-src
            os.replace(private, target)
    subprocess.run(["patch", "-p1", "--batch", "--silent", "--no-backup-if-mismatch",
                    "-i", patch_path, "-d", work], check=True)


def run_one(src, val, cid, uid):
    out = os.path.join(val, "results", f"{cid}.json")
    if os.path.exists(out):
        return cid, "cached"
    work = os.path.join(val, "work", cid)
    shutil.rmtree(work, ignore_errors=True)
    apply_patch(src, work, os.path.join(val, "commits", f"{cid}.patch"))
    rel_work = os.path.relpath(work, ROOT)
    rel_out = os.path.relpath(out, ROOT)
    start = time.time()
    cmd = [
        "docker", "run", "--rm", "--user", uid, "-e", "HOME=/tmp",
        "-e", "SKIP_INTEGRATION=0", "-e", "PYTHONDONTWRITEBYTECODE=1", "-e", "PYTHON_CPU_COUNT=4",
        "-e", "PYTHONPATH=/src:/work/tools", "-e", f"FAILPLUGIN_OUT=/work/{rel_out}",
        "-v", f"{os.path.abspath(work)}:/src:ro", "-v", f"{ROOT}:/work", "-w", "/src", IMAGE,
        "/work/.covenv/bin/python", "-m", "pytest", "tests", "-p", "failplugin", "-p", "no:cacheprovider",
        "-p", "no:pytest_cov", "-n", "8", "--timeout", "240", "-q",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    shutil.rmtree(work, ignore_errors=True)
    if not os.path.exists(out):
        raise RuntimeError(f"{cid}: no result, pytest said:\n{proc.stdout[-2000:]}{proc.stderr[-2000:]}")
    return cid, f"{time.time() - start:.0f}s"


def main(src, tests_json, val, parallel=3):
    os.makedirs(os.path.join(val, "results"), exist_ok=True)
    uid = f"{os.getuid()}:{os.getgid()}"
    commits = sorted(p[:-6] for p in os.listdir(os.path.join(val, "commits")) if p.endswith(".patch"))
    with ThreadPoolExecutor(int(parallel)) as pool:
        for future in [pool.submit(run_one, src, val, c, uid) for c in commits]:
            try:
                cid, note = future.result()
                print(f"{cid} {note}", file=sys.stderr, flush=True)
            except Exception as error:  # keep going; report at the end
                print(f"ERROR {error}", file=sys.stderr, flush=True)

    tests = json.load(open(tests_json))
    number_of = {t["id"]: t["number"] for t in tests["tests"]}
    by_file = {}
    for t in tests["tests"]:
        by_file.setdefault(t["file"], []).append(t["number"])
    kinds = json.load(open(os.path.join(val, "kinds.json")))
    answers = {}
    for cid in commits:
        path = os.path.join(val, "results", f"{cid}.json")
        if not os.path.exists(path):
            continue
        result = json.load(open(path))
        failing = {number_of[n] for n in result["failed"] if n in number_of}
        for f in result["broken_files"]:
            failing.update(by_file.get(f, []))
        unknown = [n for n in result["failed"] if n not in number_of]
        if unknown:
            print(f"{cid}: failures outside tests.json {unknown[:3]}", file=sys.stderr)
        answers[cid] = {"failing": sorted(failing), "kind": kinds.get(cid, "?")}
    # eval_sample.py expects only commits that have an answer
    with open(os.path.join(val, "answers.json"), "w") as f:
        json.dump({"commits": answers}, f, indent=1)
    shutil.copy(tests_json, os.path.join(val, "tests.json"))
    print(f"labelled {len(answers)}/{len(commits)}", file=sys.stderr)


if __name__ == "__main__":
    main(*sys.argv[1:])
