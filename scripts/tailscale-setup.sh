#!/usr/bin/env bash
set -euo pipefail

# tailscale-setup.sh -- PRINTS the commands to put the company-os k3s node and
# an operator workstation on a tailnet. Changes nothing: it never installs
# software, never touches k3s, never calls sudo. Review the output, then run
# the commands yourself. Full rationale: docs/TAILSCALE.md.
#
# Usage: scripts/tailscale-setup.sh node        <magicdns-name> <tailnet-ip>
#        scripts/tailscale-setup.sh workstation <magicdns-name>
#   e.g. scripts/tailscale-setup.sh node company-os.tail1234.ts.net 100.64.0.7

die() { echo "ERROR: $1" >&2; exit 1; }

mode="${1:-}"
name="${2:-}"
case "$mode" in
  node)
    ip="${3:-}"
    [ -n "$name" ] && [ -n "$ip" ] || die "usage: $0 node <magicdns-name> <tailnet-ip>"
    cat <<OUT
# === On the k3s node (needs sudo) ===
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up --ssh --hostname=company-os --advertise-tags=tag:k3s-node
tailscale status && tailscale ip -4

# Add the tailnet name + IP to the API server cert (merge with existing keys):
sudo tee -a /etc/rancher/k3s/config.yaml >/dev/null <<'YAML'
tls-san:
  - ${name}
  - ${ip}
YAML
sudo systemctl restart k3s
echo | openssl s_client -connect ${name}:6443 2>/dev/null | openssl x509 -noout -ext subjectAltName

# Firewall (remote/public-IP hosts): default deny, trust the tailnet only
sudo ufw default deny incoming && sudo ufw default allow outgoing
sudo ufw allow in on tailscale0
sudo ufw enable
# Never expose 6443, 10250, 30500, 5432 publicly. Never use tailscale funnel.
OUT
    ;;
  workstation)
    [ -n "$name" ] || die "usage: $0 workstation <magicdns-name>"
    cat <<OUT
# === On the operator workstation ===
# 1. Install Tailscale (https://tailscale.com/download) and: tailscale up
# 2. Fetch the kubeconfig over Tailscale SSH and point it at the tailnet name:
ssh company-os 'sudo cat /etc/rancher/k3s/k3s.yaml' > /tmp/k3s.yaml
sed -i 's#https://127.0.0.1:6443#https://${name}:6443#' /tmp/k3s.yaml
kubectl --kubeconfig /tmp/k3s.yaml config rename-context default company-os-tailnet
KUBECONFIG=\$HOME/.kube/config:/tmp/k3s.yaml kubectl config view --flatten > /tmp/merged.yaml
install -m 600 /tmp/merged.yaml \$HOME/.kube/config && rm /tmp/k3s.yaml /tmp/merged.yaml
kubectl config use-context company-os-tailnet
kubectl get nodes
# MCP servers and Claude Code follow the current context; no .mcp.json edits.
# The kubeconfig holds admin credentials: mode 600, never commit or paste it.
OUT
    ;;
  *) die "usage: $0 node <magicdns-name> <tailnet-ip> | workstation <magicdns-name>" ;;
esac
