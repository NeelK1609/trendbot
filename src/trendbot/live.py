"""Live trading on Kraken: the backtested engine decides, this module turns decisions into real (or validated) orders.

Modes, stored in the account:
- "shadow": real keys and balances; every order is checked by Kraken with validate=true but never executed,
  and fills are simulated at the live bid/ask plus fees. Proves the plumbing with no risk.
- "live": real market orders. Only reachable through `go_live` with an explicit confirmation.

Any order failure, or exchange balances that don't cover the bot's book, rolls back the unexecuted decisions
and pauses trading until a person resumes it.
"""

from __future__ import annotations

import copy
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta

import pandas as pd

from .data import SYMBOLS
from .engine import Action, Costs, Portfolio, Position, Trade, step
from .kraken import Fill, KrakenClient, KrakenError
from .paper import MANUAL_REVIEW, Fetcher, _day_record, fetch_kraken
from .store import Store
from .strategy import StrategyConfig, add_indicators

GO_LIVE_PHRASE = "I understand this trades real money"
COIN_CODE = {"BTC": 1, "ETH": 2}
SIDE_CODE = {"buy": 1, "sell": 2, "trim": 3}


def userref(day: date, symbol: str, kind: str) -> int:
    """A tag unique to (day, coin, action), e.g. 2026102412 = Oct 24 2026, BTC, sell. Fits Kraken's int32."""
    return int(day.strftime("%Y%m%d")) * 100 + COIN_CODE[symbol] * 10 + SIDE_CODE[kind]


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def open_account(store: Store, client: KrakenClient, capital: float, start: date,
                 cfg: StrategyConfig = StrategyConfig(), costs: Costs = Costs()) -> dict:
    """Opens a shadow account managing `capital` CAD. Requires that much CAD on Kraken."""
    if store.load()[0] is not None:
        raise RuntimeError("A live account already exists.")
    cad = client.balances()["CAD"]
    if cad < capital:
        raise RuntimeError(f"Kraken holds ${cad:,.2f} CAD, less than the ${capital:,.2f} you asked the bot to manage.")
    state = {
        "mode": "shadow",
        "started_on": start.isoformat(),
        "initial_cash": capital,
        "last_day": (start - timedelta(days=1)).isoformat(),
        "config": asdict(cfg),
        "costs": asdict(costs),
        "portfolio": Portfolio.new(capital, list(SYMBOLS)).to_dict(),
        "days": [],
        "orders": [],
        "events": [{"at": _now(), "kind": "opened", "detail": f"Shadow account opened to manage ${capital:,.2f} CAD"}],
    }
    store.save(state, None)
    return state


def go_live(store: Store, confirmation: str) -> None:
    if confirmation != GO_LIVE_PHRASE:
        raise RuntimeError(f'To trade real money, confirm with exactly: "{GO_LIVE_PHRASE}"')
    state, version = store.load()
    if state is None:
        raise RuntimeError("Open a shadow account first.")
    state["mode"] = "live"
    state["events"].append({"at": _now(), "kind": "live", "detail": "Switched from shadow to live: real orders from the next run"})
    store.save(state, version)


def pause(store: Store, reason: str) -> None:
    state, version = store.load()
    pf = Portfolio.from_dict(state["portfolio"])
    pf.paused_until = date.max
    state["portfolio"] = pf.to_dict()
    state["events"].append({"at": _now(), "kind": "paused", "detail": reason})
    store.save(state, version)


# --- Reverting a planned decision that didn't execute -------------------------------------------------------------

def _find_closed(pf: Portfolio, day: date, symbol: str, reason_is_trim: bool) -> int:
    for i in range(len(pf.trades) - 1, -1, -1):
        t = pf.trades[i]
        if t.symbol == symbol and t.exit_date == day and (t.reason == "trim_to_cap") == reason_is_trim:
            return i
    raise LookupError(f"No planned {'trim' if reason_is_trim else 'sale'} of {symbol} on {day}")


