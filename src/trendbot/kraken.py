"""Minimal Kraken REST client: balances, pair rules, prices, and market orders tagged for idempotency."""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
import urllib.parse
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

import httpx

API = "https://api.kraken.com"
PAIRS = {"BTC": "XBTCAD", "ETH": "ETHCAD"}
BALANCE_KEYS = {"CAD": ("ZCAD", "CAD"), "BTC": ("XXBT", "XBT"), "ETH": ("XETH", "ETH")}


class KrakenError(RuntimeError):
    pass


@dataclass(frozen=True)
class PairRules:
    pair: str
    lot_decimals: int
    order_min: Decimal
    cost_min: Decimal

    def round_volume(self, volume: float) -> Decimal:
        step = Decimal(1).scaleb(-self.lot_decimals)
        return Decimal(str(volume)).quantize(step, rounding=ROUND_DOWN)


@dataclass(frozen=True)
class Fill:
    txid: str
    volume: float
    price: float  # average fill price
    cost: float  # volume x price, in CAD
    fee: float  # in CAD


def sign(path: str, data: dict, secret: str) -> str:
    """Kraken's API-Sign: HMAC-SHA512 of path + SHA256(nonce + POST body), keyed by the base64-decoded secret."""
    body = urllib.parse.urlencode(data)
    digest = hashlib.sha256((str(data["nonce"]) + body).encode()).digest()
    mac = hmac.new(base64.b64decode(secret), path.encode() + digest, hashlib.sha512)
    return base64.b64encode(mac.digest()).decode()


class KrakenClient:
    def __init__(self, api_key: str | None = None, api_secret: str | None = None, http: httpx.Client | None = None):
        self._key, self._secret = api_key, api_secret
        self._http = http or httpx.Client(base_url=API, timeout=30, headers={"User-Agent": "trendbot/0.1"})
        self._last_nonce = 0

    # Public
    def pair_rules(self, symbol: str) -> PairRules:
        result = self._public("AssetPairs", {"pair": PAIRS[symbol]})
        info = next(iter(result.values()))
        return PairRules(PAIRS[symbol], int(info["lot_decimals"]), Decimal(info["ordermin"]), Decimal(info.get("costmin", "0")))

    def ticker(self, symbol: str) -> tuple[float, float]:
        """(best bid, best ask) in CAD."""
        result = self._public("Ticker", {"pair": PAIRS[symbol]})
        info = next(iter(result.values()))
        return float(info["b"][0]), float(info["a"][0])

    # Private
    def balances(self) -> dict[str, float]:
        raw = self._private("Balance", {})
        return {asset: sum(float(raw.get(k, 0)) for k in keys) for asset, keys in BALANCE_KEYS.items()}

    def find_order(self, userref: int) -> Fill | None:
        """A previously placed bot order with this tag, if any (open orders count as not filled yet)."""
        for method in ("OpenOrders", "ClosedOrders"):
            orders = self._private(method, {"userref": userref}).get("open" if method == "OpenOrders" else "closed", {})
            for txid, order in orders.items():
                if method == "OpenOrders":
                    raise KrakenError(f"Order {txid} (tag {userref}) is still open on Kraken")
                if order["status"] == "closed":
                    return self._fill(txid, order)
                if order["status"] in ("canceled", "expired") and float(order["vol_exec"]) == 0:
                    continue
                raise KrakenError(f"Order {txid} (tag {userref}) ended as {order['status']} with partial fill")
        return None

    def market_order(self, symbol: str, side: str, volume: Decimal, userref: int, validate: bool) -> Fill | None:
        """Places (or, with validate, only checks) a market order. Fees are charged in CAD so coin amounts stay exact."""
        params = {
            "pair": PAIRS[symbol], "type": side, "ordertype": "market", "volume": str(volume),
            "oflags": "fciq", "userref": userref,
        }
        if validate:
            params["validate"] = "true"
            self._private("AddOrder", params)
            return None
        txid = self._private("AddOrder", params)["txid"][0]
        for _ in range(20):  # market orders fill almost immediately
            order = self._private("QueryOrders", {"txid": txid})[txid]
            if order["status"] == "closed":
                return self._fill(txid, order)
            if order["status"] in ("canceled", "expired"):
                raise KrakenError(f"Order {txid} was {order['status']}: {order.get('reason')}")
            time.sleep(1.5)
        raise KrakenError(f"Order {txid} did not fill within 30 seconds")

    @staticmethod
    def _fill(txid: str, order: dict) -> Fill:
        volume = float(order["vol_exec"])
        cost = float(order["cost"])
        return Fill(txid, volume, cost / volume if volume else 0.0, cost, float(order["fee"]))

    def _public(self, method: str, params: dict) -> dict:
        return self._check(self._http.get(f"/0/public/{method}", params=params))

    def _private(self, method: str, params: dict) -> dict:
        if not self._key or not self._secret:
            raise KrakenError("Kraken API key and secret are not configured")
        nonce = max(time.time_ns() // 1000, self._last_nonce + 1)  # strictly increasing
        self._last_nonce = nonce
        data = {"nonce": nonce, **params}
        path = f"/0/private/{method}"
        headers = {"API-Key": self._key, "API-Sign": sign(path, data, self._secret)}
        return self._check(self._http.post(path, data=data, headers=headers))

    @staticmethod
    def _check(response: httpx.Response) -> dict:
        response.raise_for_status()
        payload = response.json()
        if payload.get("error"):
            raise KrakenError("; ".join(payload["error"]))
        return payload["result"]
