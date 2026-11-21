# The scorer builds this on linux/amd64 and runs it with no network. The
# digest is the image we labeled every commit in: its Python and SQLite
# decide which sqlglot tests run, so keep it if your image runs them.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
COPY fingerprint.py build_root.py /app/
# sqlglot at the pinned commit (only what the tpc-ds fingerprint reads), compiled, and the
# pinned commit's fingerprint: a map built from the pinned commit, never from a patch.
RUN python /app/build_root.py /app/root /app/data/tpcds_baseline.json
COPY data/covmap.json.gz /app/data/covmap.json.gz
COPY selector.py /app/selector.py
CMD ["python", "/app/selector.py"]
