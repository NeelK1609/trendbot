"""Azure Functions entry points: the daily paper-trading run and the status page."""

import logging
import os

import azure.functions as func

from trendbot.paper import run_daily
from trendbot.status import render_status
from trendbot.store import BlobStore

app = func.FunctionApp()


def _store() -> BlobStore:
    return BlobStore(os.environ["PAPER_STORAGE_ACCOUNT"])


# 00:10, 06:10 and 18:10 UTC. The first run does the work; the later ones only retry if it failed
# (a run with nothing left to process does nothing).
@app.timer_trigger(schedule="0 10 0,6,18 * * *", arg_name="timer", run_on_startup=False, use_monitor=True)
def daily_paper_run(timer: func.TimerRequest) -> None:
    records = run_daily(_store())
    if not records:
        logging.info("Paper run: nothing to process")
    for r in records:
        actions = ", ".join(f"{a['kind']} {a.get('symbol', '')}".strip() for a in r["actions"]) or "no trades"
        logging.info("Paper run %s: account %.2f CAD, %s", r["day"], r["equity"], actions)


@app.route(route="status", methods=["GET"], auth_level=func.AuthLevel.FUNCTION)
def status(req: func.HttpRequest) -> func.HttpResponse:
    state, _ = _store().load()
    return func.HttpResponse(render_status(state), mimetype="text/html", headers={"Cache-Control": "no-store"})
