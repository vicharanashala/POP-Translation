#!/bin/sh
# After the labelled test batch has finished AND the other-states ingest is complete:
# rebuild into data/staging, back the live index files up to data/backup_before_other_states/, swap, restart the server.
# Usage, from the repo root:  sh anara/other_states_finish.sh          (FORCE=1 skips the ingest-complete check)
set -e
cd "$(dirname "$0")/.."
PY=.venv/bin/python3
D=anara/data
STATUS=$(curl -s http://127.0.0.1:8050/api/test/status | $PY -c "import sys,json; print(json.load(sys.stdin).get('status'))" 2>/dev/null || echo unreachable)
if [ "$STATUS" = running ]; then echo "labelled test still running: wait for it (curl -s http://127.0.0.1:8050/api/test/status), then rerun"; exit 1; fi
TOTAL=$(($(wc -l < $D/other_state_docs.csv) - 1)); DONE=$(sort -u $D/other_states/done.txt | wc -l)
if [ "$DONE" -lt "$TOTAL" ] && [ -z "$FORCE" ]; then
  echo "ingest incomplete: $DONE of $TOTAL documents done. Resume: (setsid nohup $PY anara/other_states_stream.py >> $D/other_states/stream_full.log 2>&1 < /dev/null &), or FORCE=1 to build on what exists"; exit 1
fi
if ps -eo args | grep -v grep | grep -q "anara/other_states_stream.py"; then echo "ingest still running"; [ -z "$FORCE" ] && exit 1; fi
df -h . | tail -1
sh anara/other_states_stage.sh
B=$D/backup_before_other_states
mkdir -p $B
cp $D/text_report.csv $B/
for f in link_aliases.json zone_stats.pkl zone_bm25.npz zone_crops.npy zone_meta.json zone_levels.json prox_matrix.npz prox_raw.npz prox_meta.json; do
  [ -f $D/$f ] && mv $D/$f $B/$f
  mv $D/staging/$f $D/$f
done
mv $D/staging/text_report.csv $D/text_report.csv
echo "swapped; backups in $B"
PID=$(ss -ltnp | grep ':8050 ' | sed -n 's/.*pid=\([0-9]*\).*/\1/p' | head -1)
[ -n "$PID" ] && kill "$PID" && sleep 3
(setsid nohup $PY anara/app.py > $D/server.log 2>&1 < /dev/null &)
for i in $(seq 1 60); do
  [ "$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8050/)" = 200 ] && echo "server up" && break
  sleep 2
done
rm -rf $D/staging
df -h . | tail -1
