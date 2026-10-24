"""Command line: `trendbot research`, `trendbot paper run|status|resume`."""

from __future__ import annotations

import argparse
import os
import sys
import webbrowser
from pathlib import Path

from .store import BlobStore, LocalStore, Store


def _store(args: argparse.Namespace) -> Store:
    if args.azure:
        return BlobStore(args.azure)
    return LocalStore(Path(args.local))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="trendbot")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("research", help="Run the full backtest and write reports/backtest.json")

    paper = commands.add_parser("paper", help="Paper trading on Kraken CAD prices")
    paper.add_argument("action", choices=["run", "status", "resume"])
    where = paper.add_mutually_exclusive_group()
    where.add_argument("--local", default="data/paper-account.json", help="Account file (default: %(default)s)")
    where.add_argument("--azure", default=os.environ.get("PAPER_STORAGE_ACCOUNT"), help="Azure storage account name")
    paper.add_argument("--note", help="With resume: what you reviewed")
    args = parser.parse_args(argv)

    if args.command == "research":
        from .research import main as research_main

        research_main()
        return

    from .paper import resume, run_daily
    from .status import render_status

    store = _store(args)
    if args.action == "run":
        records = run_daily(store)
        if not records:
            print("Nothing to do: every completed day has been processed.")
        for r in records:
            actions = ", ".join(f"{a['kind']} {a.get('symbol', '')}".strip() for a in r["actions"]) or "no trades"
            print(f"{r['day']}: account ${r['equity']:,.2f} · {actions}")
    elif args.action == "status":
        state, _ = store.load()
        out = Path("reports/paper-status.html")
        out.parent.mkdir(exist_ok=True)
        out.write_text(render_status(state))
        print(f"Wrote {out}")
        webbrowser.open(out.resolve().as_uri())
    elif args.action == "resume":
        if not args.note:
            sys.exit("Say what you reviewed, e.g. --note \"Checked the drawdown; market crash, rules worked as designed\"")
        resume(store, args.note)
        print("Trading resumed.")
