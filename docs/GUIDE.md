# Trendbot operating guide

What the bot does, what to expect from it, and how to run it safely from paper money to real money.

This is a research project, not financial advice. The backtest shows how the rules would have done in the past; it can't promise how they'll do next.

## What it does

Once a day, right after the daily close, Trendbot looks at Bitcoin and Ether prices in Canadian dollars on Kraken and decides whether to buy, hold or sell, following the rules in *Starter Trading Rules: BTC & ETH Trend Following*. It is not trying to predict prices. It joins a trend after it has started and steps aside when the trend breaks, aiming to catch big rises and avoid long crashes.

**The rules in plain words**

| | |
|---|---|
| **Buy a coin** | when its price is above its 50-day average **and** its 20-day average is above its 50-day average. |
| **Sell it** | when the price closes below the 50-day average, **or** falls 2.5 × its normal daily range (ATR) below the highest close since buying. |
| **How much** | enough that being stopped out loses about 1% of the account. Usually 5–15% of the account per coin, never more than 50%. The rest stays in cash. |
| **Safety** | if the account falls 20% from its best value, it sells everything and stops until you review it. It never trades a coin whose price data is missing. |

## What to expect

These are normal, not bugs:

- **It loses on most trades.** In the backtest it won 34% of trades. A few large winners paid for many small losses.
- **It's in cash most of the time.** On average only about 9% of the account was invested. Weeks can pass with no trades.
- **It lags.** It buys after a rise is underway and sells after a fall has started.
- **It trails simply holding Bitcoin in strong bull markets.** Its value is in crashes: its worst fall in the backtest was 19%, while Bitcoin fell 83%.
- **Recent results are modest.** About 17% a year over 2017–2026, but most of that came before 2022. Since 2022 it has made about 3% a year.

## What happens every day

All times are UTC. Winnipeg is UTC−5 in summer and UTC−6 in winter, so the daily close is 7:00 p.m. in summer and 6:00 p.m. in winter.

| Time (UTC) | What happens |
|---|---|
| 00:00 | Kraken's daily candle closes. |
| 00:10 | **Paper run.** Fetches the closed candles, applies the rules to the pretend account, saves the result. |
| 00:20 | **Live run.** Does nothing until you open a live account. Then it places (or, in shadow mode, only validates) the day's orders. |
| 06:10 / 06:20, 18:10 / 18:20 | Retries. They do nothing if the day was already processed, and catch up if an earlier run failed. |

Everything runs in Azure (resource group `rg-trendbot`); your Mac doesn't need to be on.

## The four stages

| Stage | Money at risk | Status |
|---|---|---|
| 1. Backtest | None | Done. See the backtest dashboard. |
| 2. Paper trading | None. Pretend $10,000 CAD on real Kraken prices | **Running since Oct 23, 2026.** |
| 3. Shadow | None. Uses your real Kraken key and balances; Kraken validates every order but none execute | Ready; needs your Kraken API key |
| 4. Live | Real money, capped at the amount you choose | Off. Needs two switches |

## Your routine

**Weekly (5 minutes):** open the paper status page (and the live one once it exists). Check that the "last processed day" is yesterday, and read any new decisions. If the last processed day is more than a day old, something is wrong: check Application Insights in `rg-trendbot`.

**When it pauses:** the status page shows a red banner and the account log says why. Check:

1. **Drawdown halt** (fell 20%): look at what the market did. If it was a broad crash and the rules behaved as written, resuming is reasonable. If something looks wrong (a bad price, a bug), don't resume; investigate first.
2. **Order failed** (live only): read the error. Common causes are not enough CAD on Kraken, a key without trading permission, or Kraken maintenance.
3. **Balances don't match** (live only): someone traded the bot's coins by hand, or moved CAD out. Make the Kraken balances cover the bot's book again (or close the account and open a new one).

Then resume with a note of what you checked:

