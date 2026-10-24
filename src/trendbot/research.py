"""Runs the full test plan from the rules document and writes the results as JSON."""

from __future__ import annotations

import itertools
import json
from dataclasses import asdict, replace
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from .backtest import buy_and_hold, run_backtest
from .engine import Costs, HaltPolicy
from .data import SYMBOLS, load_history, load_kraken, today_utc
from .metrics import equity_stats, summarize, trade_stats, yearly_returns
from .strategy import StrategyConfig

HISTORY_START = date(2017, 1, 1)
TRADING_START = date(2017, 3, 1)  # after the 50-day averages have warmed up
SPLIT = date(2022, 1, 1)


def _curve(series: pd.Series, every: int = 7) -> list[list]:
    sampled = series.iloc[::every]
    if sampled.index[-1] != series.index[-1]:
        sampled = pd.concat([sampled, series.iloc[[-1]]])
    return [[d.date().isoformat(), round(float(v), 2)] for d, v in sampled.items()]


def run(cache_dir: Path, refresh: bool = False) -> dict:
    candles = {s: load_history(s, HISTORY_START, cache_dir, refresh) for s in SYMBOLS}
    end = min(df.index.max().date() for df in candles.values())
    cfg, costs = StrategyConfig(), Costs()

    main = run_backtest(candles, cfg, costs, start=TRADING_START, end=end)
    benchmarks = {
        "Hold BTC": buy_and_hold(candles, {"BTC": 1.0}, TRADING_START, end),
        "Hold BTC/ETH 50/50": buy_and_hold(candles, {"BTC": 0.5, "ETH": 0.5}, TRADING_START, end),
    }

    periods = {}
    for label, start, stop in [
        ("2017–2021", TRADING_START, SPLIT - timedelta(days=1)),
        ("2022–now", SPLIT, end),
        ("2018–now (without the 2017 boom)", date(2018, 1, 1), end),
    ]:
        result = run_backtest(candles, cfg, costs, start=start, end=stop)
        periods[label] = {
            "strategy": summarize(result),
            "hold_btc": equity_stats(buy_and_hold(candles, {"BTC": 1.0}, start, stop)).as_dict(),
            "hold_5050": equity_stats(buy_and_hold(candles, {"BTC": 0.5, "ETH": 0.5}, start, stop)).as_dict(),
        }

    grid = []
    for slow, fast, mult, rearm in itertools.product((40, 50, 60), (15, 20, 25), (2.0, 2.5, 3.0), (True, False)):
        variant = replace(cfg, slow=slow, fast=fast, atr_mult=mult, rearm_after_exit=rearm)
        result = run_backtest(candles, variant, costs, start=TRADING_START, end=end)
        stats = summarize(result)
        grid.append({
            "slow": slow, "fast": fast, "atr_mult": mult, "rearm": rearm,
            "cagr": stats["equity"]["cagr"], "max_drawdown": stats["equity"]["max_drawdown"],
            "sharpe": stats["equity"]["sharpe"], "trades": stats["trades"]["trades"],
            "win_rate": stats["trades"]["win_rate"],
        })

    # Same rules on real Kraken CAD candles vs converted candles over their overlap.
    kraken = {s: load_kraken(s, cache_dir, refresh) for s in SYMBOLS}
    k_start = max(df.index.min().date() for df in kraken.values()) + timedelta(days=60)
    k_end = min(end, *(df.index.max().date() for df in kraken.values()))
    on_kraken = run_backtest(kraken, cfg, costs, start=k_start, end=k_end)
    on_synthetic = run_backtest(candles, cfg, costs, start=k_start, end=k_end)
    to_key = lambda t: (t.symbol, t.entry_date.isoformat(), t.exit_date.isoformat() if t.exit_date else None)

    # Larger risk settings show how returns scale with risk if 1% feels too cautious.
    risk_levels = []
    for risk in (0.01, 0.02, 0.03):
        stats = summarize(run_backtest(candles, replace(cfg, risk_per_trade=risk), costs, start=TRADING_START, end=end))
        risk_levels.append({"risk_per_trade": risk, **{k: stats["equity"][k] for k in ("cagr", "max_drawdown", "sharpe")},
                            "avg_exposure": stats["avg_exposure"], "halts": stats["halts"]})

    # How the two ambiguous rules change the outcome.
    interpretations = []
    for label, variant, policy in [
        ("As written: cap only when buying, halt stops trading for good", replace(cfg, enforce_cap_daily=False), HaltPolicy("stop")),
        ("Cap only when buying, 30-day review after a halt", replace(cfg, enforce_cap_daily=False), HaltPolicy("review")),
        ("Cap enforced daily, 30-day review after a halt (chosen)", cfg, HaltPolicy("review")),
        ("Cap enforced daily, no halt", cfg, HaltPolicy("ignore")),
    ]:
        stats = summarize(run_backtest(candles, variant, costs, start=TRADING_START, end=end, halt=policy))
        interpretations.append({"label": label, **{k: stats["equity"][k] for k in ("total_return", "cagr", "max_drawdown", "sharpe")},
                                "trades": stats["trades"]["trades"], "halts": stats["halts"]})

    return {
        "generated_on": today_utc().isoformat(),
        "interpretations": interpretations,
        "period": {"start": TRADING_START.isoformat(), "end": end.isoformat()},
        "config": asdict(cfg),
        "costs": asdict(costs),
        "strategy": summarize(main),
        "benchmarks": {name: equity_stats(curve).as_dict() for name, curve in benchmarks.items()},
        "yearly": {
            "strategy": {str(y): v for y, v in yearly_returns(main.equity).items()},
            **{name: {str(y): v for y, v in yearly_returns(curve).items()} for name, curve in benchmarks.items()},
        },
        "curves": {"Strategy": _curve(main.equity), **{n: _curve(c) for n, c in benchmarks.items()}},
        "drawdowns": {
            "Strategy": _curve(main.equity / main.equity.cummax() - 1),
            "Hold BTC": _curve(benchmarks["Hold BTC"] / benchmarks["Hold BTC"].cummax() - 1),
        },
        "exposure": _curve(main.exposure),
        "trades": [
            {
                "symbol": t.symbol, "entry": t.entry_date.isoformat(), "exit": t.exit_date.isoformat(),
                "entry_price": round(t.entry_price, 2), "exit_price": round(t.exit_price, 2),
                "return_pct": t.return_pct, "pnl": t.pnl, "reason": t.reason,
            }
            for t in main.trades
        ],
        "open_positions": [
            {"symbol": s, "entry": t.entry_date.isoformat(), "entry_price": round(t.entry_price, 2)}
            for s, t in main.open_positions.items()
        ],
        "periods": periods,
        "robustness": grid,
        "risk_levels": risk_levels,
        "kraken_check": {
            "start": k_start.isoformat(), "end": k_end.isoformat(),
            "kraken_trades": len(on_kraken.trades), "synthetic_trades": len(on_synthetic.trades),
            "matching_trades": len({to_key(t) for t in on_kraken.trades} & {to_key(t) for t in on_synthetic.trades}),
            "kraken_return": equity_stats(on_kraken.equity).total_return,
            "synthetic_return": equity_stats(on_synthetic.equity).total_return,
        },
    }


