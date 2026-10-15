"""Stand-in for substack-mcp: newline-delimited JSON-RPC over stdio.
argv: <log-file> <mode ok|auth>. The bridge passes a minimal env, so config is via argv."""
import json, os, sys

for line in sys.stdin:
    m = json.loads(line)
    if m.get("method") == "tools/call":
        with open(sys.argv[1], "w") as f:
            json.dump(m["params"], f)
        if sys.argv[2] == "auth":
            res = {"isError": True, "content": [{"type": "text", "text": "SubstackAPIException: 403 Forbidden"}]}
        else:
            res = {"content": [{"type": "text", "text": json.dumps({"ok": True, "env_leak": sorted(k for k in os.environ if "SECRET" in k or "DATABASE" in k)})}]}
        print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": res}), flush=True)
    elif m.get("id") == 1:
        print(json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}), flush=True)
