from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from trendbot.engine import Portfolio
from trendbot.paper import open_demo, resume, run_daily
from trendbot.store import ConflictError, LocalStore

TODAY = date(2026, 6, 1)


def kraken_like(closes: list[float], end: date) -> pd.DataFrame:
    days = pd.date_range(end=pd.Timestamp(end), periods=len(closes), freq="D", name="date")
    close = pd.Series(closes, index=days, dtype=float)
    return pd.DataFrame({"open": close, "high": close * 1.01, "low": close * 0.99, "close": close, "volume": 1.0})


def uptrend(end: date) -> dict[str, pd.DataFrame]:
    closes = [100.0] * 100 + [100 + i for i in range(1, 101)]
    return {"BTC": kraken_like(closes, end), "ETH": kraken_like([50.0] * 200, end)}


@pytest.fixture
def store(tmp_path):
    return LocalStore(tmp_path / "account.json")


def test_first_run_opens_the_account_and_trades_yesterday(store):
    records = run_daily(store, lambda: uptrend(TODAY - timedelta(days=1)), TODAY)

    assert [r["day"] for r in records] == ["2026-05-31"]
    assert records[0]["actions"][0]["kind"] == "buy"
    assert records[0]["actions"][0]["symbol"] == "BTC"
    state, _ = store.load()
    assert state["last_day"] == "2026-05-31"
    assert "BTC" in state["portfolio"]["positions"]


def test_running_twice_on_the_same_day_does_nothing(store):
    run_daily(store, lambda: uptrend(TODAY - timedelta(days=1)), TODAY)
    assert run_daily(store, lambda: uptrend(TODAY - timedelta(days=1)), TODAY) == []


def test_missed_days_are_caught_up_in_order(store):
    run_daily(store, lambda: uptrend(TODAY - timedelta(days=1)), TODAY)
    later = TODAY + timedelta(days=3)
    records = run_daily(store, lambda: uptrend(later - timedelta(days=1)), later)
    assert [r["day"] for r in records] == ["2026-06-01", "2026-06-02", "2026-06-03"]


def test_stale_data_blocks_trading(store):
    # Kraken's latest candle is two days old: yesterday has no price, so nothing trades.
    records = run_daily(store, lambda: uptrend(TODAY - timedelta(days=2)), TODAY)
    assert records[0]["actions"] == []
    assert records[0]["signals"]["BTC"]["valid"] is False


def test_concurrent_runs_cannot_both_save(store):
    run_daily(store, lambda: uptrend(TODAY - timedelta(days=1)), TODAY)
    state, version = store.load()
    store.save(state, version)  # someone else saves in between
    with pytest.raises(ConflictError):
        store.save(state, version)


def test_halt_waits_for_a_person_to_resume(store):
    end = TODAY - timedelta(days=1)
    run_daily(store, lambda: uptrend(end), TODAY)
    state, version = store.load()
    # Force a 30% drawdown on the next day by marking the account's peak far above its value.
    state["portfolio"]["peak"] = 20_000
    store.save(state, version)

    next_day = TODAY + timedelta(days=1)
    records = run_daily(store, lambda: uptrend(next_day - timedelta(days=1)), next_day)
    assert any(a["kind"] == "halt" for a in records[0]["actions"])
    assert Portfolio.from_dict(store.load()[0]["portfolio"]).paused_until == date.max

    # Still paused on later days until someone resumes.
    later = next_day + timedelta(days=5)
    records = run_daily(store, lambda: uptrend(later - timedelta(days=1)), later)
    assert all(r["paused"] for r in records)
    assert not any(a["kind"] == "buy" for r in records for a in r["actions"])

    resume(store, "Reviewed: test")
    state, _ = store.load()
    assert state["portfolio"]["paused_until"] is None
    assert state["events"][-1]["kind"] == "resumed"


def test_portfolio_round_trips_through_json(store):
    run_daily(store, lambda: uptrend(TODAY - timedelta(days=1)), TODAY)
    state, _ = store.load()
    pf = Portfolio.from_dict(state["portfolio"])
    assert pf.to_dict() == state["portfolio"]
    assert np.isclose(pf.equity(), state["days"][-1]["equity"], atol=0.01)


def test_demo_buy_is_then_managed_by_the_rules(store):
    # Downtrend: the rules would never buy, so the forced demo position is sold on the next run.
    down = [200.0 - i * 0.5 for i in range(200)]
    end = TODAY
    open_demo(store, "BTC", 10.0, ask_price=down[-1], today=TODAY)
    state, _ = store.load()
    pf = Portfolio.from_dict(state["portfolio"])
    assert state["mode"] == "demo"
    assert pf.positions["BTC"].trade.quantity * pf.positions["BTC"].trade.entry_price + pf.positions["BTC"].trade.entry_fee <= 10.0
    assert pf.cash >= 0

    tomorrow = TODAY + timedelta(days=1)
    records = run_daily(store, lambda: {"BTC": kraken_like(down, end), "ETH": kraken_like([50.0] * 200, end)}, tomorrow)
    assert [a["kind"] for a in records[0]["actions"]] == ["sell"]
    assert records[0]["actions"][0]["reason"] == "trend_break"