def main() -> None:
    out = Path("reports")
    out.mkdir(exist_ok=True)
    results = run(Path("data"))
    (out / "backtest.json").write_text(json.dumps(results, indent=2, default=float))

    s, b = results["strategy"], results["benchmarks"]
    pct = lambda x: f"{x:+.1%}"
    print(f"Backtest {results['period']['start']} → {results['period']['end']} (CAD, fees + slippage)")
    print(f"{'':22}{'Total':>10}{'CAGR':>9}{'Max DD':>9}{'Sharpe':>8}")
    rows = [("Strategy", s["equity"])] + list(b.items())
    for name, e in rows:
        print(f"{name:22}{pct(e['total_return']):>10}{pct(e['cagr']):>9}{pct(e['max_drawdown']):>9}{e['sharpe']:>8.2f}")
    t = s["trades"]
    print(f"\nTrades {t['trades']}  win rate {t['win_rate']:.0%}  avg win {t['avg_win']:+.1%}  avg loss {t['avg_loss']:+.1%}"
          f"  profit factor {t['profit_factor']:.2f}  avg exposure {s['avg_exposure']:.0%}  halts {s['halts']}")
    print(f"exits: {t['exits']}")
    for label, p in results["periods"].items():
        e = p["strategy"]["equity"]
        print(f"{label}: strategy CAGR {pct(e['cagr'])} DD {pct(e['max_drawdown'])} | BTC CAGR {pct(p['hold_btc']['cagr'])} DD {pct(p['hold_btc']['max_drawdown'])}")
    grid = pd.DataFrame(results["robustness"])
    print(f"\nRobustness ({len(grid)} variants): CAGR min {grid.cagr.min():+.1%} median {grid.cagr.median():+.1%} max {grid.cagr.max():+.1%};"
          f" profitable {int((grid.cagr > 0).sum())}/{len(grid)}; worst DD {grid.max_drawdown.min():+.1%}")
    for r in results["risk_levels"]:
        print(f"risk {r['risk_per_trade']:.0%}: CAGR {pct(r['cagr'])} DD {pct(r['max_drawdown'])} exposure {r['avg_exposure']:.0%} halts {r['halts']}")
    print()
    for i in results["interpretations"]:
        print(f"{i['label']:62} total {pct(i['total_return']):>9} CAGR {pct(i['cagr']):>7} DD {pct(i['max_drawdown']):>7} trades {i['trades']:>3} halts {len(i['halts'])}")
    print(f"Kraken check: {results['kraken_check']}")


if __name__ == "__main__":
    main()
