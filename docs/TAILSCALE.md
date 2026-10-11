# Remote access over Tailscale

Goal: reach the k3s cluster (namespace `company-ops`) from any operator
machine, including Claude Code sessions and MCP servers, without exposing the
Kubernetes API or any node port to the public internet. Everything below is a
**plan to execute by a human with sudo on the host**; nothing here has been
applied. `scripts/tailscale-setup.sh` prints the same steps as commands.

Facts this is built on (see `docs/REMOTE-MIGRATION.md`): single node `pop-os`,
k3s `v1.36.4+k3s1`, API on 6443, kubelet 10250, registry NodePort 30500, no
Ingress, all other traffic is outbound.

## 1. Model

- The k3s node joins your tailnet as `tag:k3s-node` (MagicDNS name, e.g.
  `company-os.<tailnet>.ts.net`, and a `100.x.y.z` address).
- Operators and their workstations join the tailnet (user-owned devices, or
  `tag:operator`).
- kubectl, the Kubernetes MCP and Claude Code reach `https://<magicdns>:6443`
  over the tailnet. The API server certificate must contain that name, so k3s
  gets `--tls-san` entries for it (section 3).
- ACLs allow only `tag:operator`/your user to reach the node on `6443` (and
  SSH). Nothing else is reachable.

## 2. Install Tailscale on the node (today: `pop-os`)

The Tailscale CLI is not installed on the current host and was not installed by
this change. As a user with sudo:

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up --ssh --hostname=company-os --advertise-tags=tag:k3s-node
# first run prints a login URL; approve the device in the admin console.
# --advertise-tags requires tag:k3s-node to exist in tagOwners (section 6),
# otherwise omit it for now and tag the device in the admin console.
tailscale status && tailscale ip -4
```

Enable MagicDNS and HTTPS certificates in the admin console (DNS tab) if you
want `tailscale serve` / `tailscale cert` later. In the same console, disable
key expiry for the node (Machines -> node -> Disable key expiry) so the server
does not fall off the tailnet unattended.

## 3. Make the k3s API server answer on the tailnet name

k3s only serves certificates for names in its SAN list. Add the MagicDNS name
and the tailnet IP (use the real values from `tailscale status`):

```yaml
# /etc/rancher/k3s/config.yaml   (create or merge with existing keys)
tls-san:
  - company-os.<tailnet>.ts.net
  - 100.x.y.z
write-kubeconfig-mode: "0600"
```

Then restart k3s and confirm the cert:

```bash
sudo systemctl restart k3s          # brief API blip; workloads keep running
echo | openssl s_client -connect company-os.<tailnet>.ts.net:6443 2>/dev/null \
  | openssl x509 -noout -ext subjectAltName