```bash
uv run trendbot paper resume --azure trendbot5y26lw6lmtr5m --note "Market-wide crash; rules worked as designed"
uv run trendbot live resume  --azure trendbot5y26lw6lmtr5m --note "Topped up CAD after failed order"
```

## Going from paper to real money

Only move on when **every** box is true:

- [ ] Paper trading has run for at least 1–3 months, and its decisions made sense to you each time.
- [ ] Shadow mode has run for at least two weeks with no failed or rejected orders.
- [ ] The Kraken API key cannot withdraw funds.
- [ ] The capital is money you can afford to lose entirely. Start small (for example $300–$500).
- [ ] You'll check the live status page at least weekly (or trade alerts are set up).

**Steps**

1. **Kraken API key.** In Kraken → Settings → API, create a key with only: *Query Funds*, *Query Open Orders & Trades*, *Query Closed Orders & Trades*, *Create & Modify Orders*. Leave every withdrawal and funding permission off.
2. **Store it in Key Vault** (the secret is typed, not shown or saved in your shell history):
   ```bash
   read -rs K && az keyvault secret set --vault-name trendbot-kv-5y26lw6lmt --name kraken-api-key --value "$K" -o none; unset K
   read -rs S && az keyvault secret set --vault-name trendbot-kv-5y26lw6lmt --name kraken-api-secret --value "$S" -o none; unset S
   ```
3. **Open a shadow account.** Kraken must hold at least this much CAD:
   ```bash
   uv run trendbot live open --capital 500 --azure trendbot5y26lw6lmtr5m --vault https://trendbot-kv-5y26lw6lmt.vault.azure.net/
   ```
4. **Watch shadow mode** at `https://trendbot-5y26lw6lmtr5m.azurewebsites.net/api/live-status?code=<key>`. Get the key with
   `az functionapp function keys list -g rg-trendbot -n trendbot-5y26lw6lmtr5m --function-name live_status --query default -o tsv`.
5. **Go live** (both switches):
   ```bash
   uv run trendbot live go-live --confirm "I understand this trades real money" --azure trendbot5y26lw6lmtr5m
   az functionapp config appsettings set -g rg-trendbot -n trendbot-5y26lw6lmtr5m --settings LIVE_TRADING_ENABLED=true
   ```

**To stop at any time**, either works on its own:

```bash
az functionapp config appsettings set -g rg-trendbot -n trendbot-5y26lw6lmtr5m --settings LIVE_TRADING_ENABLED=false
uv run trendbot live pause --reason "Stopping for now" --azure trendbot5y26lw6lmtr5m
```

Stopping doesn't sell anything. Coins the bot holds stay in your Kraken account; sell them on Kraken if you want to exit completely (then close the bot's account, since its book will no longer match).

## Changing settings

Settings (risk per trade, averages, stop width) are frozen into an account when it opens, so a code change can't silently change a running account. To try different settings, backtest them first (`uv run trendbot research`), then open a new account.

## Costs

About cents a month: the daily runs fit within Azure Functions' free allowance, and storage, Key Vault and logs are tiny. Kraken charges about 0.40% per trade (included in every result). The $20 monthly Azure budget alert covers this resource group.

## Taxes

In Canada, each sale or trim of crypto is a taxable event. The live account records every order with its date, price, quantity and fee (`orders` in the account file), which is what you'd need. Ask an accountant how it applies to you.

## Commands

| Command | What it does |
|---|---|
| `uv run pytest` | Runs the tests |
| `uv run trendbot research` | Re-runs the full backtest |
| `uv run trendbot paper status --azure trendbot5y26lw6lmtr5m` | Opens the paper account's status page |
| `uv run trendbot live status --azure trendbot5y26lw6lmtr5m` | Opens the live account's status page |
| `uv run trendbot live pause --reason "..." --azure ...` | Pauses live trading |
| `uv run trendbot {paper,live} resume --note "..." --azure ...` | Resumes after you've reviewed a pause |
| `./scripts/build-function.sh` + `az functionapp deployment source config-zip ...` | Deploys a code change |
