"""A self-contained HTML page describing a paper account: value, today's signals, positions, decisions and trades."""

from __future__ import annotations

from datetime import date
from html import escape

from .engine import Portfolio
from .strategy import StrategyConfig

REASONS = {
    "trend_break": "Price closed below the 50-day average",
    "trailing_stop": "Trailing stop hit",
    "trim_to_cap": "Trimmed back to 50% of the account",
    "drawdown_halt": "Safety halt: account fell 20% from its peak",
    "trend_up": "Price above the 50-day average, 20-day above 50-day",
}


def _money(v: float | None) -> str:
    return "—" if v is None else f"${v:,.2f}"


def _pct(v: float | None, sign: bool = True) -> str:
    return "—" if v is None else (f"{v:+.1%}" if sign else f"{v:.1%}")


def _sparkline(values: list[float], width: int = 640, height: int = 120) -> str:
    if len(values) < 2:
        return '<p class="muted">The chart appears after the second day of trading.</p>'
    lo, hi = min(values), max(values)
    span = hi - lo or 1
    pad = 8
    points = " ".join(
        f"{pad + i * (width - 2 * pad) / (len(values) - 1):.1f},{height - pad - (v - lo) * (height - 2 * pad) / span:.1f}"
        for i, v in enumerate(values)
    )
    last_x, last_y = points.split(" ")[-1].split(",")
    return (
        f'<svg viewBox="0 0 {width} {height}" width="100%" role="img" aria-label="Account value over time">'
        f'<polyline points="{points}" fill="none" stroke="var(--accent)" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>'
        f'<circle cx="{last_x}" cy="{last_y}" r="3.5" fill="var(--accent)"/></svg>'
        f'<div class="axis"><span>{_money(lo)}</span><span>{_money(hi)}</span></div>'
    )


def _coin_status(symbol: str, signal: dict, pf: Portfolio) -> tuple[str, str]:
    if not signal.get("valid"):
        return "warn", "No trade: Kraken had no price for this day"
    if symbol in pf.positions:
        stop = signal.get("stop")
        return "ok", f"Holding · sells below {_money(stop)} or the 50-day average {_money(signal['sma_slow'])}"
    if signal.get("entry_signal"):
        return "ok", "Buy signal (waits for the trend to reset if it just sold)"
    if signal["close"] <= signal["sma_slow"]:
        gap = signal["sma_slow"] / signal["close"] - 1
        return "muted", f"Waiting: price is {gap:.1%} below the 50-day average"
    return "muted", "Waiting: 20-day average is below the 50-day average"


def render_status(state: dict | None, now: date | None = None) -> str:
    if state is None:
        body = '<p>No paper account yet. It opens on the first daily run after 00:10 UTC.</p>'
        return _page(body)

    pf = Portfolio.from_dict(state["portfolio"])
    cfg = StrategyConfig(**state["config"])
    days = state["days"]
    initial = state["initial_cash"]
    equity = days[-1]["equity"] if days else initial
    invested = days[-1]["invested"] if days else 0.0
    peak = max([initial] + [d["equity"] for d in days])
    paused = pf.paused_until is not None

    banner = ""
    if paused:
        banner = ('<div class="banner" role="alert"><strong>Trading is paused.</strong> The account fell '
                  f"{cfg.max_drawdown:.0%} from its peak. Review it, then run <code>trendbot paper resume</code>.</div>")

    tiles = f"""
      <div class="tiles">
        <div><span class="label">Account value</span><span class="big">{_money(equity)}</span></div>
        <div><span class="label">Since {escape(state['started_on'])}</span><span class="big {('good' if equity >= initial else 'bad')}">{_pct(equity / initial - 1)}</span></div>
        <div><span class="label">Invested</span><span class="big">{_pct(invested / equity if equity else 0, sign=False)}</span></div>
        <div><span class="label">Below peak</span><span class="big">{_pct(equity / peak - 1)}</span></div>
      </div>"""

    latest = days[-1] if days else None
    coins = ""
    if latest:
        rows = []
        for symbol, signal in latest["signals"].items():
            tone, text = _coin_status(symbol, signal, pf)
            rows.append(
                f"<tr><td>{escape(symbol)}</td><td>{_money(signal.get('close'))}</td><td>{_money(signal.get('sma_fast'))}</td>"
                f"<td>{_money(signal.get('sma_slow'))}</td><td class='{tone} wrap'>{escape(text)}</td></tr>"
            )
        coins = (f"<h2>Signals at the {escape(latest['day'])} close</h2>"
                 "<div class='scroll'><table><thead><tr><th>Coin</th><th>Close</th><th>20-day avg</th><th>50-day avg</th><th>Status</th></tr></thead>"
                 f"<tbody>{''.join(rows)}</tbody></table></div>")

    positions = "".join(
        f"<tr><td>{escape(s)}</td><td>{escape(p.trade.entry_date.isoformat())}</td><td>{_money(p.trade.entry_price)}</td>"
        f"<td>{p.trade.quantity:.6f}</td><td>{_money(p.trade.quantity * pf.last_close[s])}</td>"
        f"<td class='{'good' if pf.last_close[s] >= p.trade.entry_price else 'bad'}'>{_pct(pf.last_close[s] / p.trade.entry_price - 1)}</td></tr>"
        for s, p in pf.positions.items()
    ) or "<tr><td colspan='6' class='muted'>No open positions. The account is in cash.</td></tr>"

    decisions = [
        (d["day"], a) for d in reversed(days) for a in d["actions"]
    ][:40]
    decision_rows = "".join(
        f"<tr><td>{escape(day)}</td><td>{escape(a['kind'].title())}</td><td>{escape(a.get('symbol') or '')}</td>"
        f"<td>{_money(a.get('price'))}</td><td class='wrap'>{escape(REASONS.get(a.get('reason', ''), a.get('reason', '')))}</td></tr>"
        for day, a in decisions
    ) or "<tr><td colspan='5' class='muted'>No trades yet. Most days the rules do nothing.</td></tr>"

    trade_rows = "".join(
        f"<tr><td>{escape(t.symbol)}</td><td>{t.entry_date.isoformat()}</td><td>{t.exit_date.isoformat()}</td>"
        f"<td class='{'good' if t.pnl > 0 else 'bad'}'>{_pct(t.return_pct)}</td><td>{_money(t.pnl)}</td>"
        f"<td class='wrap'>{escape(REASONS.get(t.reason or '', t.reason or ''))}</td></tr>"
        for t in reversed(pf.trades)
    ) or "<tr><td colspan='6' class='muted'>No closed trades yet.</td></tr>"

    events = "".join(f"<li><span class='muted'>{escape(e['at'][:10])}</span> {escape(e['detail'])}</li>" for e in reversed(state["events"]))

    body = f"""
      {banner}
      {tiles}
      <section><h2>Account value</h2>{_sparkline([initial] + [d['equity'] for d in days])}</section>
      <section>{coins}</section>
      <section><h2>Open positions</h2><div class="scroll"><table><thead><tr><th>Coin</th><th>Bought</th><th>Buy price</th><th>Quantity</th><th>Value</th><th>Change</th></tr></thead><tbody>{positions}</tbody></table></div></section>
      <section><h2>Recent decisions</h2><div class="scroll"><table><thead><tr><th>Day</th><th>Action</th><th>Coin</th><th>Price</th><th>Why</th></tr></thead><tbody>{decision_rows}</tbody></table></div></section>
      <section><h2>Closed trades</h2><div class="scroll"><table><thead><tr><th>Coin</th><th>Bought</th><th>Sold</th><th>Return</th><th>Profit</th><th>Why it sold</th></tr></thead><tbody>{trade_rows}</tbody></table></div></section>
      <section><h2>Account log</h2><ul class="log">{events}</ul></section>
      <p class="muted">Paper account on Kraken CAD prices with {state['costs']['fee_rate']:.2%} fees and {state['costs']['slippage']:.2%} slippage per side.
        Risk per trade {cfg.risk_per_trade:.0%}, at most {cfg.max_coin_fraction:.0%} in one coin. Last processed day: {escape(state['last_day'])}.
        Runs daily at 00:10 UTC. No real money is involved.</p>"""
    return _page(body)


