# Quality Momentum Strategy — Complete Playbook

*Zulfiqar Kamal · Last updated 2026-11-13*

---

## What This Strategy Is and How It Makes Money

The strategy buys the strongest S&P 500 stocks and holds them for a month at a time. That is the whole idea.

“Strongest” means: the stock is profitable, its revenue is growing, it has manageable debt, and its price has risen more than most other S&P 500 stocks over the past 12 months — and it is still above its own 200-day average price (so it is in an uptrend right now, not just recovering from a fall).

Every month-end the robot checks the full S&P 500 (about 500 stocks), ranks them by this 12-month momentum score, and holds the top 10 names. Stocks that fall below the top 30 are sold; ones that trigger the 20% stop-loss are sold the same day. If the whole market falls below its 200-day trend line, everything is sold and the account moves to cash until the market recovers.

**Where the profit comes from:** Academic research (Jegadeesh & Titman 1993, Fama & French 2008 and many others) shows that stocks that have gone up recently tend to keep going up for another 3-12 months. This “momentum anomaly” has worked in every major market for over 100 years. Combining it with a quality filter (profitable, growing revenue) reduces the chance of holding “junk” stocks that are only temporarily popular.

**Why monthly, not daily:** A 2026 backtest over 2005-2026 on S&P 500 stocks showed monthly rebalancing produced 21.7% CAGR vs 19.1% for daily. Transaction costs eat daily strategies alive and momentum works over months, not days.

---

## The Full Pipeline

### Every weekday at 21:30 UTC (after the US market close)

GitHub Actions runs `autotrader.py`. It does two things:

**1. Daily stop-loss check**
Any holding that is 20% below its purchase price is sold immediately. This caps the loss on any single bad stock.

**2. Month-end rebalance** *(last trading day of each month only)*
- Download 15 months of price data for all S&P 500 stocks.
- Check if SPY is above its 200-day average. If not → sell everything, hold cash (the “crash brake”).
- Rank every stock by 12-1 momentum (12-month return, skip the last month).
- Quality-check the top candidates: profitable? revenue growing? debt/equity under 2×?
- NEW in Nov-2026: Cross-check new buys against TradingView live technicals — RSI below 75, MACD positive, price at least 2% above 200-day average. This avoids buying overextended stocks.
- Pick the top 10 that pass all filters, respecting a cap of 3 stocks per sector.
- Sell anything no longer in the top 30. Buy everything new.
- Size each position at 10% of the account.

**TradingView webhook crash brake** *(additional layer)*
A TradingView Pine Script alert fires to a Cloudflare Worker, which triggers a GitHub Actions workflow, which forces a sell-all. This means the crash brake can fire intraday from a TV alert, not just at the month-end check.

### Weekly review (Fridays)
Check paper-trading results against the go-live thresholds below. Report win rate, average gain, average loss, and open positions.

---

## Backtest Results (2005–2026, S&P 500 universe)

| Metric | Quality Momentum (monthly) | S&P 500 buy-and-hold |
|---|---|---|
| CAGR | **21.7%** | ~10% |
| Max drawdown | -40% (2008-09) | -57% (2008-09) |
| Sharpe ratio | 0.94 | ~0.5 |
| Annual turnover | ~100% | 0% |

**Important caveats:**
- The backtest uses today’s S&P 500 membership (survivorship bias — dead companies are not included). Real results will likely be a few percent lower.
- No point-in-time fundamental data was used; quality checks used Yahoo Finance’s current figures. Some stocks would have failed the quality filter at the time but pass today.
- The -40% drawdown happened during the 2008 financial crisis. The crash brake (sell when SPY < 200-day) should reduce this significantly in a live account, but the brake fires at month-end so there is still a gap risk.

**What the new TradingView entry filter is designed to do:**
Avoid buying stocks when they are already overbought (RSI ≥ 75) or the momentum is not confirmed by MACD. In the backtest data, stocks bought with RSI > 75 at entry underperformed by an average of 3-5% over the next 3 months and had a higher chance of a sharp reversal. This filter should reduce peak-to-trough drawdowns on individual positions without meaningfully cutting overall CAGR.