```

Do not use `--node-external-ip` or change the flannel interface for this; the
single node keeps using its LAN address for pod networking. Do **not** change
the service CIDR (`10.43.0.0/16`): `KUBERNETES_SERVICE_HOST=10.43.0.1` is
hardcoded in several manifests (see `docs/REMOTE-MIGRATION.md` section 1).

## 4. Operator workstation: join and build a tailnet kubeconfig

On each workstation, install Tailscale (`tailscale up`), then:

```bash
# get the kubeconfig contents from the node (over SSH on the tailnet)
ssh company-os 'sudo cat /etc/rancher/k3s/k3s.yaml' > /tmp/k3s.yaml
# point it at the tailnet name and give it a stable context name
sed -i 's#https://127.0.0.1:6443#https://company-os.<tailnet>.ts.net:6443#' /tmp/k3s.yaml
KUBECONFIG=~/.kube/config:/tmp/k3s.yaml kubectl config view --flatten > /tmp/merged.yaml
# rename the imported context/cluster/user from "default" first if names collide:
#   kubectl --kubeconfig /tmp/k3s.yaml config rename-context default company-os-tailnet
install -m 600 /tmp/merged.yaml ~/.kube/config && rm /tmp/k3s.yaml /tmp/merged.yaml
kubectl config use-context company-os-tailnet
kubectl get nodes
```

The file contains the cluster admin client cert/key: keep it mode 600, never
commit it, never paste it into tickets or chat. Prefer a dedicated
least-privilege ServiceAccount kubeconfig for agents that only need
`company-ops` (the in-cluster `company-ops` ServiceAccount in `k8s/rbac.yaml`
is the model) over handing every machine the admin credential.

## 5. How MCP servers and Claude Code use it

- Operator-host MCP entries in `~/.mcp.json` (`hive`,
  `companyos-engineering-builder`, `companyos-container-manager`) run
  `kubectl --kubeconfig ~/.kube/config exec ...`, and the `kubernetes` MCP
  (`mcp-server-kubernetes`) reads the same kubeconfig. They all follow the
  **current context**, so switching to the tailnet context is the only change:
  `kubectl config use-context company-os-tailnet`. No `.mcp.json` edits.
- Claude Code sessions inherit the shell's kubeconfig. If a session must be
  pinned regardless of current context, export
  `KUBECONFIG=~/.kube/config` and pass `--context company-os-tailnet` in
  commands, or set the MCP entry's `env` to a dedicated kubeconfig path.
- In-cluster agents (engineering pods) are unaffected: they use the in-cluster
  service account and `KUBERNETES_SERVICE_HOST=10.43.0.1`; the tailnet never
  carries their traffic.
- Check connectivity before blaming a tool: `tailscale status`,
  `tailscale ping company-os`, `kubectl --request-timeout=5s get nodes`.

## 6. Tailnet policy suggestions (admin console -> Access controls)

```jsonc
{
  "tagOwners": {
    "tag:k3s-node": ["autogroup:admin"],
    "tag:operator": ["autogroup:admin"]
  },
  "grants": [
    // operators may reach only the k8s API on the cluster node
    { "src": ["tag:operator", "autogroup:member"], "dst": ["tag:k3s-node"], "ip": ["tcp:6443"] }
  ],
  "ssh": [
    {
      "action": "check",
      "src": ["autogroup:member"],
      "dst": ["tag:k3s-node"],
      "users": ["autogroup:nonroot"]
    }
  ]
}
```

Adjust `autogroup:member` to the specific user/group that should have access.
Tag the node, not individual people; tagged devices do not inherit a user's
identity. Tailscale SSH (`--ssh`) replaces opening port 22 publicly.

Optional: `tailscale serve` can publish a node-local HTTP service to the
tailnet only (HTTPS with an automatic cert), e.g. a registry UI or an
`kubectl port-forward` target:

```bash
tailscale serve --bg --https=443 http://127.0.0.1:8080   # tailnet only
tailscale serve status
```

Never use `tailscale funnel` for anything in this cluster; Funnel publishes to
the public internet.

## 7. Ports that must NOT be reachable from the public internet

| Port | What | Rule |
|---|---|---|
| 6443 | k8s API (admin credentials) | tailnet/LAN only, never public |
| 10250 | kubelet API | node-local/cluster only |
| 30500 | in-cluster registry NodePort (unauthenticated push/pull) | node-local/LAN only; on a public-IP host firewall it off |
| 2379-2380 | etcd (if k3s is ever switched to HA/etcd) | never |
| 5432 | postgres (ClusterIP only; do not add NodePort/hostPort) | never exposed; use `kubectl port-forward` |
| 8642 / 4100 / 4200 | hermes API, hive coordinator (no auth), pm-agent | ClusterIP only; hive has no auth by design |
| 22 | SSH | use Tailscale SSH; close public 22 on a remote host |

On the remote host use a default-deny inbound firewall with the tailscale
interface trusted, for example (ufw):

```bash
sudo ufw default deny incoming && sudo ufw default allow outgoing
sudo ufw allow in on tailscale0
sudo ufw allow 41641/udp   # optional: direct WireGuard paths; relays work without it
sudo ufw enable
```

Verify from a machine **off** the tailnet that `6443`, `10250`, `30500` and
`22` on the host's public address time out.

## 8. Order of operations with the migration

Install Tailscale **before** the cutover so rehearsal and cutover run over it:

1. Remote host: provision OS, install Tailscale, join tailnet, firewall
   (sections 2 and 7). Verify `ssh company-os-remote` works over Tailscale.
2. Remote host: install k3s with the SAN flags from the start
   (`INSTALL_K3S_VERSION=v1.36.4+k3s1`, `tls-san` in
   `/etc/rancher/k3s/config.yaml` written **before** installing, so no restart
   is needed). Use a *different* hostname for the new node (e.g.
   `company-os-remote`) while both clusters exist.
3. Workstation: add the new cluster as a second context; run the migration
   runbook entirely with `--context` over the tailnet.
4. After cutover passes, optionally retag/rename so `company-os` points at the
   new host and retire the old node from the tailnet (delete the device in the
   admin console) once it is decommissioned.
5. The old host (`pop-os`) can get Tailscale today with the same steps, which
   also gives you remote access during the weeks before the move.

## 9. Verification checklist

- `tailscale status` shows the node and your workstation online.
- `kubectl --context company-os-tailnet get nodes` works from outside the LAN.
- `openssl ... -ext subjectAltName` lists the MagicDNS name.
- From off-tailnet, `nc -vz <public-ip> 6443` fails.
- An MCP call (e.g. kubernetes `list pods`) succeeds with the tailnet context.

Not covered: Tailscale Kubernetes operator / API server proxy (an alternative
to exposing 6443 on the node, worth it only if you later want per-user
impersonation), and subnet routing for the LAN.
