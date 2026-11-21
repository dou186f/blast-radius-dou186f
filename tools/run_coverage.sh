#!/bin/sh
# Runs sqlglot's suite with per-test coverage contexts, inside the same
# python:3.13-slim image the organisers labelled with. Run from the repo root:
#   sg docker -c "sh tools/run_coverage.sh lazy 0"   # normal (lazy dialect imports)
#   sg docker -c "sh tools/run_coverage.sh pre 1"    # every sqlglot module imported first
# Outputs go to build/<name>/ ; nothing is written inside sqlglot-src.
set -eu
NAME=$1
PREIMPORT=$2
IMAGE=python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
ROOT=$(pwd)
mkdir -p "$ROOT/build/$NAME"
# Hard caps, no swap: a runaway run is killed inside its container instead of freezing
# the machine. Override with COVERAGE_MEMORY / COVERAGE_CPUS.
docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp \
  --memory "${COVERAGE_MEMORY:-8g}" --memory-swap "${COVERAGE_MEMORY:-8g}" --cpus "${COVERAGE_CPUS:-16}" \
  -e COVMAP_PREIMPORT="$PREIMPORT" -e COVMAP_OUT="/work/build/$NAME" -e WORKERS="${WORKERS:-16}" \
  -e COVERAGE_FILE="/work/build/$NAME/.coverage" \
  -v "$ROOT/sqlglot-src:/src:ro" -v "$ROOT:/work" -w /src "$IMAGE" sh -c '
set -eu
if [ ! -x /work/.covenv/bin/python ]; then
  python -m venv /work/.covenv
  /work/.covenv/bin/pip install -q -r /work/requirements.lock coverage pytest-cov
fi
rm -f "$COVMAP_OUT"/.coverage* "$COVMAP_OUT"/opens-*.json "$COVMAP_OUT"/classes-*.json "$COVMAP_OUT"/defaults-*.json "$COVMAP_OUT"/monitored-*.json "$COVMAP_OUT"/lambdas-*.json
export SKIP_INTEGRATION=0 COVMAP_ROOT=/src PYTHONPATH=/src:/work/tools PYTHONDONTWRITEBYTECODE=1
/work/.covenv/bin/python -m pytest tests -p covplugin -p no:cacheprovider \
  -n ${WORKERS} --cov=sqlglot --cov-context=test --cov-report= -q
'
