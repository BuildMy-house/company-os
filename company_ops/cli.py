from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .axiom_client import AxiomClient
from .ledger import Ledger
from .routing import choose_provider
from .telemetry import Telemetry
from .workers import load_workers, run_worker


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="company-ops")
    parser.add_argument("--db", default=os.environ.get("COMPANY_DATABASE_URL") or os.environ.get("TEST_COMPANY_DATABASE_URL", ""))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    sub.add_parser("status")
    plan = sub.add_parser("plan"); plan.add_argument("goal"); plan.add_argument("--owner", default="hermes"); plan.add_argument("--budget", type=int, default=0)
    charge = sub.add_parser("charge"); charge.add_argument("agent"); charge.add_argument("action_type"); charge.add_argument("--key"); charge.add_argument("--plan")
    revenue = sub.add_parser("revenue"); revenue.add_argument("net_cents", type=int); revenue.add_argument("source")
    route = sub.add_parser("route"); route.add_argument("task_type"); route.add_argument("--no-free", action="store_true")
    tl = sub.add_parser("telemetry"); tl.add_argument("event"); tl.add_argument("--field", action="append", default=[])
    tlq = sub.add_parser("telemetry-query"); tlq.add_argument("aql", nargs="?", help="AQL query string"); tlq.add_argument("--metric", choices=["errors", "performance", "usage"], help="Pre-built summary"); tlq.add_argument("--timeframe", default="24h", help="Time range (e.g. 24h, 7d)"); tlq.add_argument("--dataset", default=None, help="Axiom dataset name"); tlq.add_argument("--limit", type=int, default=50, help="Max rows")
    worker = sub.add_parser("worker"); worker.add_argument("task_type"); worker.add_argument("prompt"); worker.add_argument("--worker", default="auto"); worker.add_argument("--config", default="mcp-workers.json"); worker.add_argument("--execute", action="store_true"); worker.add_argument("--no-free", action="store_true"); worker.add_argument("--timeout", type=float, default=120)
    args = parser.parse_args(argv)
    if args.command == "route":
        print(json.dumps(choose_provider(args.task_type, not args.no_free), sort_keys=True)); return 0
    if args.command == "telemetry":
        fields = dict(item.split("=", 1) for item in args.field)
        print(Telemetry().emit(args.event, **fields)); return 0
    if args.command == "telemetry-query":
        client = AxiomClient(dataset=args.dataset)
        if not client.token:
            print(json.dumps({"error": "AXIOM_TOKEN not set"})); return 2
        if args.metric:
            result = client.summary(args.metric, timeframe=args.timeframe)
        elif args.aql:
            result = client.query(args.aql, timeframe=args.timeframe)
        else:
            print(json.dumps({"error": "Provide an AQL query or --metric"})); return 2
        tables = result.get("tables", [])
        if tables:
            rows = tables[0].get("columns", {}).get("columns", [])
            if isinstance(rows, list):
                print(json.dumps(rows[:args.limit], default=str, sort_keys=True, indent=2))
            else:
                print(json.dumps(result, default=str, sort_keys=True, indent=2))
        else:
            print(json.dumps(result, default=str, sort_keys=True, indent=2))
        return 0
    if args.command == "worker":
        config = Path(args.config)
        if not config.exists():
            print(json.dumps({"status": "failed", "error": f"MCP worker config not found: {config}"})); return 2
        result = run_worker(args.worker, args.task_type, args.prompt, load_workers(config), not args.no_free, not args.execute, args.timeout)
        print(json.dumps(result.as_dict(), default=str, sort_keys=True)); return 0 if result.status != "failed" else 1
    ledger = Ledger(args.db); ledger.init()
    try:
        if args.command == "init": result = {"dsn": args.db, "status": "ready"}
        elif args.command == "status": result = ledger.status()
        elif args.command == "plan": result = {"id": ledger.create_plan(args.goal, args.owner, args.budget)}
        elif args.command == "charge": result = ledger.charge(args.agent, args.action_type, args.plan, args.key)
        elif args.command == "revenue": result = ledger.record_revenue(args.net_cents, args.source)
        else: parser.error("unknown command")
        print(json.dumps(result, default=str, sort_keys=True)); return 0
    finally: ledger.close()
