"""Azure Functions entry points: daily paper and live runs, and their status pages."""

import logging
import os

import azure.functions as func

from trendbot.live import run_live_daily
from trendbot.paper import run_daily
from trendbot.secrets import kraken_from_vault
from trendbot.status import render_status
from trendbot.store import BlobStore

app = func.FunctionApp()


def _store(container: str, blob: str = "account.json") -> BlobStore:
    return BlobStore(os.environ["PAPER_STORAGE_ACCOUNT"], container, blob)


def _log(name: str, records: list[dict]) -> None:
    if not records:
        logging.info("%s run: nothing to process", name)
    for r in records:
        actions = ", ".join(f"{a['kind']} {a.get('symbol') or ''}".strip() for a in r["actions"]) or "no trades"
        logging.info("%s run %s: account %.2f CAD, %s", name, r["day"], r["equity"], actions)


# 00:10, 06:10 and 18:10 UTC. The first run does the work; later ones only retry if it failed
# (a run with nothing left to process does nothing).
@app.timer_trigger(schedule="0 10 0,6,18 * * *", arg_name="timer", run_on_startup=False, use_monitor=True)
def daily_paper_run(timer: func.TimerRequest) -> None:
    _log("Paper", run_daily(_store("paper")))
    demo = _store("paper", "demo.json")
    if demo.load()[0] is not None:
        _log("Demo", run_daily(demo))


# Ten minutes after the paper run. Does nothing until a live account is opened with `trendbot live open`.
# Real orders need the account in live mode AND LIVE_TRADING_ENABLED=true; otherwise orders are only validated.
@app.timer_trigger(schedule="0 20 0,6,18 * * *", arg_name="timer", run_on_startup=False, use_monitor=True)
def daily_live_run(timer: func.TimerRequest) -> None:
    store = _store("live")
    if store.load()[0] is None:
        logging.info("Live run: no live account opened")
        return
    allow = os.environ.get("LIVE_TRADING_ENABLED", "").lower() == "true"
    _log("Live", run_live_daily(store, kraken_from_vault(os.environ["KRAKEN_VAULT_URL"]), allow_real_orders=allow))


@app.route(route="status", methods=["GET"], auth_level=func.AuthLevel.FUNCTION)
def status(req: func.HttpRequest) -> func.HttpResponse:
    state, _ = _store("paper").load()
    return func.HttpResponse(render_status(state), mimetype="text/html", headers={"Cache-Control": "no-store"})


@app.route(route="live-status", methods=["GET"], auth_level=func.AuthLevel.FUNCTION)
def live_status(req: func.HttpRequest) -> func.HttpResponse:
    state, _ = _store("live").load()
    page = render_status(state, empty="No live account yet. Open one with: trendbot live open --capital 500")
    return func.HttpResponse(page, mimetype="text/html", headers={"Cache-Control": "no-store"})


@app.route(route="demo-status", methods=["GET"], auth_level=func.AuthLevel.FUNCTION)
def demo_status(req: func.HttpRequest) -> func.HttpResponse:
    state, _ = _store("paper", "demo.json").load()
    page = render_status(state, empty="No demo account yet. Create one with: trendbot demo buy")
    return func.HttpResponse(page, mimetype="text/html", headers={"Cache-Control": "no-store"})
