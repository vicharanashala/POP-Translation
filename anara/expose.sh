#!/usr/bin/env bash
# Publish the Source Mapper on the internet through a Cloudflare QUICK tunnel that runs in a hardened container (the `tunnel`
# service of docker-compose.yml). No tunnel program ever runs on this host. Free, no account; the *.trycloudflare.com host is random.
#   anara/expose.sh            = ./deploy.sh up (if needed) + ./deploy.sh tunnel-up   (prints the public URL)
#   anara/expose.sh preflight  only the safety checks (used by deploy.sh tunnel-up)
#   ./deploy.sh tunnel-down | tunnel-status     stop / inspect
# Pre-flight refuses to go on unless: /health answers, the app container is read-only/unprivileged/non-root, /api/modes is 401
# without a login, /auth/users.json exists with mode 600. The Host allow-list (allowed_hosts.txt) gets ONLY the tunnel's host.
set -euo pipefail
cd "$(dirname "$0")"
URL="http://127.0.0.1:8050"
dc() { docker compose exec -T "$@"; }

preflight() {
  curl -fsS -m 5 "${URL}/health" >/dev/null || { echo "Nothing answers on ${URL}. Run ./deploy.sh up first." >&2; exit 1; }
  docker compose ps --status running --services 2>/dev/null | grep -qx proxy || { echo "The anara container is not running (is the HOST instance on :8050?). Run ./deploy.sh up." >&2; exit 1; }
  [ "$(docker inspect -f '{{.HostConfig.ReadonlyRootfs}} {{.HostConfig.Privileged}} {{.Config.User}}' anara-app-1)" = "true false 10001:10001" ] \
    || { echo "Refusing to expose: anara-app-1 is not read-only / unprivileged / non-root." >&2; exit 1; }
  local code; code=$(curl -s -o /dev/null -w '%{http_code}' -m 5 "${URL}/api/modes")
  [ "$code" = 401 ] || { echo "Refusing to expose: /api/modes answered ${code} without a login (expected 401)." >&2; exit 1; }
  [ "$(dc app stat -c %a /auth/users.json 2>/dev/null)" = 600 ] || { echo "Refusing to expose: /auth/users.json is missing or not mode 600 in the container. Run ./deploy.sh init." >&2; exit 1; }
  if dc app test -f /auth/initial_passwords.txt 2>/dev/null; then
    echo "Note: the initial passwords file still exists. Read it once yourself (./deploy.sh show-passwords), store them, then ./deploy.sh forget-passwords." >&2
  fi
}

case "${1:-}" in
  preflight) preflight;;
  "") ./deploy.sh up; ./deploy.sh tunnel-up;;
  *) echo "usage: expose.sh [preflight]" >&2; exit 1;;
esac
