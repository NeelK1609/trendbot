"""Runs the shared daily engine over history, and the buy-and-hold benchmarks it's compared against."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import pandas as pd

from .engine import Costs, HaltPolicy, Portfolio, Trade, step
from .strategy import StrategyConfig, add_indicators


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

    pf = Portfolio.new(initial_cash, list(frames))
    equity_curve, exposure_curve = {}, {}
    for day in days:
        report = step(pf, day, {s: f.loc[day] if day in f.index else None for s, f in frames.items()}, cfg, costs, halt)
        equity_curve[day] = report.equity
        exposure_curve[day] = report.invested / report.equity if report.equity else 0.0

    return BacktestResult(
        equity=pd.Series(equity_curve, name="equity"),
        exposure=pd.Series(exposure_curve, name="exposure"),
        trades=pf.trades,
        halts=pf.halts,
        config=cfg,
        costs=costs,
        initial_cash=initial_cash,
        open_positions={s: p.trade for s, p in pf.positions.items()},
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
