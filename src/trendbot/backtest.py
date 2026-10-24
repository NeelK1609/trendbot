"""Day-by-day simulation of one CAD account trading BTC and ETH with the strategy rules.

Decisions use the completed daily candle and fill at that day's close, adjusted for slippage and
fees: the bot runs minutes after the UTC daily close, so the next open is effectively the same price.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

import pandas as pd

from .strategy import (
    StrategyConfig,
    trim_value,
    add_indicators,
    drawdown_breached,
    entry_signal,
    exit_reason,
    has_valid_data,
    position_value,
)


@dataclass(frozen=True)
class Costs:
    fee_rate: float = 0.004  # Kraken Pro taker fee at the entry volume tier
    slippage: float = 0.001  # paid on each side; buy/sell ~1% round trip in total with fees


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


@dataclass(frozen=True)
class HaltPolicy:
    """What happens after the 20% drawdown halt. Live, a person reviews and resumes; backtests need a rule."""

    mode: str = "review"  # "stop": never trade again; "review": pause, then resume; "ignore": no halt
    review_days: int = 30


@dataclass
class BacktestResult:
    equity: pd.Series
    exposure: pd.Series  # fraction of equity invested at each close
    trades: list[Trade]
    halts: list[date]
    config: StrategyConfig
    costs: Costs
    initial_cash: float
    open_positions: dict[str, Trade] = field(default_factory=dict)


def run_backtest(
    candles: dict[str, pd.DataFrame],
    cfg: StrategyConfig = StrategyConfig(),
    costs: Costs = Costs(),
    start: date | None = None,
    end: date | None = None,
    initial_cash: float = 10_000.0,
    halt: HaltPolicy = HaltPolicy(),
) -> BacktestResult:
    """Indicators are computed on all data provided (warm-up included); trading happens in [start, end]."""
    frames = {s: add_indicators(df, cfg) for s, df in candles.items()}
    days = sorted(set().union(*(f.index for f in frames.values())))
    days = [d for d in days if (start is None or d.date() >= start) and (end is None or d.date() <= end)]

    cash = initial_cash
    positions: dict[str, Position] = {}
    armed = {s: True for s in frames}
    last_close: dict[str, float] = {}
    trades: list[Trade] = []
    peak = initial_cash
    halts: list[date] = []
    paused_until: date | None = None
    equity_curve, exposure_curve = {}, {}

    def sell(symbol: str, row: pd.Series, day: pd.Timestamp, reason: str) -> None:
        nonlocal cash
        pos = positions.pop(symbol)
        price = row["close"] * (1 - costs.slippage)
        fee = pos.trade.quantity * price * costs.fee_rate
        cash += pos.trade.quantity * price - fee
        pos.trade.exit_date, pos.trade.exit_price, pos.trade.exit_fee, pos.trade.reason = day.date(), price, fee, reason
        trades.append(pos.trade)
        armed[symbol] = not cfg.rearm_after_exit

    def trim(symbol: str, row: pd.Series, day: pd.Timestamp, value: float) -> None:
        """Sell part of a position, booked as its own closed trade so P&L stays exact."""
        nonlocal cash
        pos = positions[symbol]
        price = row["close"] * (1 - costs.slippage)
        quantity = min(value / row["close"], pos.trade.quantity)
        fraction = quantity / pos.trade.quantity
        fee = quantity * price * costs.fee_rate
        cash += quantity * price - fee
        trades.append(Trade(symbol, pos.trade.entry_date, pos.trade.entry_price, quantity,
                            pos.trade.entry_fee * fraction, day.date(), price, fee, "trim_to_cap"))
        pos.trade.quantity -= quantity
        pos.trade.entry_fee *= 1 - fraction

    for day in days:
        rows = {s: f.loc[day] if day in f.index else None for s, f in frames.items()}
        valid = {s: r is not None and has_valid_data(r) for s, r in rows.items()}
        for s, r in rows.items():
            if valid[s]:
                last_close[s] = r["close"]

        # 1. Exits (only on good data; otherwise hold and wait for good data).
        for symbol in list(positions):
            row = rows[symbol]
            if not valid[symbol]:
                continue
            pos = positions[symbol]
            pos.highest_close = max(pos.highest_close, row["close"])
            if reason := exit_reason(row, pos.highest_close, cfg):
                sell(symbol, row, day, reason)

        equity = cash + sum(p.trade.quantity * last_close[s] for s, p in positions.items())

        # 2. Keep every coin under the per-coin cap.
        for symbol in list(positions):
            if valid[symbol] and (excess := trim_value(positions[symbol].trade.quantity * last_close[symbol], equity, cfg)) > 1:
                trim(symbol, rows[symbol], day, excess)
        equity = cash + sum(p.trade.quantity * last_close[s] for s, p in positions.items())

        # 3. Safety: a 20% fall from the peak closes everything and pauses trading for review.
        if paused_until is not None and day.date() >= paused_until:
            paused_until, peak = None, equity  # reviewed: resume, measuring drawdown from here
        peak = max(peak, equity)
        if halt.mode != "ignore" and paused_until is None and drawdown_breached(equity, peak, cfg):
            for symbol in list(positions):
                if valid[symbol]:
                    sell(symbol, rows[symbol], day, "drawdown_halt")
            halts.append(day.date())
            paused_until = date.max if halt.mode == "stop" else day.date() + timedelta(days=halt.review_days)
            equity = cash + sum(p.trade.quantity * last_close[s] for s, p in positions.items())

        # 4. Entries. Re-arming is tracked even while paused, so a reset during review counts.
        for symbol, row in rows.items():
            if symbol in positions or not valid[symbol]:
                continue
            if not entry_signal(row):
                armed[symbol] = True
                continue
            if paused_until is None and armed[symbol]:
                price = row["close"] * (1 + costs.slippage)
                budget = min(position_value(equity, row["close"], row["atr"], cfg), cash / (1 + costs.fee_rate))
                if budget < 1:
                    continue
                quantity = budget / price
                fee = budget * costs.fee_rate
                cash -= budget + fee
                positions[symbol] = Position(Trade(symbol, day.date(), price, quantity, fee), row["close"])

        invested = sum(p.trade.quantity * last_close[s] for s, p in positions.items())
        equity = cash + invested
        equity_curve[day] = equity
        exposure_curve[day] = invested / equity if equity else 0.0

    return BacktestResult(
        equity=pd.Series(equity_curve, name="equity"),
        exposure=pd.Series(exposure_curve, name="exposure"),
        trades=trades,
        halts=halts,
        config=cfg,
        costs=costs,
        initial_cash=initial_cash,
        open_positions={s: p.trade for s, p in positions.items()},
    )


def buy_and_hold(
    candles: dict[str, pd.DataFrame], weights: dict[str, float], start: date, end: date | None = None,
    costs: Costs = Costs(), initial_cash: float = 10_000.0,
) -> pd.Series:
    """Benchmark: buy once at the first close (paying fees and slippage) and never sell."""
    curve = None
    for symbol, weight in weights.items():
        close = candles[symbol]["close"].loc[pd.Timestamp(start) : pd.Timestamp(end) if end else None].ffill()
        units = initial_cash * weight * (1 - costs.fee_rate) / (close.iloc[0] * (1 + costs.slippage))
        value = units * close
        curve = value if curve is None else curve.add(value, fill_value=0)
    return curve.rename("equity")