---

## Go-Live Checklist — What “Ready for Real Money” Looks Like

The bar was set on 2026-10-09. All three groups must be met before switching `LIVE_TRADING=yes-real-money`.

### Group 1: Paper-trading track record (objective thresholds)
- [ ] At least **3 months** of paper trading on the Alpaca paper account
- [ ] At least **15–20 closed trades** (not open positions, but completed buy-then-sell cycles)
- [ ] **Win rate ≥ 55%** (more than half of closed trades ended positive)
- [ ] **Average loss ≤ −10%** (losers did not get out of hand)
- [ ] **Average trade return ≥ +6%** (winners more than cover losers)
- [ ] **3–5 positions open at any one time** (too few = not diversified; too many = signal diluted)

### Group 2: Infrastructure checks
- [ ] GitHub Actions autotrader ran without errors for at least 20 consecutive weekdays
- [ ] At least one crash-brake event was triggered and handled correctly (or manually simulated with `--force-monthly --dry-run`)
- [ ] Phone alerts (ntfy) confirmed working for at least 3 trade events
- [ ] TradingView webhook pipeline tested end-to-end: TV alert → Cloudflare Worker → GitHub Actions → Alpaca order

### Group 3: Account sizing
- [ ] At least **$500 available** in the live Alpaca account (strategy minimum)
- [ ] `STRATEGY_FRACTION` set to 1.0 or the fraction you are comfortable with
- [ ] All secrets (`ALPACA_KEY_ID`, `ALPACA_SECRET_KEY`, `NTFY_TOPIC`) added to the **live** GitHub Actions environment (separate from paper secrets)

### Progress as of 2026-11-13
Paper trading started. Track record building. The go-live date is estimated at **early February 2027** if the robot continues running cleanly.

---

## For Followers — How to Copy This Strategy

### What you need
- An **Alpaca brokerage account** (free; supports paper trading before you go live): alpaca.markets
- A **GitHub account** (free): fork the repository `zulfiqarkamal/Github-cloud`
- A **TradingView account** (free tier is fine for the alerts)
- An **ntfy.sh topic** (free) for phone notifications

### Setup steps
1. Fork the repo on GitHub.
2. Add your own Alpaca API keys as GitHub Actions secrets: `ALPACA_KEY_ID`, `ALPACA_SECRET_KEY`, `NTFY_TOPIC`.
3. Set `LIVE_TRADING` to nothing (leave unset) for paper trading. Set to `yes-real-money` only when you are ready.
4. The workflow in `.github/workflows/autotrader.yml` runs every weekday at 21:30 UTC automatically.
5. Import `research/quality-momentum/tradingview_alerts.pine` into TradingView on the SPY chart and set it to alert your Cloudflare Worker URL.
6. Watch the GitHub Actions run logs and your phone for trade alerts.

### What to expect
- **Monthly trades:** 2-5 buys and sells per month on average.
- **Minimum capital:** $500 (each of 10 positions is ~$50; below that, lot sizes round down to zero).
- **Practical minimum for meaningful returns:** $5,000–$10,000.
- **This is not a get-rich-quick strategy.** The backtest shows ~22% per year on average, but individual years vary widely. 2008 saw a -40% drawdown before recovering.
- **Paper-trade for at least 3 months before using real money.** See the go-live checklist above.

### Transparency
The full source code is in the GitHub repository. Every trade is logged in GitHub Actions run history. The strategy has no black box — all rules are written in plain Python in `strategy.py` and `autotrader.py`.

---

## Questions and Contact

This project is a personal research effort by Zulfiqar Kamal (IT Audit & GRC professional, Copenhagen). The strategy is shared for educational purposes and does not constitute financial advice. Past backtest performance does not guarantee future results.

For questions or to follow progress, follow the GitHub repository.
