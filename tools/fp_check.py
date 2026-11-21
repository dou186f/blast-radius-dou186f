"""EVALUATION ONLY. Inside the organisers' image: copy sqlglot + the tpc-ds inputs to /tmp,
compile them, compute the pinned digest (twice), then each request's digest and CPU time.

    python tools/fp_check.py /src /work/fingerprint.py requests.json out.json
"""
import json, os, resource, shutil, subprocess, sys, compileall

src, script, req_path, out_path = sys.argv[1:5]
root = "/tmp/root"
shutil.copytree(os.path.join(src, "sqlglot"), os.path.join(root, "sqlglot"))
os.makedirs(os.path.join(root, "tests", "fixtures", "optimizer"))
for f in ("__init__.py", "helpers.py"):
    shutil.copy(os.path.join(src, "tests", f), os.path.join(root, "tests", f))
shutil.copytree(os.path.join(src, "tests/fixtures/optimizer/tpc-ds"), os.path.join(root, "tests/fixtures/optimizer/tpc-ds"))
compileall.compile_dir(root, quiet=1)
env = dict(os.environ, PYTHONHASHSEED="0")


def run(patched, baseline=None):
    r0 = resource.getrusage(resource.RUSAGE_CHILDREN)
    try:
        p = subprocess.run([sys.executable, script, root], input=json.dumps({"patched": patched, "baseline": baseline}),
                           capture_output=True, text=True, env=env, timeout=30)
    except subprocess.TimeoutExpired:
        return {"error": "timeout", "cpu": 30.0}
    r1 = resource.getrusage(resource.RUSAGE_CHILDREN)
    cpu = r1.ru_utime + r1.ru_stime - r0.ru_utime - r0.ru_stime
    if p.returncode:
        return {"error": p.stderr.strip().splitlines()[-1:] , "cpu": cpu}
    return dict(json.loads(p.stdout), cpu=cpu)


first, second = run({}), run({})
assert first["digests"] == second["digests"], "baseline is not deterministic"
baseline = first["digests"]
out = {"_base": [first["cpu"], second["cpu"], run({}, baseline)]}
print(out["_base"], flush=True)
for cid, patched in json.load(open(req_path)).items():
    out[cid] = run(patched, baseline)
    print(cid, out[cid], flush=True)
json.dump(out, open(out_path, "w"))
