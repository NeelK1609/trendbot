from datetime import date, timedelta
from decimal import Decimal

import pandas as pd
import pytest

from trendbot.engine import Portfolio
from trendbot.kraken import Fill, KrakenError, PairRules
from trendbot.live import GO_LIVE_PHRASE, go_live, open_account, run_live_daily, userref
from trendbot.store import LocalStore

TODAY = date(2026, 6, 1)


class FakeKraken:
    """An in-memory exchange: fills market orders at the ask/bid, tracks balances and tagged orders."""

    def __init__(self, cad=1_000.0, prices=None, fee_rate=0.004):
        self.balance = {"CAD": cad, "BTC": 0.0, "ETH": 0.0}
        self.prices = prices or {"BTC": 200.0, "ETH": 50.0}
        self.fee_rate = fee_rate
        self.orders: dict[int, Fill] = {}
        self.placed, self.validated = [], []
        self.fail_on: set[str] = set()

    def pair_rules(self, symbol):
        return PairRules(symbol, 8, Decimal("0.0001"), Decimal("1"))

    def ticker(self, symbol):
        p = self.prices[symbol]
        return p * 0.999, p * 1.001

    def balances(self):
        return dict(self.balance)

    def find_order(self, ref):
        return self.orders.get(ref)

    def market_order(self, symbol, side, volume, ref, validate):
        if f"{side} {symbol}" in self.fail_on:
            raise KrakenError("EOrder:Insufficient funds")
        if validate:
            self.validated.append((symbol, side, volume))
            return None
        bid, ask = self.ticker(symbol)
        price = ask if side == "buy" else bid
        cost = float(volume) * price
        fee = cost * self.fee_rate
        if side == "buy":
            self.balance["CAD"] -= cost + fee
            self.balance[symbol] += float(volume)
        else:
            self.balance["CAD"] += cost - fee
            self.balance[symbol] -= float(volume)
        fill = Fill(f"T{len(self.placed)}", float(volume), price, cost, fee)
        self.orders[ref] = fill
        self.placed.append((symbol, side, volume))
        return fill


def candles(end: date, btc: list[float], eth: list[float]) -> dict[str, pd.DataFrame]:
    def frame(closes):
        days = pd.date_range(end=pd.Timestamp(end), periods=len(closes), freq="D", name="date")
        c = pd.Series(closes, index=days, dtype=float)
        return pd.DataFrame({"open": c, "high": c * 1.01, "low": c * 0.99, "close": c, "volume": 1.0})
    return {"BTC": frame(btc), "ETH": frame(eth)}


UP = [100.0] * 100 + [100 + i for i in range(1, 101)]  # ends at 200, strong uptrend
FLAT = [50.0] * 200


@pytest.fixture
def store(tmp_path):
    return LocalStore(tmp_path / "live.json")


def opened(store, kraken, mode="shadow"):
    open_account(store, kraken, capital=500, start=TODAY - timedelta(days=1))
    if mode == "live":
        go_live(store, GO_LIVE_PHRASE)


def test_userref_is_unique_per_day_coin_and_action_and_fits_int32():
    refs = {userref(date(2026, 10, 24), s, k) for s in ("BTC", "ETH") for k in ("buy", "sell", "trim")}
    assert len(refs) == 6
    assert max(refs) < 2**31


def test_cannot_open_with_more_capital_than_kraken_holds(store):
    with pytest.raises(RuntimeError, match="less than"):
        open_account(store, FakeKraken(cad=100), capital=500, start=TODAY)


def test_going_live_needs_the_exact_phrase(store):
    kraken = FakeKraken()
    opened(store, kraken)
    with pytest.raises(RuntimeError):
        go_live(store, "yes")
    assert store.load()[0]["mode"] == "shadow"


def test_shadow_validates_orders_but_never_executes(store):
    kraken = FakeKraken()
    opened(store, kraken)
    records = run_live_daily(store, kraken, lambda: candles(TODAY - timedelta(days=1), UP, FLAT), TODAY, allow_real_orders=True)

    assert [a["kind"] for a in records[0]["actions"]] == ["buy"]
    assert kraken.validated and not kraken.placed
    assert kraken.balance == {"CAD": 1_000.0, "BTC": 0.0, "ETH": 0.0}
    state, _ = store.load()
    assert state["orders"][0]["txid"].startswith("shadow-")


