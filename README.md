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

## Paper trading

The backtested engine (`engine.step`) runs once a day on Kraken's real CAD candles with a pretend $10,000 CAD account. Settings are frozen into the account when it opens, so later code changes can't silently alter a running account.

```bash
uv run trendbot paper run                     # local account in data/paper-account.json
uv run trendbot paper status --azure <storage-account>   # opens an HTML status page (or set TRENDBOT_STORAGE_ACCOUNT)
uv run trendbot paper resume --azure <storage-account> --note "what you reviewed"
```

- Every completed UTC day since the last run is processed in order, so a missed run catches up.
- A coin with no Kraken price for a day isn't traded that day.
- After a 20% drawdown the account closes its positions and stays paused until you run `resume`.

### Azure

`infra/main.bicep` deploys a Flex Consumption Function App (daily timer at 00:10 UTC, with retries at 06:10 and 18:10 that do nothing if the day is done), a storage account with shared-key access disabled, and Application Insights. Everything authenticates with Entra ID: the app's managed identity in Azure, `az login` on your machine.

```bash
az group create -n rg-trendbot -l canadacentral
az deployment group create -g rg-trendbot -f infra/main.bicep -p ownerPrincipalId=$(az ad signed-in-user show --query id -o tsv)
./scripts/build-function.sh
az functionapp deployment source config-zip -g rg-trendbot -n <function-app> --src build/function.zip --build-remote true
```

The status page is `https://<function-app>.azurewebsites.net/api/status?code=<function key>`.

## Live trading on Kraken

The same engine decides; `live.py` turns each decision into a Kraken market order and records the **actual** fill (price, quantity, fee), so the books match the exchange.

**Safeguards**

- **Two switches for real orders**: the account must be in `live` mode (`trendbot live go-live` with a confirmation phrase) **and** the Function App setting `LIVE_TRADING_ENABLED` must be `true`. With either off, Kraken only validates orders (`validate=true`) and fills are simulated. A Bicep redeploy resets the setting to `false`.
- **Capital cap**: the bot manages only the CAD you give it at `open`, even if the Kraken account holds more.
- **No double orders**: every order carries a `userref` tag for its day, coin and action; a repeated run finds the existing order instead of placing another.
- **Fail safe**: an order error, or Kraken balances that don't cover the bot's book, rolls back the unexecuted decisions and pauses trading until you `resume`.
- **Keys** live in Azure Key Vault; the app's identity can only read them.

**Runbook**

1. On Kraken, create an API key with only: *Query Funds*, *Query Open Orders & Trades*, *Query Closed Orders & Trades*, *Create & Modify Orders*. **Do not allow withdrawals.**
2. Store it (you'll be prompted; nothing is echoed):
   ```bash
   az keyvault secret set --vault-name <vault> --name kraken-api-key --value "$(read -rs k; echo $k)"
   az keyvault secret set --vault-name <vault> --name kraken-api-secret --value "$(read -rs s; echo $s)"
   ```
3. Open a shadow account (needs at least that much CAD on Kraken):
   `uv run trendbot live open --capital 500 --azure <storage-account> --vault <vault-url>`
4. Let it run in shadow mode for a while and watch `/api/live-status`.
5. Go live: `uv run trendbot live go-live --confirm "I understand this trades real money" --azure <storage-account>`, then set `LIVE_TRADING_ENABLED=true` on the Function App.
6. To stop at any time: `uv run trendbot live pause --reason "..." --azure <storage-account>` or set `LIVE_TRADING_ENABLED=false`.
