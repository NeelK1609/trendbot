import math
from datetime import date

import numpy as np
import pandas as pd
import pytest

from trendbot.backtest import Costs, HaltPolicy, run_backtest
from trendbot.strategy import (
    StrategyConfig,
    add_indicators,
    drawdown_breached,
    entry_signal,
    exit_reason,
    has_valid_data,
    position_value,
    trailing_stop,
)

CFG = StrategyConfig()
NO_COSTS = Costs(fee_rate=0.0, slippage=0.0)


def candles(closes: list[float], start: str = "2020-01-01", spread: float = 0.01) -> pd.DataFrame:
    close = pd.Series(closes, index=pd.date_range(start, periods=len(closes), freq="D", name="date"), dtype=float)
    return pd.DataFrame({
        "open": close.shift(1).fillna(close),
        "high": close * (1 + spread),
        "low": close * (1 - spread),
        "close": close,
        "volume": 1.0,
    })


def row(**values) -> pd.Series:
    base = dict(open=100, high=101, low=99, close=100, sma_fast=95, sma_slow=90, atr=4)
    return pd.Series({**base, **values}, dtype=float)


def test_indicators_match_definitions():
    df = add_indicators(candles(list(range(1, 101))), CFG)
    assert df["sma_slow"].iloc[49] == pytest.approx(np.mean(range(1, 51)))
    assert math.isnan(df["sma_slow"].iloc[48])
    assert df["sma_fast"].iloc[-1] == pytest.approx(np.mean(range(81, 101)))
    assert df["atr"].iloc[-1] > 0


def test_entry_needs_price_above_slow_and_fast_above_slow():
    assert entry_signal(row(close=100, sma_fast=95, sma_slow=90))
    assert not entry_signal(row(close=85, sma_fast=95, sma_slow=90))
    assert not entry_signal(row(close=100, sma_fast=88, sma_slow=90))


def test_exit_on_trend_break_before_trailing_stop():
    assert exit_reason(row(close=89, sma_slow=90), highest_close=150, cfg=CFG) == "trend_break"


def test_exit_on_trailing_stop():
    # highest 120, ATR 4 -> stop at 110
    assert trailing_stop(120, 4, CFG) == 110
    assert exit_reason(row(close=109, sma_slow=90, atr=4), highest_close=120, cfg=CFG) == "trailing_stop"
    assert exit_reason(row(close=111, sma_slow=90, atr=4), highest_close=120, cfg=CFG) is None


def test_sizing_matches_the_rules_example():
    # Stop 10% away (2.5 x ATR 4 on a close of 100) -> position is 10% of the account.
    assert position_value(10_000, close=100, atr=4, cfg=CFG) == pytest.approx(1_000)


def test_sizing_is_capped_at_half_the_account():
    assert position_value(10_000, close=100, atr=0.4, cfg=CFG) == pytest.approx(5_000)


def test_broken_data_blocks_trading():
    assert has_valid_data(row())
    assert not has_valid_data(row(close=float("nan")))
    assert not has_valid_data(row(atr=float("nan")))


def test_drawdown_rule():
    assert drawdown_breached(8_000, 10_000, CFG)
    assert not drawdown_breached(8_100, 10_000, CFG)


def test_backtest_buys_an_uptrend_and_sells_when_it_breaks():
    closes = [100.0] * 60 + [100 + i for i in range(1, 61)] + [160 - 3 * i for i in range(1, 41)]
    result = run_backtest({"BTC": candles(closes)}, CFG, NO_COSTS)

    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.reason in {"trend_break", "trailing_stop"}
    assert trade.entry_date == date(2020, 3, 1)  # first day of the uptrend (day 61)
    assert trade.exit_price < 160


def test_costs_reduce_profit():
    closes = [100.0] * 60 + [100 + i for i in range(1, 61)] + [160 - 3 * i for i in range(1, 41)]
    free = run_backtest({"BTC": candles(closes)}, CFG, NO_COSTS).trades[0]
    paid = run_backtest({"BTC": candles(closes)}, CFG, Costs()).trades[0]
    assert paid.pnl < free.pnl
    assert paid.entry_fee > 0 and paid.exit_fee > 0


def test_no_trading_on_missing_day():
    closes = [100.0] * 60 + [100 + i for i in range(1, 61)]
    df = candles(closes)
    signal_day = add_indicators(df, CFG).apply(entry_signal, axis=1).idxmax()
    df.loc[signal_day, ["open", "high", "low", "close"]] = np.nan
    result = run_backtest({"BTC": df}, CFG, NO_COSTS)
    assert result.open_positions["BTC"].entry_date > signal_day.date()


def test_rearm_waits_for_the_signal_to_reset_after_a_stop_out():
    cfg = StrategyConfig(rearm_after_exit=True)
    closes = [100.0] * 60 + [100 + 2 * i for i in range(1, 41)] + [175, 168, 170, 172, 174, 176]
    result = run_backtest({"BTC": candles(closes, spread=0.002)}, cfg, NO_COSTS)
    stops = [t for t in result.trades if t.reason == "trailing_stop"]
    assert stops, "expected a trailing-stop exit"
    assert "BTC" not in result.open_positions  # trend never reset, so no immediate re-entry


def test_drawdown_halt_stops_all_further_trading():
    cfg = StrategyConfig(risk_per_trade=0.5, max_coin_fraction=1.0, atr_mult=20)  # oversized on purpose
    # Uptrend, a one-day 40% crash, then a fresh uptrend the bot must ignore while halted.
    closes = [100.0] * 60 + [100 + i for i in range(1, 41)] + [84.0] + [84 + 2 * i for i in range(1, 80)]
    result = run_backtest({"BTC": candles(closes)}, cfg, NO_COSTS, halt=HaltPolicy(mode="stop"))
    assert result.halts == [date(2020, 4, 10)]
    assert all(t.entry_date < date(2020, 4, 10) for t in result.trades)
    assert not result.open_positions


def test_review_policy_resumes_after_the_pause():
    cfg = StrategyConfig(risk_per_trade=0.5, max_coin_fraction=1.0, atr_mult=20)
    closes = [100.0] * 60 + [100 + i for i in range(1, 41)] + [84.0] + [84 + 2 * i for i in range(1, 80)]
    result = run_backtest({"BTC": candles(closes)}, cfg, NO_COSTS, halt=HaltPolicy(mode="review", review_days=30))
    assert result.halts == [date(2020, 4, 10)]
    assert "BTC" in result.open_positions
    assert result.open_positions["BTC"].entry_date >= date(2020, 5, 10)


def test_winner_is_trimmed_back_to_the_cap():
    cfg = StrategyConfig(max_coin_fraction=0.5, enforce_cap_daily=True)
    closes = [100.0] * 60 + [100 * 1.03**i for i in range(1, 120)]
    result = run_backtest({"BTC": candles(closes, spread=0.002)}, cfg, NO_COSTS)
    trims = [t for t in result.trades if t.reason == "trim_to_cap"]
    assert trims
    final = result.open_positions["BTC"]
    last_close = closes[-1]
    assert final.quantity * last_close <= 0.5 * result.equity.iloc[-1] * 1.04  # within one day's move of the cap
