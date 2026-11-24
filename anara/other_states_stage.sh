#!/bin/sh
# Build the index files for the sheet + Central + other states' documents into data/staging (ANARA_DATA), leaving the
# live data/ files untouched (the server keeps running). Rebuilds what the Central ingest rebuilt: zone_stats.pkl,
# zone_* (zonesearch build, also writes zone_levels.json), prox_* (proximity build), link_aliases.json, text_report.csv.
# NOT rebuilt (embedding-paired, original documents only): keyword_stats.pkl, keyword_index.json, chunk_matrix.npz.
# data/pagetext and data/pdfs are shared by symlink (the build only reads them).
# Usage, from the repo root:  sh anara/other_states_stage.sh
set -e
cd "$(dirname "$0")/.."
PY=.venv/bin/python3
D=anara/data
S=$D/staging
rm -rf "$S" && mkdir -p "$S"
ABS=$(cd "$D" && pwd)
for f in pagetext pdfs crop_master.json central_docs.csv zone_headings.json; do ln -s "$ABS/$f" "$S/$f"; done
cp "$D/other_state_docs.csv" "$D/zone_meta.json" "$D/link_aliases.json" "$S/"
$PY - <<'P'
import pandas as pd
a = pd.read_csv("anara/data/text_report.csv")
b = pd.read_csv("anara/data/other_states/text_report_other.csv")
b = b[~b.doc_id.isin(a.doc_id)].drop_duplicates("doc_id")
pd.concat([a, b], ignore_index=True).to_csv("anara/data/staging/text_report.csv", index=False)
print("text_report:", len(a), "+", len(b), "other-state rows")
P
export ANARA_DATA="$ABS/staging"
$PY anara/zonesearch.py build
$PY anara/proximity.py build
$PY anara/link_aliases.py build
ls -la "$S"
