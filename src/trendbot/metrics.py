"""Performance statistics for backtests and benchmarks."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass

import pandas as pd

from .backtest import BacktestResult
from .engine import Trade

DAYS_PER_YEAR = 365  # crypto trades every day


@dataclass(frozen=True)
class Stats:
    total_return: float
    cagr: float
    max_drawdown: float
    volatility: float
    sharpe: float
    calmar: float

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class TradeStats:
    trades: int
    win_rate: float
    avg_win: float
    avg_loss: float
    profit_factor: float
    avg_holding_days: float
    exits: dict

    def as_dict(self) -> dict:
        return asdict(self)


def equity_stats(equity: pd.Series) -> Stats:
    equity = equity.dropna()
    years = max((equity.index[-1] - equity.index[0]).days / DAYS_PER_YEAR, 1 / DAYS_PER_YEAR)
    total = equity.iloc[-1] / equity.iloc[0] - 1
    cagr = (1 + total) ** (1 / years) - 1
    drawdown = (equity / equity.cummax() - 1).min()
    daily = equity.pct_change().dropna()
    vol = daily.std() * math.sqrt(DAYS_PER_YEAR)
    sharpe = daily.mean() / daily.std() * math.sqrt(DAYS_PER_YEAR) if daily.std() > 0 else 0.0
    calmar = cagr / abs(drawdown) if drawdown < 0 else math.inf
    return Stats(total, cagr, drawdown, vol, sharpe, calmar)


def trade_stats(trades: list[Trade]) -> TradeStats:
    closed = [t for t in trades if t.exit_price is not None]
    if not closed:
        return TradeStats(0, 0.0, 0.0, 0.0, 0.0, 0.0, {})
    wins = [t for t in closed if t.pnl > 0]
    losses = [t for t in closed if t.pnl <= 0]
    gross_win = sum(t.pnl for t in wins)
    gross_loss = -sum(t.pnl for t in losses)
    exits: dict[str, int] = {}
    for t in closed:
        exits[t.reason or "unknown"] = exits.get(t.reason or "unknown", 0) + 1
    return TradeStats(
        trades=len(closed),
        win_rate=len(wins) / len(closed),
        avg_win=sum(t.return_pct for t in wins) / len(wins) if wins else 0.0,
        avg_loss=sum(t.return_pct for t in losses) / len(losses) if losses else 0.0,
        profit_factor=gross_win / gross_loss if gross_loss > 0 else math.inf,
        avg_holding_days=sum((t.exit_date - t.entry_date).days for t in closed) / len(closed),
        exits=exits,
    )


def yearly_returns(equity: pd.Series) -> pd.Series:
    year_end = equity.groupby(equity.index.year).last()
    year_start = equity.groupby(equity.index.year).first()
    prior = year_end.shift(1).fillna(year_start)
    return year_end / prior - 1


def summarize(result: BacktestResult) -> dict:
    return {
        "equity": equity_stats(result.equity).as_dict(),
        "trades": trade_stats(result.trades).as_dict(),
        "avg_exposure": float(result.exposure.mean()),
        "halts": [d.isoformat() for d in result.halts],
    }
