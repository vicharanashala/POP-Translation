#!/usr/bin/env bash
# Stops the cloudflared container (docker network anara_edge, 172.29.251.0/24) from reaching your LAN, the Tailscale
# address, the libvirt bridge or any other private address. Internet access and the nginx proxy keep working.
# NEEDS sudo and is NOT run by the stack. Run it yourself:
#
#   sudo ./deploy/firewall.sh add       install the rules
#   sudo ./deploy/firewall.sh check     show them
#   sudo ./deploy/firewall.sh remove    take them out
#
# Two chains matter: FORWARD traffic (container -> LAN router, other machines) goes through DOCKER-USER, and traffic to
# an address of THIS machine (192.168.122.1, the Tailscale 100.x address) goes through INPUT. The rules are not
# persistent: they disappear on reboot or when Docker restarts, so re-run `add` afterwards (or put it in a systemd unit).
set -euo pipefail
EDGE=172.29.251.0/24
PRIVATE=(10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 100.64.0.0/10 169.254.0.0/16)
TAG="anara-edge"

rules() {   # prints "chain rule..." lines, in the order they must be inserted (first line ends up first in the chain)
  echo "DOCKER-USER -s $EDGE -d $EDGE -j RETURN"                       # proxy <-> tunnel on the same network
  for n in "${PRIVATE[@]}"; do echo "DOCKER-USER -s $EDGE -d $n -j DROP"; done
  echo "INPUT -s $EDGE -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT"   # replies to connections the host started
  echo "INPUT -s $EDGE -j DROP"                                          # the tunnel container may not talk to the host itself
}

case "${1:-}" in
  add)
    [ "$(id -u)" = 0 ] || { echo "run with sudo" >&2; exit 1; }
    "$0" remove >/dev/null 2>&1 || true
    rules | tac | while read -r chain rest; do iptables -I "$chain" $rest -m comment --comment "$TAG"; done
    echo "installed. Check with: docker run --rm --network anara_edge busybox wget -T 3 -qO- http://192.168.1.1 (must fail)";;
  remove)
    [ "$(id -u)" = 0 ] || { echo "run with sudo" >&2; exit 1; }
    rules | while read -r chain rest; do while iptables -D "$chain" $rest -m comment --comment "$TAG" 2>/dev/null; do :; done; done
    echo "removed";;
  check)
    iptables -S DOCKER-USER | grep "$TAG" || echo "no DOCKER-USER rules"
    iptables -S INPUT | grep "$TAG" || echo "no INPUT rules";;
  *) sed -n '2,13p' "$0"; exit 1;;
esac
