"""The trading rules from "Starter Trading Rules: BTC & ETH Trend Following", as pure functions.

Evaluated once per day on the completed daily candle. The backtester, the paper trader and the live
trader all call these same functions, so what was tested is exactly what trades.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class StrategyConfig:
    fast: int = 20
    slow: int = 50
    atr_period: int = 14
    atr_mult: float = 2.5
    risk_per_trade: float = 0.01  # lose at most 1% of the account if the stop is hit
    max_coin_fraction: float = 0.50  # never more than half the account in one coin
    max_drawdown: float = 0.20  # stop trading after a 20% fall from the account's peak
    # The rules don't say when to buy again after an exit. True: wait until the entry
    # condition has been false for a day (avoids re-buying the day after a stop-out).
    rearm_after_exit: bool = True
    # "No more than 50% of the account in any one coin": True enforces it every day by trimming
    # winners back to the cap; False only checks it when buying.
    enforce_cap_daily: bool = True


def add_indicators(candles: pd.DataFrame, cfg: StrategyConfig) -> pd.DataFrame:
    """Adds sma_fast, sma_slow and atr (Wilder's 14-day average true range) columns."""
    df = candles.copy()
    close = df["close"]
    df["sma_fast"] = close.rolling(cfg.fast, min_periods=cfg.fast).mean()
    df["sma_slow"] = close.rolling(cfg.slow, min_periods=cfg.slow).mean()
    prev_close = close.shift(1)
    true_range = pd.concat(
        [df["high"] - df["low"], (df["high"] - prev_close).abs(), (df["low"] - prev_close).abs()], axis=1
    ).max(axis=1, skipna=False)
    df["atr"] = true_range.ewm(alpha=1 / cfg.atr_period, adjust=False, min_periods=cfg.atr_period).mean()
    return df


def has_valid_data(row: pd.Series) -> bool:
    """The safety rule "no trading on broken data": every price and indicator must be present and positive."""
    values = [row.get(c) for c in ("open", "high", "low", "close", "sma_fast", "sma_slow", "atr")]
    return all(v is not None and math.isfinite(v) and v > 0 for v in values)


def entry_signal(row: pd.Series) -> bool:
    """Buy when price is above the slow average and the fast average is above the slow one."""
    return row["close"] > row["sma_slow"] and row["sma_fast"] > row["sma_slow"]


def trailing_stop(highest_close: float, atr: float, cfg: StrategyConfig) -> float:
    """2.5 x ATR below the highest close since entry. Per the rules it widens when ATR rises."""
    return highest_close - cfg.atr_mult * atr


def exit_reason(row: pd.Series, highest_close: float, cfg: StrategyConfig) -> str | None:
    if row["close"] < row["sma_slow"]:
        return "trend_break"
    if row["close"] < trailing_stop(highest_close, row["atr"], cfg):
        return "trailing_stop"
    return None


def position_value(equity: float, close: float, atr: float, cfg: StrategyConfig) -> float:
    """Size so that hitting the initial stop loses `risk_per_trade` of equity, capped per coin.

    Example from the rules: stop 10% away -> position is 10% of the account (1% / 10%).
    """
    stop_distance = cfg.atr_mult * atr / close
    if stop_distance <= 0:
        return 0.0
    return min(equity * cfg.risk_per_trade / stop_distance, equity * cfg.max_coin_fraction)


def drawdown_breached(equity: float, peak_equity: float, cfg: StrategyConfig) -> bool:
    return peak_equity > 0 and equity <= peak_equity * (1 - cfg.max_drawdown)


def trim_value(position_value_now: float, equity: float, cfg: StrategyConfig) -> float:
    """How much of a position to sell so it is back under the per-coin cap (0 if within it)."""
    if not cfg.enforce_cap_daily:
        return 0.0
    return max(0.0, position_value_now - equity * cfg.max_coin_fraction)
