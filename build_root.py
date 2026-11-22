"""Docker build step: fetch sqlglot at the pinned commit, keep what fingerprint.py needs,
compile it, and record the pinned commit's fingerprints (tpc-ds and max_nodes).

    python build_root.py /app/root /app/data/fp_baseline.json
"""

import compileall
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import urllib.request

COMMIT = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
URL = f"https://codeload.github.com/tobymao/sqlglot/tar.gz/{COMMIT}"
KEEP = ("sqlglot/", "tests/__init__.py", "tests/helpers.py", "tests/fixtures/optimizer/tpc-ds/")


def main(root, baseline_path):
    with urllib.request.urlopen(URL, timeout=120) as response:
        data = response.read()
    prefix = f"sqlglot-{COMMIT}/"
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for member in tar.getmembers():
            if not member.isfile() or not member.name.startswith(prefix):
                continue
            rel = member.name[len(prefix):]
            if not rel.startswith(KEEP):
                continue
            target = os.path.join(root, rel)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with tar.extractfile(member) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
    if not compileall.compile_dir(root, quiet=1):
        sys.exit("compileall failed")
    env = dict(os.environ, PYTHONHASHSEED="0", PYTHONDONTWRITEBYTECODE="1")
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fingerprint.py")
    baseline = {}
    for check, least in (("tpcds", 50), ("max_nodes", 5)):
        runs = []
        for _ in range(2):
            out = subprocess.run([sys.executable, script, root],
                                 input=json.dumps({"check": check}), capture_output=True,
                                 text=True, env=env, check=True)
            runs.append(json.loads(out.stdout))
        if runs[0]["digests"] != runs[1]["digests"] or len(runs[0]["digests"]) < least:
            sys.exit(f"{check} fingerprint is not deterministic")
        baseline[check] = {"digests": runs[0]["digests"]}
        if "seconds" in runs[0]:  # the slower of the two runs: the slowdown check's base
            baseline[check]["seconds"] = max(run["seconds"] for run in runs)
    os.makedirs(os.path.dirname(baseline_path), exist_ok=True)
    with open(baseline_path, "w") as f:
        json.dump(baseline, f)


if __name__ == "__main__":
    main(*sys.argv[1:3])
