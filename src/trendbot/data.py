"""Daily BTC and ETH candles in Canadian dollars.

Kraken's public API only returns the most recent ~720 daily candles, so long backtests use Coinbase's
USD candles converted to CAD with the Bank of Canada's daily USD/CAD rate. Live and paper trading use
Kraken's real CAD candles. All candles are UTC days, and the current (unfinished) day is always dropped.
"""

from __future__ import annotations

import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pandas as pd

SYMBOLS = ("BTC", "ETH")
PRICE_COLUMNS = ["open", "high", "low", "close"]
COLUMNS = [*PRICE_COLUMNS, "volume"]

COINBASE_URL = "https://api.exchange.coinbase.com/products/{product}/candles"
BOC_URL = "https://www.bankofcanada.ca/valet/observations/FXUSDCAD/json"
KRAKEN_URL = "https://api.kraken.com/0/public/OHLC"
KRAKEN_PAIRS = {"BTC": "XBTCAD", "ETH": "ETHCAD"}

USER_AGENT = "trendbot/0.1 (research)"


def today_utc() -> date:
    return datetime.now(UTC).date()


def _client() -> httpx.Client:
    return httpx.Client(timeout=30, headers={"User-Agent": USER_AGENT})


def _frame(rows: list[tuple], index: list[date]) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=COLUMNS, index=pd.DatetimeIndex(index, name="date"), dtype=float)
    return df[~df.index.duplicated(keep="last")].sort_index()


def fetch_coinbase_usd(symbol: str, start: date, end: date, client: httpx.Client | None = None) -> pd.DataFrame:
    """Completed daily USD candles from Coinbase Exchange, [start, end]."""
    client = client or _client()
    end = min(end, today_utc() - timedelta(days=1))
    rows, index = [], []
    cursor = start
    while cursor <= end:
        chunk_end = min(cursor + timedelta(days=299), end)  # API max 300 candles per request
        response = client.get(
            COINBASE_URL.format(product=f"{symbol}-USD"),
            params={
                "granularity": 86400,
                "start": f"{cursor.isoformat()}T00:00:00Z",
                "end": f"{chunk_end.isoformat()}T23:59:59Z",
            },
        )
        response.raise_for_status()
        for t, low, high, open_, close, volume in response.json():
            index.append(datetime.fromtimestamp(t, UTC).date())
            rows.append((open_, high, low, close, volume))
        cursor = chunk_end + timedelta(days=1)
        time.sleep(0.35)  # stay well under the public rate limit
    return _frame(rows, index).loc[pd.Timestamp(start) : pd.Timestamp(end)]


def fetch_usdcad(start: date, end: date, client: httpx.Client | None = None) -> pd.Series:
    """Bank of Canada daily USD/CAD, forward-filled over weekends and holidays to every calendar day."""
    client = client or _client()
    response = client.get(BOC_URL, params={"start_date": start.isoformat(), "end_date": end.isoformat()})
    response.raise_for_status()
    obs = response.json()["observations"]
    rates = pd.Series(
        [float(o["FXUSDCAD"]["v"]) for o in obs],
        index=pd.DatetimeIndex([o["d"] for o in obs], name="date"),
        name="usdcad",
    )
    days = pd.date_range(start, end, freq="D", name="date")
    return rates.reindex(days).ffill().bfill()


def fetch_kraken_cad(symbol: str, client: httpx.Client | None = None) -> pd.DataFrame:
    """Kraken's completed daily CAD candles (about the last 720 days)."""
    client = client or _client()
    response = client.get(KRAKEN_URL, params={"pair": KRAKEN_PAIRS[symbol], "interval": 1440})
    response.raise_for_status()
    payload = response.json()
    if payload["error"]:
        raise RuntimeError(f"Kraken error for {symbol}: {payload['error']}")
    key = next(k for k in payload["result"] if k != "last")
    rows, index = [], []
    for t, open_, high, low, close, _vwap, volume, _count in payload["result"][key]:
        day = datetime.fromtimestamp(t, UTC).date()
        if day >= today_utc():
            continue  # today's candle is still forming
        index.append(day)
        rows.append((open_, high, low, close, volume))
    return _frame(rows, index)


def to_cad(usd: pd.DataFrame, usdcad: pd.Series) -> pd.DataFrame:
    rate = usdcad.reindex(usd.index)
    cad = usd.copy()
    cad[PRICE_COLUMNS] = usd[PRICE_COLUMNS].mul(rate, axis=0)
    return cad.dropna(subset=PRICE_COLUMNS)


def complete_days(df: pd.DataFrame) -> pd.DataFrame:
    """Every calendar day from first to last; days with no data become NaN so the strategy can skip them."""
    days = pd.date_range(df.index.min(), df.index.max(), freq="D", name="date")
    return df.reindex(days)


def load_history(symbol: str, start: date, cache_dir: Path, refresh: bool = False) -> pd.DataFrame:
    """Long CAD history for backtests (Coinbase USD x Bank of Canada USD/CAD), cached as CSV."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"{symbol}-CAD-synthetic.csv"
    if path.exists() and not refresh:
        return pd.read_csv(path, index_col="date", parse_dates=["date"])

    end = today_utc() - timedelta(days=1)
    with _client() as client:
        usd = fetch_coinbase_usd(symbol, start, end, client)
        usdcad = fetch_usdcad(start, end, client)
    cad = complete_days(to_cad(usd, usdcad))
    cad.to_csv(path)
    return cad


def load_kraken(symbol: str, cache_dir: Path, refresh: bool = False) -> pd.DataFrame:
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"{symbol}-CAD-kraken.csv"
    if path.exists() and not refresh:
        return pd.read_csv(path, index_col="date", parse_dates=["date"])
    with _client() as client:
        df = complete_days(fetch_kraken_cad(symbol, client))
    df.to_csv(path)
    return df


def data_gaps(df: pd.DataFrame) -> list[date]:
    return [d.date() for d in df.index[df["close"].isna()]]
