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
from concurrent.futures import ThreadPoolExecutor, as_completed

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
    rel_out = os.path.relpath(out, ROOT)
    start = time.time()
    # Hard memory cap per container and no swap: a run that needs more is killed inside
    # its own container instead of taking the whole machine down (TPC-DS tests copy their
    # tables into every pool process). The cgroup's peak usage is reported afterwards.
    pytest = ("/work/.covenv/bin/python -m pytest tests -p failplugin -p no:cacheprovider "
              f"-p no:pytest_cov -n {WORKERS} --timeout 240 -q >/dev/null 2>&1; "
              "cat /sys/fs/cgroup/memory.peak")
    cmd = [
        "docker", "run", "--rm", "--user", uid, "-e", "HOME=/tmp",
        "--memory", MEMORY, "--memory-swap", MEMORY,
        "-e", "SKIP_INTEGRATION=0", "-e", "PYTHONDONTWRITEBYTECODE=1", "-e", f"PYTHON_CPU_COUNT={POOL}",
        "-e", "PYTHONPATH=/src:/work/tools", "-e", f"FAILPLUGIN_OUT=/work/{rel_out}",
        "-v", f"{os.path.abspath(work)}:/src:ro", "-v", f"{ROOT}:/work", "-w", "/src", IMAGE,
        "sh", "-c", pytest,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    shutil.rmtree(work, ignore_errors=True)
    peak = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else "?"
    peak = f"{int(peak) / 2**30:.1f}GB" if peak.isdigit() else peak
    if not os.path.exists(out):
        raise RuntimeError(f"{cid}: no result (peak {peak}, killed for memory?)")
    if json.load(open(out)).get("crashed"):
        os.remove(out)  # a worker died: labels unreliable, retried on the next run
        raise RuntimeError(f"{cid}: a test worker crashed (peak {peak}), result discarded")
    return cid, f"{time.time() - start:.0f}s peak {peak}"


MEMORY = os.environ.get("LABEL_MEMORY", "6g")
WORKERS = int(os.environ.get("LABEL_WORKERS", "6"))
POOL = int(os.environ.get("LABEL_POOL", "2"))


def main(src, tests_json, val, parallel=2):
    os.makedirs(os.path.join(val, "results"), exist_ok=True)
    uid = f"{os.getuid()}:{os.getgid()}"
    commits = sorted(p[:-6] for p in os.listdir(os.path.join(val, "commits")) if p.endswith(".patch"))
    with ThreadPoolExecutor(int(parallel)) as pool:
        # report each mutant as soon as it finishes (or fails), not in submission order
        for future in as_completed([pool.submit(run_one, src, val, c, uid) for c in commits]):
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
