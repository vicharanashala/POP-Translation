#!/usr/bin/env bash
# Run the public Source Mapper in Docker (see INDEX.md, "Public container").
#   ./deploy.sh sync-data   hard-link the data files the keyword modes read into deploy/data (no copy, no extra disk)
#   ./deploy.sh build       build the image (python slim + the packages; ~0.7 GB)
#   ./deploy.sh init        first time: seed old test runs, create the container's OWN users (not the host instance's)
#   ./deploy.sh up | down | status | logs
#   ./deploy.sh show-passwords   YOU run this: prints the container's initial passwords to your terminal (then: ./deploy.sh forget-passwords)
#   ./deploy.sh tunnel-up | tunnel-down | tunnel-status   Cloudflare quick tunnel in a container (prints the public URL; see expose.sh)
# The host instance (python anara/app.py) and the container both use 127.0.0.1:8050: stop one before starting the other.
set -euo pipefail
cd "$(dirname "$0")"
FILES="central_docs.csv other_state_docs.csv crop_master.json keyword_index.json keyword_stats.pkl labelled_db_questions.jsonl link_aliases.json
 prox_matrix.npz prox_meta.json prox_raw.npz text_report.csv word_rarity.csv zone_bm25.npz zone_crops.npy zone_headings.json zone_levels.json
 zone_meta.json zone_stats.pkl"
run() { docker compose run --rm --no-deps -T "$@"; }
case "${1:-}" in
  sync-data)
    mkdir -p deploy/data
    for f in $FILES; do ln -f "data/$f" "deploy/data/$f"; done
    rm -rf deploy/data/pagetext; cp -al data/pagetext deploy/data/pagetext
    echo "deploy/data: $(ls deploy/data | wc -l) entries (hard links; same disk blocks as data/)";;
  build) docker compose build app;;
  init)
    [ -d deploy/data ] || { echo "run ./deploy.sh sync-data first" >&2; exit 1; }
    run -v "$PWD/data/test_runs:/seed:ro" app sh -c 'cp /seed/*.json /runs/ 2>/dev/null; ls /runs | wc -l'
    run app sh -c 'test -f /auth/users.json && echo "users exist" || python anara/auth.py genusers';;
  up)
    if ss -ltn | grep -q '127.0.0.1:8050 '; then
      docker compose ps --status running --services 2>/dev/null | grep -q proxy || { echo "127.0.0.1:8050 is in use (the host instance?). Stop it first." >&2; exit 1; }
    fi
    docker compose up -d;;
  down) docker compose down;;
  status) docker compose ps;;
  logs) docker compose logs --tail 100 "${2:-app}";;
  passwords|show-passwords) run app cat /auth/initial_passwords.txt;;
  tunnel-up)
    ./expose.sh preflight
    docker compose --profile tunnel up -d tunnel
    host=""
    for _ in $(seq 1 60); do
      host=$(docker compose logs --no-log-prefix tunnel 2>/dev/null | grep -oE '[a-z0-9-]+\.trycloudflare\.com' | grep -v '^api\.' | tail -1 || true)
      [ -n "$host" ] && break
      sleep 1
    done
    [ -n "$host" ] || { echo "No tunnel URL in 60 s; stopping the tunnel." >&2; docker compose logs --tail 20 tunnel >&2; docker compose rm -sf tunnel >/dev/null; exit 1; }
    # only this host is allowed; the file is re-read by the app on change (no restart)
    run app sh -c "printf '%s\n' '$host' > /auth/allowed_hosts.txt"
    echo "Public URL (random, public; anyone with it reaches the login page): https://$host";;
  tunnel-down)
    docker compose rm -sf tunnel >/dev/null
    run app sh -c ": > /auth/allowed_hosts.txt"
    echo "tunnel stopped, allowed_hosts.txt emptied";;
  tunnel-status)
    docker compose ps tunnel
    host=$(docker compose logs --no-log-prefix tunnel 2>/dev/null | grep -oE '[a-z0-9-]+\.trycloudflare\.com' | grep -v '^api\.' | tail -1 || true)
    echo "host: ${host:-none}; allowed_hosts.txt: $(run app cat /auth/allowed_hosts.txt 2>/dev/null | tr '\n' ' ')";;
  forget-passwords) run app rm -f /auth/initial_passwords.txt;;
  *) sed -n '2,9p' "$0"; exit 1;;
esac