def _revert(pf: Portfolio, snapshot: Portfolio, day: date, action: Action) -> None:
    proceeds = action.quantity * action.price - action.fee
    if action.kind == "buy":
        pos = pf.positions.pop(action.symbol)
        pf.cash += pos.trade.quantity * pos.trade.entry_price + pos.trade.entry_fee
        pf.armed[action.symbol] = snapshot.armed.get(action.symbol, True)
    elif action.kind == "sell":
        trade = pf.trades.pop(_find_closed(pf, day, action.symbol, reason_is_trim=False))
        trade.exit_date = trade.exit_price = trade.reason = None
        trade.exit_fee = 0.0
        pf.positions[action.symbol] = Position(trade, snapshot.positions[action.symbol].highest_close)
        pf.cash -= proceeds
        pf.armed[action.symbol] = snapshot.armed.get(action.symbol, True)
    elif action.kind == "trim":
        trim = pf.trades.pop(_find_closed(pf, day, action.symbol, reason_is_trim=True))
        pos = pf.positions[action.symbol]
        pos.trade.quantity += trim.quantity
        pos.trade.entry_fee += trim.entry_fee
        pf.cash -= proceeds


def _apply_fill(pf: Portfolio, day: date, action: Action, fill: Fill) -> None:
    """Replace the engine's modelled fill with what actually happened."""
    if action.kind == "buy":
        trade = pf.positions[action.symbol].trade
        pf.cash += (trade.quantity * trade.entry_price + trade.entry_fee) - (fill.cost + fill.fee)
        trade.quantity, trade.entry_price, trade.entry_fee = fill.volume, fill.price, fill.fee
    else:
        trade = pf.trades[_find_closed(pf, day, action.symbol, reason_is_trim=action.kind == "trim")]
        modelled = action.quantity * action.price - action.fee
        pf.cash += (fill.cost - fill.fee) - modelled
        if action.kind == "trim":
            pf.positions[action.symbol].trade.quantity += trade.quantity - fill.volume
        trade.quantity, trade.exit_price, trade.exit_fee = fill.volume, fill.price, fill.fee


# --- The daily run -------------------------------------------------------------------------------------------------

def _execute(client: KrakenClient, state: dict, day: date, action: Action, rules: dict, available_cash: float,
             real: bool) -> Fill | None:
    """Places one order, or finds the one a previous attempt already placed. None means skipped (too small).

    `available_cash` is the bot's CAD before this order, the most a buy may spend including fees.
    """
    side = "buy" if action.kind == "buy" else "sell"
    ref = userref(day, action.symbol, action.kind)
    if real and (existing := client.find_order(ref)):
        return existing

    pair = rules[action.symbol]
    bid, ask = client.ticker(action.symbol)
    volume = pair.round_volume(action.quantity)
    if action.kind == "buy":
        # The live ask can sit a little above the close the engine planned with: shrink to fit the cash.
        volume = min(volume, pair.round_volume(available_cash / (ask * (1 + state["costs"]["fee_rate"]))))
    if volume < pair.order_min or float(volume) * (ask if side == "buy" else bid) < float(pair.cost_min):
        if action.kind == "buy":
            return None
        raise KrakenError(f"{action.symbol} {side} of {volume} is below Kraken's minimum order")

    fill = client.market_order(action.symbol, side, volume, ref, validate=not real)
    if fill is None:  # shadow: Kraken accepted the order; simulate the fill at the touch price
        price = ask if side == "buy" else bid
        cost = float(volume) * price
        fill = Fill(f"shadow-{ref}", float(volume), price, cost, cost * state["costs"]["fee_rate"])
    state["orders"].append({"at": _now(), "day": day.isoformat(), "userref": ref, "symbol": action.symbol, "side": side,
                            "reason": action.reason, "mode": "live" if real else "shadow", **asdict(fill)})
    return fill