def _page(body: str) -> str:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex"><title>Trendbot paper account</title>
<style>
  :root {{ --bg: #f7f8f6; --panel: #ffffff; --fg: #17201b; --muted: #5d6b63; --line: #dfe4e0; --accent: #1f7a5a; --good: #1f7a5a; --bad: #b4372c; --warn: #9a6400; color-scheme: light; }}
  @media (prefers-color-scheme: dark) {{ :root {{ --bg: #111614; --panel: #18201c; --fg: #e6ece8; --muted: #9aa8a0; --line: #2b3530; --accent: #5cc39b; --good: #5cc39b; --bad: #f08a7e; --warn: #e3b25a; color-scheme: dark; }} }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; background: var(--bg); color: var(--fg); font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }}
  main {{ max-width: 60rem; margin: 0 auto; padding: 24px max(16px, env(safe-area-inset-left)) 48px; display: grid; gap: 24px; }}
  h1 {{ font-size: 1.5rem; margin: 0; display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }}
  h2 {{ font-size: 1rem; margin: 0 0 8px; }}
  .pill {{ font-size: .75rem; font-weight: 600; letter-spacing: .06em; padding: 2px 8px; border-radius: 999px; border: 1px solid var(--line); color: var(--muted); }}
  .tiles {{ display: grid; gap: 12px; grid-template-columns: repeat(auto-fit, minmax(10rem, 1fr)); }}
  .tiles > div {{ background: var(--panel); border: 1px solid var(--line); border-radius: 10px; padding: 14px; display: grid; gap: 4px; }}
  .label {{ color: var(--muted); font-size: .8rem; }}
  .big {{ font-size: 1.5rem; font-variant-numeric: tabular-nums; }}
  section {{ background: var(--panel); border: 1px solid var(--line); border-radius: 10px; padding: 16px; min-width: 0; }}
  .scroll {{ overflow-x: auto; }}
  table {{ border-collapse: collapse; width: 100%; font-variant-numeric: tabular-nums; }}
  th, td {{ text-align: right; padding: 6px 8px; border-bottom: 1px solid var(--line); white-space: nowrap; }}
  th:first-child, td:first-child {{ text-align: left; }}
  td.wrap {{ white-space: normal; text-align: left; min-width: 14rem; }}
  th {{ color: var(--muted); font-weight: 600; font-size: .8rem; }}
  .good {{ color: var(--good); }} .bad {{ color: var(--bad); }} .warn {{ color: var(--warn); }} .muted {{ color: var(--muted); }} .ok {{ color: var(--fg); }}
  .banner {{ border: 1px solid var(--bad); color: var(--bad); border-radius: 10px; padding: 12px 16px; background: var(--panel); }}
  .axis {{ display: flex; justify-content: space-between; color: var(--muted); font-size: .8rem; }}
  .log {{ margin: 0; padding-left: 18px; display: grid; gap: 4px; }}
  code {{ font-family: ui-monospace, monospace; font-size: .9em; }}
</style></head>
<body><main><h1>Trendbot <span class="pill">PAPER</span></h1>{body}</main></body></html>"""
