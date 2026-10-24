# trendbot

Trend-following bot for BTC and ETH on Kraken, in CAD. Built in three stages: backtest, paper trade, then live with small amounts.

**Rules** (from "Starter Trading Rules: BTC & ETH Trend Following"), checked once a day at the UTC daily close:

- **Buy** when price > 50-day average and 20-day average > 50-day average.
- **Sell** when price closes below the 50-day average, or below a trailing stop 2.5 × ATR(14) under the highest close since buying.
- **Size** so a stop-out loses at most 1% of the account; at most 50% of the account in one coin; the rest stays in cash.
- **Safety**: a 20% fall from the account's peak closes everything and pauses trading for review; no trading on missing or stale prices.

Two points the rules leave open are made explicit in `StrategyConfig`: the 50% cap is enforced daily (winners are trimmed), and after a stop-out the bot waits for the buy signal to reset before buying again.

## Research

```bash
uv run python -m trendbot.research   # writes reports/backtest.json
uv run pytest
```

Backtests use Coinbase USD candles converted to CAD with the Bank of Canada daily rate (Kraken's public API only has ~2 years); this matches Kraken's real CAD closes to within 0.2% on average. Costs: 0.40% fee + 0.10% slippage per side.

This is a research tool, not financial advice. Past results don't predict future returns.
