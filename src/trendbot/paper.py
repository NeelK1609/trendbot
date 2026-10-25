"""Paper trading: the backtested engine run once a day on Kraken's real CAD candles with a pretend account."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta

import pandas as pd

from .data import SYMBOLS, fetch_kraken_cad, today_utc
from .engine import Costs, HaltPolicy, Portfolio, step
from .store import Store
from .strategy import StrategyConfig, add_indicators

Fetcher = Callable[[], dict[str, pd.DataFrame]]

MANUAL_REVIEW = HaltPolicy(mode="manual")


def fetch_kraken() -> dict[str, pd.DataFrame]:
    return {s: fetch_kraken_cad(s) for s in SYMBOLS}


def new_account(start: date, cash: float = 10_000.0, cfg: StrategyConfig = StrategyConfig(), costs: Costs = Costs()) -> dict:
    """An account whose first trading day is `start`. Settings are frozen into the account."""
    return {
        "mode": "paper",
        "started_on": start.isoformat(),
        "initial_cash": cash,
        "last_day": (start - timedelta(days=1)).isoformat(),
        "config": asdict(cfg),
        "costs": asdict(costs),
        "portfolio": Portfolio.new(cash, list(SYMBOLS)).to_dict(),
        "days": [],
        "events": [{"at": datetime.now(UTC).isoformat(timespec="seconds"), "kind": "opened", "detail": f"Paper account opened with ${cash:,.0f} CAD"}],
    }


def _day_record(report) -> dict:
    return {
        "day": report.day.isoformat(),
        "equity": round(report.equity, 2),
        "cash": round(report.cash, 2),
        "invested": round(report.invested, 2),
        "paused": report.paused,
        "actions": [{k: v for k, v in asdict(a).items() if v is not None} for a in report.actions],
        "signals": report.signals,
    }


def run_daily(store: Store, fetch: Fetcher = fetch_kraken, today: date | None = None) -> list[dict]:
    """Processes every completed UTC day since the last run, through yesterday. Safe to run repeatedly."""
    today = today or today_utc()
    through = today - timedelta(days=1)

    state, version = store.load()
    if state is None:
        state = new_account(through)
    last_day = date.fromisoformat(state["last_day"])
    if last_day >= through:
        return []

    cfg, costs = StrategyConfig(**state["config"]), Costs(**state["costs"])
    candles = fetch()
    frames = {}
    for symbol, df in candles.items():
        # Every calendar day through yesterday; a day Kraken didn't report becomes NaN, which blocks trading it.
        days = pd.date_range(df.index.min(), pd.Timestamp(through), freq="D", name="date")
        frames[symbol] = add_indicators(df.reindex(days), cfg)

    pf = Portfolio.from_dict(state["portfolio"])
    records = []
    day = last_day + timedelta(days=1)
    while day <= through:
        ts = pd.Timestamp(day)
        rows = {s: f.loc[ts] if ts in f.index else None for s, f in frames.items()}
        records.append(_day_record(step(pf, ts, rows, cfg, costs, MANUAL_REVIEW)))
        day += timedelta(days=1)

    state["portfolio"] = pf.to_dict()
    state["days"].extend(records)
    state["last_day"] = through.isoformat()
    for record in records:
        for action in record["actions"]:
            if action["kind"] == "halt":
                state["events"].append({"at": datetime.now(UTC).isoformat(timespec="seconds"), "kind": "halted",
                                        "detail": f"Trading paused on {record['day']}: {action['reason']}. Review, then resume."})
    store.save(state, version)
    return records


def open_demo(store: Store, symbol: str, cad: float, ask_price: float, today: date | None = None,
              cfg: StrategyConfig = StrategyConfig(), costs: Costs = Costs()) -> dict:
    """A demo account that buys `symbol` right away with all its cash, outside the rules, to show how trades appear.

    From the next daily run the rules manage the position like any other (they may sell it straight away).
    """
    from .engine import Position, Trade

    today = today or today_utc()
    if store.load()[0] is not None:
        raise RuntimeError("A demo account already exists.")
    state = new_account(today, cad, cfg, costs)
    state["mode"] = "demo"
    pf = Portfolio.from_dict(state["portfolio"])
    quantity = round(cad / (ask_price * (1 + costs.fee_rate)) - 5e-9, 8)  # round down to 8 decimals
    fee = quantity * ask_price * costs.fee_rate
    pf.cash -= quantity * ask_price + fee
    pf.positions[symbol] = Position(Trade(symbol, today, ask_price, quantity, fee), ask_price)
    pf.last_close[symbol] = ask_price
    state["portfolio"] = pf.to_dict()
    now = datetime.now(UTC).isoformat(timespec="seconds")
    state["events"] = [
        {"at": now, "kind": "opened", "detail": f"Demo account opened with ${cad:,.2f} CAD"},
        {"at": now, "kind": "demo_buy", "detail": f"Demo buy outside the rules: {quantity:.8f} {symbol} at ${ask_price:,.2f} "
                                                  f"(Kraken's live ask) for ${quantity * ask_price:,.2f} + ${fee:,.2f} fee. "
                                                  "From the next daily run, the rules manage it."},
    ]
    store.save(state, None)
    return state


def resume(store: Store, note: str) -> None:
    """Records that a person reviewed a drawdown halt and restarts trading."""
    state, version = store.load()
    if state is None:
        raise RuntimeError("There is no paper account yet.")
    pf = Portfolio.from_dict(state["portfolio"])
    if pf.paused_until is None:
        raise RuntimeError("Trading isn't paused.")
    pf.resume()
    state["portfolio"] = pf.to_dict()
    state["events"].append({"at": datetime.now(UTC).isoformat(timespec="seconds"), "kind": "resumed", "detail": note})
    store.save(state, version)
