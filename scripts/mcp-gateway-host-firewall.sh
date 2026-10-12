#!/usr/bin/env bash
set -euo pipefail

# Host-level restriction for the MCP gateway NodePort (30430) on the k3s node.
# ufw's INPUT chain does NOT see DNAT'd NodePort traffic (it is rewritten in PREROUTING and
# routed through FORWARD), so rules go in a dedicated iptables chain hooked from
# PREROUTING in the raw table, where the original destination port is still visible.
# Run as root by an administrator. Idempotent. --remove undoes it.
#
# Usage: sudo scripts/mcp-gateway-host-firewall.sh [--remove] [--dry-run]
# Env:   ALLOWED_CIDRS  space-separated sources (default tailnet + LAN)
#        NODEPORT       default 30430

NODEPORT="${NODEPORT:-30430}"
ALLOWED_CIDRS="${ALLOWED_CIDRS:-100.64.0.0/10 192.168.0.0/16}"
CHAIN=MCP-GATEWAY-GUARD
remove=false; dry=false
for arg in "$@"; do
  case "$arg" in --remove) remove=true ;; --dry-run) dry=true ;; *) echo "unknown arg $arg" >&2; exit 2 ;; esac
done
run() { if $dry; then echo "+ $*"; else "$@"; fi; }

if $remove; then
  run iptables -t raw -D PREROUTING -p tcp --dport "$NODEPORT" -j "$CHAIN" 2>/dev/null || true
  run iptables -t raw -F "$CHAIN" 2>/dev/null || true
  run iptables -t raw -X "$CHAIN" 2>/dev/null || true
  exit 0
fi

run iptables -t raw -N "$CHAIN" 2>/dev/null || true
run iptables -t raw -F "$CHAIN"
for cidr in $ALLOWED_CIDRS; do run iptables -t raw -A "$CHAIN" -s "$cidr" -j RETURN; done
run iptables -t raw -A "$CHAIN" -j DROP
if ! iptables -t raw -C PREROUTING -p tcp --dport "$NODEPORT" -j "$CHAIN" 2>/dev/null; then
  run iptables -t raw -I PREROUTING -p tcp --dport "$NODEPORT" -j "$CHAIN"
fi
echo "guard installed for tcp/$NODEPORT; allowed: $ALLOWED_CIDRS"
