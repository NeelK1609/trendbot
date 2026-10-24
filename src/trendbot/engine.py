"""The account and its once-a-day decision step, shared by the backtester, the paper trader and the live trader.

Each day: exits, then trimming to the per-coin cap, then the drawdown safety check, then entries.
Fills are at the day's close, adjusted for slippage and fees.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta

import pandas as pd

from .strategy import (
    StrategyConfig,
    drawdown_breached,
    entry_signal,
    exit_reason,
    has_valid_data,
    position_value,
    trailing_stop,
    trim_value,
)


@dataclass(frozen=True)
class Costs:
    fee_rate: float = 0.004  # Kraken Pro taker fee at the entry volume tier
    slippage: float = 0.001  # paid on each side; about 1% round trip in total with fees


@dataclass(frozen=True)
class HaltPolicy:
    """What happens after the 20% drawdown halt.

    "manual": wait for a person to resume (paper and live). "review": resume after `review_days`
    (backtests, standing in for a person). "stop": never trade again. "ignore": no halt.
    """

    mode: str = "review"
    review_days: int = 30


@dataclass
class Trade:
    symbol: str
    entry_date: date
    entry_price: float
    quantity: float
    entry_fee: float
    exit_date: date | None = None
    exit_price: float | None = None
    exit_fee: float = 0.0
    reason: str | None = None

    @property
    def pnl(self) -> float:
        if self.exit_price is None:
            return 0.0
        return self.quantity * (self.exit_price - self.entry_price) - self.entry_fee - self.exit_fee

    @property
    def return_pct(self) -> float:
        cost = self.quantity * self.entry_price + self.entry_fee
        return self.pnl / cost if cost else 0.0


@dataclass
class Position:
    trade: Trade
    highest_close: float


@dataclass
class Action:
    kind: str  # buy, sell, trim, halt
    symbol: str | None
    price: float | None = None
    quantity: float | None = None
    fee: float | None = None
    reason: str | None = None


@dataclass
class DayReport:
    day: date
    equity: float
    cash: float
    invested: float
    paused: bool
    actions: list[Action]
    signals: dict[str, dict]


@dataclass
class Portfolio:
    cash: float
    positions: dict[str, Position] = field(default_factory=dict)
    armed: dict[str, bool] = field(default_factory=dict)
    last_close: dict[str, float] = field(default_factory=dict)
    peak: float = 0.0
    halts: list[date] = field(default_factory=list)
    paused_until: date | None = None
    trades: list[Trade] = field(default_factory=list)

    @classmethod
    def new(cls, cash: float, symbols: list[str]) -> Portfolio:
        return cls(cash=cash, armed={s: True for s in symbols}, peak=cash)

    def equity(self) -> float:
        return self.cash + self.invested()

    def invested(self) -> float:
        return sum(p.trade.quantity * self.last_close[s] for s, p in self.positions.items())

    def resume(self) -> None:
        """A person has reviewed a drawdown halt: trade again, measuring drawdown from here."""
        self.paused_until = None
        self.peak = self.equity()

    # Plain JSON for storage.
    def to_dict(self) -> dict:
        def trade(t: Trade) -> dict:
            d = asdict(t)
            d["entry_date"] = t.entry_date.isoformat()
            d["exit_date"] = t.exit_date.isoformat() if t.exit_date else None
            return d

        return {
            "cash": self.cash,
            "positions": {s: {"trade": trade(p.trade), "highest_close": p.highest_close} for s, p in self.positions.items()},
            "armed": self.armed,
            "last_close": self.last_close,
            "peak": self.peak,
            "halts": [d.isoformat() for d in self.halts],
            "paused_until": self.paused_until.isoformat() if self.paused_until else None,
            "trades": [trade(t) for t in self.trades],
        }

    @classmethod
    def from_dict(cls, d: dict) -> Portfolio:
        def trade(t: dict) -> Trade:
            return Trade(**{**t, "entry_date": date.fromisoformat(t["entry_date"]),
                            "exit_date": date.fromisoformat(t["exit_date"]) if t["exit_date"] else None})

        return cls(
            cash=d["cash"],
            positions={s: Position(trade(p["trade"]), p["highest_close"]) for s, p in d["positions"].items()},
            armed=d["armed"],
            last_close=d["last_close"],
            peak=d["peak"],
            halts=[date.fromisoformat(x) for x in d["halts"]],
            paused_until=date.fromisoformat(d["paused_until"]) if d["paused_until"] else None,
            trades=[trade(t) for t in d["trades"]],
        )


def _clean(v) -> float | None:
    return float(v) if v is not None and isinstance(v, (int, float)) and math.isfinite(v) else None


def step(
    pf: Portfolio,
    day: pd.Timestamp,
    rows: dict[str, pd.Series | None],
    cfg: StrategyConfig,
    costs: Costs,
    halt: HaltPolicy,
) -> DayReport:
    """Applies one day's decisions to the portfolio using rows that already carry indicators."""
    actions: list[Action] = []
    valid = {s: r is not None and has_valid_data(r) for s, r in rows.items()}
    for s, r in rows.items():
        if valid[s]:
            pf.last_close[s] = float(r["close"])

    def sell(symbol: str, row: pd.Series, reason: str) -> None:
        pos = pf.positions.pop(symbol)
        price = float(row["close"]) * (1 - costs.slippage)
        fee = pos.trade.quantity * price * costs.fee_rate
        pf.cash += pos.trade.quantity * price - fee
        pos.trade.exit_date, pos.trade.exit_price, pos.trade.exit_fee, pos.trade.reason = day.date(), price, fee, reason
        pf.trades.append(pos.trade)
        pf.armed[symbol] = not cfg.rearm_after_exit
        actions.append(Action("sell", symbol, price, pos.trade.quantity, fee, reason))

    def trim(symbol: str, row: pd.Series, value: float) -> None:
        """Sell part of a position, booked as its own closed trade so P&L stays exact."""
        pos = pf.positions[symbol]
        price = float(row["close"]) * (1 - costs.slippage)
        quantity = min(value / float(row["close"]), pos.trade.quantity)
        fraction = quantity / pos.trade.quantity
        fee = quantity * price * costs.fee_rate
        pf.cash += quantity * price - fee
        pf.trades.append(Trade(symbol, pos.trade.entry_date, pos.trade.entry_price, quantity,
                               pos.trade.entry_fee * fraction, day.date(), price, fee, "trim_to_cap"))
        pos.trade.quantity -= quantity
        pos.trade.entry_fee *= 1 - fraction
        actions.append(Action("trim", symbol, price, quantity, fee, "trim_to_cap"))

    # 1. Exits, only on good data; otherwise hold and wait for good data.
    for symbol in list(pf.positions):
        if not valid[symbol]:
            continue
        row = rows[symbol]
        pos = pf.positions[symbol]
        pos.highest_close = max(pos.highest_close, float(row["close"]))
        if reason := exit_reason(row, pos.highest_close, cfg):
            sell(symbol, row, reason)

    # 2. Keep every coin under the per-coin cap.
    equity = pf.equity()
    for symbol in list(pf.positions):
        if valid[symbol] and (excess := trim_value(pf.positions[symbol].trade.quantity * pf.last_close[symbol], equity, cfg)) > 1:
            trim(symbol, rows[symbol], excess)
    equity = pf.equity()

    # 3. Safety: a 20% fall from the peak closes everything and pauses trading.
    if pf.paused_until is not None and day.date() >= pf.paused_until:
        pf.paused_until, pf.peak = None, equity
    pf.peak = max(pf.peak, equity)
    if halt.mode != "ignore" and pf.paused_until is None and drawdown_breached(equity, pf.peak, cfg):
        for symbol in list(pf.positions):
            if valid[symbol]:
                sell(symbol, rows[symbol], "drawdown_halt")
        pf.halts.append(day.date())
        pf.paused_until = day.date() + timedelta(days=halt.review_days) if halt.mode == "review" else date.max
        actions.append(Action("halt", None, reason=f"account fell {cfg.max_drawdown:.0%} from its peak"))
        equity = pf.equity()

    # 4. Entries. Re-arming is tracked even while paused, so a reset during review counts.
    signals: dict[str, dict] = {}
    for symbol, row in rows.items():
        signal = bool(valid[symbol] and entry_signal(row))
        signals[symbol] = {
            "valid": valid[symbol],
            "close": _clean(row["close"]) if row is not None else None,
            "sma_fast": _clean(row["sma_fast"]) if row is not None else None,
            "sma_slow": _clean(row["sma_slow"]) if row is not None else None,
            "atr": _clean(row["atr"]) if row is not None else None,
            "entry_signal": signal,
            "stop": trailing_stop(pf.positions[symbol].highest_close, float(row["atr"]), cfg)
            if symbol in pf.positions and valid[symbol] else None,
        }
        if symbol in pf.positions or not valid[symbol]:
            continue
        if not signal:
            pf.armed[symbol] = True
            continue
        if pf.paused_until is None and pf.armed[symbol]:
            price = float(row["close"]) * (1 + costs.slippage)
            budget = min(position_value(equity, float(row["close"]), float(row["atr"]), cfg), pf.cash / (1 + costs.fee_rate))
            if budget < 1:
                continue
            quantity = budget / price
            fee = budget * costs.fee_rate
            pf.cash -= budget + fee
            pf.positions[symbol] = Position(Trade(symbol, day.date(), price, quantity, fee), float(row["close"]))
            actions.append(Action("buy", symbol, price, quantity, fee, "trend_up"))

    invested = pf.invested()
    return DayReport(day.date(), pf.cash + invested, pf.cash, invested, pf.paused_until is not None, actions, signals)