def test_live_mode_without_the_app_setting_only_validates(store):
    kraken = FakeKraken()
    opened(store, kraken, mode="live")
    run_live_daily(store, kraken, lambda: candles(TODAY - timedelta(days=1), UP, FLAT), TODAY)  # setting off
    assert kraken.validated and not kraken.placed
    assert store.load()[0]["orders"][0]["mode"] == "shadow"


def test_live_buy_records_the_actual_fill(store):
    kraken = FakeKraken()
    opened(store, kraken, mode="live")
    REAL = {"allow_real_orders": True}
    run_live_daily(store, kraken, lambda: candles(TODAY - timedelta(days=1), UP, FLAT), TODAY, **REAL)

    state, _ = store.load()
    pf = Portfolio.from_dict(state["portfolio"])
    trade = pf.positions["BTC"].trade
    fill = kraken.orders[userref(TODAY - timedelta(days=1), "BTC", "buy")]
    assert trade.quantity == fill.volume
    assert trade.entry_price == pytest.approx(fill.price)
    assert pf.cash == pytest.approx(500 - fill.cost - fill.fee)
    assert kraken.balance["BTC"] == pytest.approx(trade.quantity)


def test_a_rerun_reuses_the_tagged_order_instead_of_buying_twice(store):
    kraken = FakeKraken()
    opened(store, kraken, mode="live")
    REAL = {"allow_real_orders": True}
    state, version = store.load()
    run_live_daily(store, kraken, lambda: candles(TODAY - timedelta(days=1), UP, FLAT), TODAY, **REAL)
    # Simulate a crash after the order but before saving: restore the old state and run again.
    store.save(state, store.load()[1])
    run_live_daily(store, kraken, lambda: candles(TODAY - timedelta(days=1), UP, FLAT), TODAY, **REAL)
    assert len(kraken.placed) == 1


def test_failed_order_is_rolled_back_and_pauses_trading(store):
    kraken = FakeKraken()
    kraken.fail_on.add("buy BTC")
    opened(store, kraken, mode="live")
    REAL = {"allow_real_orders": True}
    records = run_live_daily(store, kraken, lambda: candles(TODAY - timedelta(days=1), UP, FLAT), TODAY, **REAL)

    state, _ = store.load()
    pf = Portfolio.from_dict(state["portfolio"])
    assert pf.positions == {}
    assert pf.cash == pytest.approx(500)
    assert pf.paused_until == date.max
    assert records[0]["actions"][-1]["kind"] == "failed"
    assert state["events"][-1]["kind"] == "paused"


def test_mismatched_balances_pause_before_trading(store):
    kraken = FakeKraken()
    opened(store, kraken, mode="live")
    REAL = {"allow_real_orders": True}
    run_live_daily(store, kraken, lambda: candles(TODAY - timedelta(days=1), UP, FLAT), TODAY, **REAL)
    kraken.balance["BTC"] = 0.0  # someone sold the bot's BTC by hand

    tomorrow = TODAY + timedelta(days=1)
    records = run_live_daily(store, kraken, lambda: candles(tomorrow - timedelta(days=1), UP + [201], FLAT + [50]), tomorrow, **REAL)
    assert records == []
    state, _ = store.load()
    assert "Balances don't match" in state["events"][-1]["detail"]
    assert Portfolio.from_dict(state["portfolio"]).paused_until == date.max


def test_live_sell_records_actual_proceeds(store):
    kraken = FakeKraken()
    opened(store, kraken, mode="live")
    REAL = {"allow_real_orders": True}
    run_live_daily(store, kraken, lambda: candles(TODAY - timedelta(days=1), UP, FLAT), TODAY, **REAL)

    crash_day = TODAY + timedelta(days=1)
    kraken.prices["BTC"] = 120.0
    records = run_live_daily(store, kraken, lambda: candles(crash_day - timedelta(days=1), UP + [120], FLAT + [50]), crash_day, **REAL)
    assert [a["kind"] for a in records[0]["actions"]] == ["sell"]

    state, _ = store.load()
    pf = Portfolio.from_dict(state["portfolio"])
    assert pf.positions == {}
    assert kraken.balance["BTC"] == pytest.approx(0, abs=1e-9)
    assert pf.cash == pytest.approx(kraken.balance["CAD"] - 500, rel=1e-9)  # bot's book = its share of the CAD
