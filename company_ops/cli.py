from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .axiom_client import AxiomClient
from .ledger import Ledger
from .resource_pools import ResourcePools
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
    deploy = sub.add_parser("deployment")
    deploy_sub = deploy.add_subparsers(dest="deploy_command", required=True)
    deploy_rec = deploy_sub.add_parser("record"); deploy_rec.add_argument("environment"); deploy_rec.add_argument("version"); deploy_rec.add_argument("--status", default="proposed")
    deploy_up = deploy_sub.add_parser("update"); deploy_up.add_argument("deployment_id"); deploy_up.add_argument("status"); deploy_up.add_argument("--result", default=None)
    expense = sub.add_parser("expense")
    expense_sub = expense.add_subparsers(dest="expense_command", required=True)
    exp_add = expense_sub.add_parser("add"); exp_add.add_argument("category"); exp_add.add_argument("amount_cents", type=int); exp_add.add_argument("recurrence"); exp_add.add_argument("--source", default=None)
    expense_sub.add_parser("list")
    worker = sub.add_parser("worker"); worker.add_argument("task_type"); worker.add_argument("prompt"); worker.add_argument("--worker", default="auto"); worker.add_argument("--config", default="mcp-workers.json"); worker.add_argument("--execute", action="store_true"); worker.add_argument("--no-free", action="store_true"); worker.add_argument("--timeout", type=float, default=120)
    rp = sub.add_parser("resource-pool")
    rp_sub = rp.add_subparsers(dest="rp_command", required=True)
    rp_rec = rp_sub.add_parser("record"); rp_rec.add_argument("pool_id"); rp_rec.add_argument("provider"); rp_rec.add_argument("unit"); rp_rec.add_argument("quota_amount", type=float); rp_rec.add_argument("period_type"); rp_rec.add_argument("period_start"); rp_rec.add_argument("period_end_or_reset_at"); rp_rec.add_argument("--consumed", type=float, default=0); rp_rec.add_argument("--source", default=""); rp_rec.add_argument("--level", default="claude_to_worker")
    rp_status = rp_sub.add_parser("status"); rp_status.add_argument("pool_id")
    rp_sub.add_parser("list")
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
    if args.command == "resource-pool":
        rp_conn = ResourcePools(args.db)
        try:
            if args.rp_command == "record":
                rp_conn.record_pool(
                    args.pool_id, args.provider, args.unit, args.quota_amount,
                    args.period_type, args.period_start, args.period_end_or_reset_at,
                    consumed_amount=args.consumed, source=args.source, level=args.level,
                )
                result = {"status": "ok", "pool_id": args.pool_id}
            elif args.rp_command == "status":
                pool_info = rp_conn.get_pool(args.pool_id)
                if pool_info is None:
                    print(json.dumps({"error": f"no resource pool with pool_id={args.pool_id!r}"})); return 1
                remaining = rp_conn.remaining_budget_vs_time(args.pool_id)
                result = {**pool_info, **remaining}
            elif args.rp_command == "list":
                result = rp_conn.list_pools()
            else:
                parser.error("unknown resource-pool subcommand")
            print(json.dumps(result, default=str, sort_keys=True)); return 0
        finally:
            rp_conn.close()
    ledger = Ledger(args.db); ledger.init()
    try:
        if args.command == "init": result = {"dsn": args.db, "status": "ready"}
        elif args.command == "status": result = ledger.status()
        elif args.command == "plan": result = {"id": ledger.create_plan(args.goal, args.owner, args.budget)}
        elif args.command == "charge": result = ledger.charge(args.agent, args.action_type, args.plan, args.key)
        elif args.command == "revenue": result = ledger.record_revenue(args.net_cents, args.source)
        elif args.command == "deployment":
            if args.deploy_command == "record":
                result = {"id": ledger.add_deployment(args.environment, args.version, args.status)}
            elif args.deploy_command == "update":
                ledger.update_deployment_status(args.deployment_id, args.status, args.result)
                result = {"id": args.deployment_id, "status": args.status}
            else: parser.error("unknown deployment subcommand")
        elif args.command == "expense":
            if args.expense_command == "add":
                result = {"id": ledger.add_expense(args.category, args.amount_cents, args.recurrence, args.source)}
            elif args.expense_command == "list":
                result = ledger.list_expenses()
            else: parser.error("unknown expense subcommand")
        else: parser.error("unknown command")
        print(json.dumps(result, default=str, sort_keys=True)); return 0
    finally: ledger.close()
