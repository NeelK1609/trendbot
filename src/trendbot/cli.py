"""Command line: `trendbot research`, `trendbot paper ...`, `trendbot live ...`."""

from __future__ import annotations

import argparse
import os
import sys
import webbrowser
from pathlib import Path

from .store import BlobStore, LocalStore, Store


def _store(args: argparse.Namespace, container: str) -> Store:
    if args.azure:
        return BlobStore(args.azure, container)
    return LocalStore(Path(args.local))


def _print_records(records: list[dict]) -> None:
    if not records:
        print("Nothing to do: every completed day has been processed, or trading is paused.")
    for r in records:
        actions = ", ".join(f"{a['kind']} {a.get('symbol') or ''}".strip() for a in r["actions"]) or "no trades"
        print(f"{r['day']}: account ${r['equity']:,.2f} · {actions}")


def _show(state: dict | None, name: str, empty: str) -> None:
    from .status import render_status

    out = Path(f"reports/{name}-status.html")
    out.parent.mkdir(exist_ok=True)
    out.write_text(render_status(state, empty=empty))
    print(f"Wrote {out}")
    webbrowser.open(out.resolve().as_uri())


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="trendbot")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("research", help="Run the full backtest and write reports/backtest.json")

    for name, actions, default in [
        ("paper", ["run", "status", "resume"], "data/paper-account.json"),
        ("live", ["open", "run", "status", "go-live", "pause", "resume"], "data/live-account.json"),
    ]:
        sub = commands.add_parser(name, help=f"{name.title()} trading")
        sub.add_argument("action", choices=actions)
        where = sub.add_mutually_exclusive_group()
        where.add_argument("--local", default=default, help="Account file (default: %(default)s)")
        where.add_argument("--azure", default=os.environ.get("TRENDBOT_STORAGE_ACCOUNT"), help="Azure storage account name")
        sub.add_argument("--note", help="resume: what you reviewed")
        if name == "live":
            sub.add_argument("--vault", default=os.environ.get("KRAKEN_VAULT_URL"), help="Key Vault URL holding the Kraken API key")
            sub.add_argument("--capital", type=float, help="open: CAD the bot may manage")
            sub.add_argument("--confirm", help="go-live: the confirmation phrase")
            sub.add_argument("--reason", help="pause: why")

    args = parser.parse_args(argv)
    if args.command == "research":
        from .research import main as research_main

        research_main()
        return

    from .paper import resume

    store = _store(args, "paper" if args.command == "paper" else "live")

    if args.command == "paper":
        from .paper import run_daily

        if args.action == "run":
            _print_records(run_daily(store))
        elif args.action == "status":
            _show(store.load()[0], "paper", "No paper account yet.")
        elif args.action == "resume":
            if not args.note:
                sys.exit('Say what you reviewed, e.g. --note "Checked the drawdown; the rules worked as designed"')
            resume(store, args.note)
            print("Trading resumed.")
        return

    from . import live
    from .data import today_utc
    from .secrets import kraken_from_vault

    def client():
        if not args.vault:
            sys.exit("Set --vault or KRAKEN_VAULT_URL to the Key Vault holding kraken-api-key and kraken-api-secret.")
        return kraken_from_vault(args.vault)

    if args.action == "open":
        if not args.capital or args.capital <= 0:
            sys.exit("Say how much CAD the bot may manage, e.g. --capital 500")
        live.open_account(store, client(), args.capital, today_utc())
        print(f"Shadow account opened to manage ${args.capital:,.2f} CAD. Orders are only validated until you go live.")
    elif args.action == "run":
        allow = os.environ.get("LIVE_TRADING_ENABLED", "").lower() == "true"
        _print_records(live.run_live_daily(store, client(), allow_real_orders=allow))
    elif args.action == "status":
        _show(store.load()[0], "live", "No live account yet. Open one with: trendbot live open --capital 500")
    elif args.action == "go-live":
        live.go_live(store, args.confirm or "")
        print("Account switched to live. Real orders also need LIVE_TRADING_ENABLED=true on the Function App.")
    elif args.action == "pause":
        live.pause(store, args.reason or "Paused by hand")
        print("Trading paused. Resume with: trendbot live resume --note \"...\"")
    elif args.action == "resume":
        if not args.note:
            sys.exit('Say what you reviewed, e.g. --note "Checked balances on Kraken; all match"')
        resume(store, args.note)
        print("Trading resumed.")
