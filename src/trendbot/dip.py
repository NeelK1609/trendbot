"""Research: "buy a 0.5% dip, sell at +1%" on hourly candles, to see whether frequent small trades survive fees.

Rules tested (no stop-loss, as proposed):
- While flat, track the highest price since the last sale. A resting buy order sits 0.5% below that high.
- Once bought, a resting sell order sits 1% above the buy price, and stays there until it fills.
- Each coin trades half the account. Orders fill at their limit price when an hourly low/high reaches it;
  if both levels are touched in the same hour, the sale is assumed to happen in a later hour (conservative).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pandas as pd

from .data import COINBASE_URL, USER_AGENT, fetch_usdcad, to_cad


def fetch_hourly_cad(symbol: str, start: date, cache_dir: Path) -> pd.DataFrame:
    path = cache_dir / f"{symbol}-CAD-hourly.csv"
    if path.exists():
        return pd.read_csv(path, index_col="date", parse_dates=["date"])
    end = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    rows, index = [], []
    with httpx.Client(timeout=30, headers={"User-Agent": USER_AGENT}) as client:
        cursor = datetime(start.year, start.month, start.day, tzinfo=UTC)
        while cursor < end:
            chunk_end = min(cursor + timedelta(hours=299), end)
            r = client.get(COINBASE_URL.format(product=f"{symbol}-USD"), params={
                "granularity": 3600, "start": cursor.isoformat(), "end": chunk_end.isoformat()})
            r.raise_for_status()
            for t, low, high, open_, close, volume in r.json():
                index.append(datetime.fromtimestamp(t, UTC).replace(tzinfo=None))
                rows.append((open_, high, low, close, volume))
            cursor = chunk_end + timedelta(hours=1)
            time.sleep(0.34)
        usdcad = fetch_usdcad(start, end.date(), client)
    usd = pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"],
                       index=pd.DatetimeIndex(index, name="date")).sort_index()
    usd = usd[~usd.index.duplicated()]
    rate = usdcad.reindex(usd.index.normalize()).to_numpy()
    cad = usd.copy()
    for c in ("open", "high", "low", "close"):
        cad[c] = usd[c] * rate
    cad.to_csv(path)
    return cad


@dataclass(frozen=True)
class DipResult:
    label: str
    total_return: float
    cagr: float
    max_drawdown: float
    round_trips: int
    open_at_end: bool
    longest_stuck_days: float
    worst_open_loss: float  # worst mark-to-market loss of a held position, as a fraction


def run_dip(candles: pd.DataFrame, dip: float = 0.005, target: float = 0.01, fee: float = 0.0025,
            cash: float = 5_000.0) -> tuple[pd.Series, int, bool, float, float]:
    high, low, close = candles["high"].to_numpy(), candles["low"].to_numpy(), candles["close"].to_numpy()
    times = candles.index
    peak = high[0]
    qty = 0.0
    entry = 0.0
    entry_time = None
    trips = 0
    longest = 0.0
    worst = 0.0
    equity = []
    for i in range(len(close)):
        if qty == 0:
            buy_at = peak * (1 - dip)
            if low[i] <= buy_at:
                qty = cash * (1 - fee) / buy_at
                cash = 0.0
                entry, entry_time = buy_at, times[i]
            else:
                peak = max(peak, high[i])
        else:
            sell_at = entry * (1 + target)
            if high[i] >= sell_at and times[i] != entry_time:
                cash = qty * sell_at * (1 - fee)
                qty = 0.0
                trips += 1
                longest = max(longest, (times[i] - entry_time).total_seconds() / 86400)
                peak = high[i]
            else:
                worst = min(worst, low[i] / entry - 1)
        equity.append(cash + qty * close[i])
    if qty:
        longest = max(longest, (times[-1] - entry_time).total_seconds() / 86400)
    return pd.Series(equity, index=times), trips, qty > 0, longest, worst


def main() -> None:
    start = date(2019, 1, 1)
    cache = Path("data")
    data = {s: fetch_hourly_cad(s, start, cache) for s in ("BTC", "ETH")}
    years = (data["BTC"].index[-1] - data["BTC"].index[0]).days / 365
    print(f"Hourly CAD candles {data['BTC'].index[0]:%Y-%m-%d} → {data['BTC'].index[-1]:%Y-%m-%d} ({len(data['BTC']):,} hours each)\n")
    print(f"{'Fees per side':38}{'Total':>11}{'Per year':>10}{'Worst fall':>12}{'Round trips':>13}{'Longest stuck':>15}{'Worst open loss':>17}")
    for label, fee in [("None (impossible, for reference)", 0.0), ("0.25% Kraken maker (limit orders)", 0.0025),
                       ("0.40% Kraken taker (market orders)", 0.0040)]:
        curves, trips, longest, worst, stuck_end = [], 0, 0.0, 0.0, []
        for s, df in data.items():
            eq, t, open_end, stuck, w = run_dip(df, fee=fee)
            curves.append(eq)
            trips += t
            longest = max(longest, stuck)
            worst = min(worst, w)
            if open_end:
                stuck_end.append(s)
        total = curves[0].add(curves[1], fill_value=0).ffill()
        ret = total.iloc[-1] / 10_000 - 1
        cagr = (1 + ret) ** (1 / years) - 1
        dd = (total / total.cummax() - 1).min()
        print(f"{label:38}{ret:>+11.1%}{cagr:>+10.1%}{dd:>12.1%}{trips:>13,}{longest:>12.0f} days{worst:>17.1%}"
              + (f"   (still holding {', '.join(stuck_end)})" if stuck_end else ""))
    hold = sum(5_000 / df["close"].iloc[0] * df["close"].iloc[-1] for df in data.values())
    print(f"\nBuy and hold 50/50 over the same period: {hold / 10_000 - 1:+.1%}")


if __name__ == "__main__":
    main()
