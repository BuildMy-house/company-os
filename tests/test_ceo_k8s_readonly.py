"""CEO (hermes-gateway) cluster visibility: wired, and the exposed k8s server is read-only."""
from pathlib import Path

import yaml

CFG = yaml.safe_load((Path(__file__).resolve().parent.parent / "hermes" / "config.yaml").read_text())
SERVERS = CFG["mcp_servers"]


def test_container_manager_gets_in_cluster_host():
    env = SERVERS["container_manager"]["env"]
    assert env["KUBERNETES_SERVICE_HOST"] and env["KUBERNETES_SERVICE_PORT"]


def test_k8s_readonly_is_read_only_and_in_cluster():
    args = SERVERS["k8s_readonly"]["args"]
    assert "--read-only" in args and "--disable-destructive" in args
    assert args[args.index("--cluster-provider") + 1] == "in-cluster"
    assert SERVERS["k8s_readonly"]["env"]["KUBERNETES_SERVICE_HOST"]


def test_api_server_exposes_readonly_not_patch_capable_k8s():
    toolsets = CFG["platform_toolsets"]["api_server"]
    assert toolsets[0] == "hermes-api-server"
    assert "k8s_readonly" in toolsets and "container_manager" in toolsets
    assert "k8s_deployment" not in toolsets