def _balances_cover_book(client: KrakenClient, pf: Portfolio) -> str | None:
    balances = client.balances()
    if balances["CAD"] + 0.01 < pf.cash:
        return f"Kraken holds ${balances['CAD']:,.2f} CAD but the bot's book has ${pf.cash:,.2f}"
    for symbol, pos in pf.positions.items():
        if balances[symbol] + 1e-9 < pos.trade.quantity:
            return f"Kraken holds {balances[symbol]:.8f} {symbol} but the bot's book has {pos.trade.quantity:.8f}"
    return None


def run_live_daily(store: Store, client: KrakenClient, fetch: Fetcher = fetch_kraken, today: date | None = None,
                   allow_real_orders: bool = False) -> list[dict]:
    """Real orders need two switches: the account in "live" mode and `allow_real_orders` (the LIVE_TRADING_ENABLED
    app setting). With either off, orders are only validated by Kraken and fills are simulated."""
    from .data import today_utc

    today = today or today_utc()
    through = today - timedelta(days=1)
    state, version = store.load()
    if state is None:
        return []
    last_day = date.fromisoformat(state["last_day"])
    if last_day >= through:
        return []

    pf = Portfolio.from_dict(state["portfolio"])
    if pf.paused_until is not None:
        return []  # waiting for a person; the engine would also do nothing

    real = state["mode"] == "live" and allow_real_orders
    if real and (problem := _balances_cover_book(client, pf)):
        pf.paused_until = date.max
        state["portfolio"] = pf.to_dict()
        state["events"].append({"at": _now(), "kind": "paused", "detail": f"Balances don't match: {problem}. Review, then resume."})
        store.save(state, version)
        return []

    cfg, costs = StrategyConfig(**state["config"]), Costs(**state["costs"])
    frames = {}
    for symbol, df in fetch().items():
        days = pd.date_range(df.index.min(), pd.Timestamp(through), freq="D", name="date")
        frames[symbol] = add_indicators(df.reindex(days), cfg)
    rules = {s: client.pair_rules(s) for s in SYMBOLS}

    records = []
    day = last_day + timedelta(days=1)
    while day <= through:
        ts = pd.Timestamp(day)
        rows = {s: f.loc[ts] if ts in f.index else None for s, f in frames.items()}
        snapshot = copy.deepcopy(pf)
        report = step(pf, ts, rows, cfg, costs, MANUAL_REVIEW)

        failed: str | None = None
        orders = [a for a in report.actions if a.kind in ("sell", "trim", "buy")]
        for i, action in enumerate(orders):
            available = pf.cash
            if action.kind == "buy":
                planned = pf.positions[action.symbol].trade
                available += planned.quantity * planned.entry_price + planned.entry_fee
            try:
                fill = _execute(client, state, day, action, rules, available, real)
            except (KrakenError, OSError) as e:
                failed = f"{action.kind} {action.symbol} failed: {e}"
                for undone in orders[i:]:
                    _revert(pf, snapshot, day, undone)
                    report.actions.remove(undone)
                report.actions.append(Action("failed", action.symbol, reason=failed))
                break
            if fill is None:
                _revert(pf, snapshot, day, action)
                report.actions.remove(action)
                state["events"].append({"at": _now(), "kind": "skipped",
                                        "detail": f"Skipped buying {action.symbol} on {day}: below Kraken's minimum order"})
            else:
                _apply_fill(pf, day, action, fill)

        record = _day_record(report)
        record["equity"] = round(pf.equity(), 2)
        record["cash"] = round(pf.cash, 2)
        record["invested"] = round(pf.invested(), 2)
        records.append(record)
        state["last_day"] = day.isoformat()
        if failed:
            pf.paused_until = date.max
            record["paused"] = True
            state["events"].append({"at": _now(), "kind": "paused", "detail": f"Trading paused on {day}: {failed}. Review, then resume."})
            break
        if any(a.kind == "halt" for a in report.actions):
            state["events"].append({"at": _now(), "kind": "halted",
                                    "detail": f"Trading paused on {day}: account fell {cfg.max_drawdown:.0%} from its peak. Review, then resume."})
        day += timedelta(days=1)

    state["portfolio"] = pf.to_dict()
    state["days"].extend(records)
    store.save(state, version)
    return records
