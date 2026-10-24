"""Turns reports/backtest.json into the CSV files the backtest dashboard reads."""

from __future__ import annotations

import csv
import json
from pathlib import Path


def _write(path: Path, rows: list[dict]) -> None:
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def export(results: dict, out: Path) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    r4 = lambda x: round(float(x), 4)
    files = {}

    strategy, benchmarks = results["strategy"], results["benchmarks"]
    files["summary.csv"] = [
        {"series": name, **{k: r4(e[k]) for k in ("total_return", "cagr", "max_drawdown", "sharpe", "volatility")}}
        for name, e in [("Strategy", strategy["equity"]), *benchmarks.items()]
    ]

    t, k = strategy["trades"], results["kraken_check"]
    files["stats.csv"] = [{
        "start": results["period"]["start"], "end": results["period"]["end"],
        "trades": t["trades"], "win_rate": r4(t["win_rate"]), "avg_win": r4(t["avg_win"]), "avg_loss": r4(t["avg_loss"]),
        "profit_factor": r4(t["profit_factor"]), "avg_holding_days": round(t["avg_holding_days"], 1),
        "avg_exposure": r4(strategy["avg_exposure"]), "halts": len(strategy["halts"]),
        "fee_rate": results["costs"]["fee_rate"], "slippage": results["costs"]["slippage"],
        "kraken_start": k["start"], "kraken_end": k["end"], "kraken_trades": k["kraken_trades"],
        "converted_trades": k["synthetic_trades"], "matching_trades": k["matching_trades"],
        "kraken_return": r4(k["kraken_return"]), "converted_return": r4(k["synthetic_return"]),
    }]

    drawdowns = {name: dict(points) for name, points in results["drawdowns"].items()}
    files["curves.csv"] = [
        {"date": day, "series": name, "value": value, "drawdown": r4(drawdowns[name][day]) if name in drawdowns else ""}
        for name, points in results["curves"].items()
        for day, value in points
    ]

    files["periods.csv"] = [
        {
            "period": label,
            "strategy_cagr": r4(p["strategy"]["equity"]["cagr"]),
            "strategy_max_drawdown": r4(p["strategy"]["equity"]["max_drawdown"]),
            "btc_cagr": r4(p["hold_btc"]["cagr"]), "btc_max_drawdown": r4(p["hold_btc"]["max_drawdown"]),
            "mix_cagr": r4(p["hold_5050"]["cagr"]), "mix_max_drawdown": r4(p["hold_5050"]["max_drawdown"]),
            "strategy_trades": p["strategy"]["trades"]["trades"],
        }
        for label, p in results["periods"].items()
    ]

    yearly = results["yearly"]
    files["yearly.csv"] = [
        {"year": int(y), "strategy": r4(v), "hold_btc": r4(yearly["Hold BTC"][y]), "hold_mix": r4(yearly["Hold BTC/ETH 50/50"][y])}
        for y, v in yearly["strategy"].items()
    ]

    cfg = results["config"]
    files["robustness.csv"] = [
        {
            "variant": f"{g['fast']}/{g['slow']} · {g['atr_mult']}×ATR · {'rearm' if g['rearm'] else 'immediate'}",
            "fast": g["fast"], "slow": g["slow"], "atr_mult": g["atr_mult"],
            "reentry": "wait for reset" if g["rearm"] else "immediate",
            "cagr": r4(g["cagr"]), "max_drawdown": r4(g["max_drawdown"]), "sharpe": r4(g["sharpe"]),
            "trades": g["trades"], "win_rate": r4(g["win_rate"]),
            "is_default": g["fast"] == cfg["fast"] and g["slow"] == cfg["slow"]
            and g["atr_mult"] == cfg["atr_mult"] and g["rearm"] == cfg["rearm_after_exit"],
        }
        for g in results["robustness"]
    ]

    files["interpretations.csv"] = [
        {"reading": i["label"], **{k: r4(i[k]) for k in ("total_return", "cagr", "max_drawdown", "sharpe")},
         "trades": i["trades"], "halts": len(i["halts"])}
        for i in results["interpretations"]
    ]

    files["risk.csv"] = [
        {"risk_per_trade": r["risk_per_trade"], **{k: r4(r[k]) for k in ("cagr", "max_drawdown", "sharpe", "avg_exposure")},
         "halts": len(r["halts"])}
        for r in results["risk_levels"]
    ]

    files["trades.csv"] = [
        {"id": n + 1, "symbol": tr["symbol"], "entry": tr["entry"], "exit": tr["exit"],
         "entry_price": tr["entry_price"], "exit_price": tr["exit_price"],
         "return_pct": r4(tr["return_pct"]), "pnl": round(tr["pnl"], 2), "reason": tr["reason"]}
        for n, tr in enumerate(results["trades"])
    ]

    paths = []
    for name, rows in files.items():
        path = out / name
        _write(path, rows)
        paths.append(path)
    return paths


def main() -> None:
    results = json.loads(Path("reports/backtest.json").read_text())
    for path in export(results, Path("reports/dashboard")):
        print(path, path.stat().st_size)


if __name__ == "__main__":
    main()
