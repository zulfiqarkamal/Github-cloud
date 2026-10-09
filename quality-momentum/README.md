# Quality Momentum: a rule-based US stock strategy (paper trading only)

These are research tools. Nothing here places live trades: the order script only connects to an IBKR **paper** account.

## Which approach, and why

| Approach | What the evidence says | Reliability | Time needed | Verdict |
|---|---|---|---|---|
| Day trading | 97% of persistent Brazilian day traders and 80%+ of Taiwanese ones lost money after costs | Very low | Hours a day | No |
| Copy trading (eToro, Collective2) | Leaderboards only show survivors; copiers pay fees and get worse prices than the trader | Low | Low | Ideas only |
| Post-earnings drift | Gone in non-microcap US stocks since about 2006 (Martineau 2022) | Low | Medium | No |
| Plain index fund (S&P 500 or a global ETF) | About 10% a year over the long run. Most professional funds fail to beat it over 15 years (S&P SPIVA scorecards) | **Highest** | Almost none | **Core** |
| Index + 200-day crash brake | Similar return with smaller crashes historically. Can whipsaw in choppy markets (Faber 2007) | Medium-high | 5 min a month | Optional |
| Sector-ETF momentum | Momentum across sectors, with no stock-picking risk | Medium | 30 min a month | Satellite candidate |
| **Quality Momentum (stocks)** | Momentum (Jegadeesh & Titman 1993) plus profitability (Novy-Marx 2013) plus a trend filter (Hurst, Ooi & Pedersen) | Medium | 1 hour a month | **Satellite** |

**Recommendation: core + satellite.**

- **70%** in a plain index fund. This is the "relatively safe" part, and it's the hardest benchmark for anyone to beat.
- **30%** in Quality Momentum. This part is the attempt to beat the market.

Only scale the satellite up if it passes the backtest *and* 3 months of paper trading. No strategy beats the market reliably every year. Anyone who claims one does is guessing or selling.

## Exact rules (checked on the last trading day of each month)

**Entry**

1. Universe: S&P 500 stocks priced above $10.
2. Quality filter: positive net income over the last 12 months, revenue growing year on year, and debt/equity below 2 (banks exempt from the debt rule).
3. Trend: the stock closes above its 200-day moving average.
4. Rank by 12-month return excluding the last month. Buy the **top 10 at 10% each**, with no more than **3 per sector**.
5. Crash brake: only buy when **SPY closes above its 200-day average**.

**Exit**

6. Sell a holding when it drops out of the top 30, or closes below its own 200-day average.
7. Sell **everything** when SPY closes below its 200-day average, and hold cash until SPY is back above it.
8. Sell straight away if a stock falls **20% below its entry price**.

**Risk**

9. No leverage, options or CFDs. At most 10% of the satellite in any one stock.
10. If the satellite falls 20% from its peak, stop buying and review the strategy.
11. Never override the rules because of news or a gut feeling. Log every trade.

All the numbers live in `RULES` in `strategy.py`.

## Step 1: run the backtest on your computer

The free price sites are blocked from the cloud, so this runs locally. It needs Python 3.10+.

```bash
cd quality-momentum
pip install -r requirements.txt
python backtest.py                    # 2005 -> today, takes a few minutes
```

It compares four strategies:

1. SPY buy-and-hold
2. SPY with the crash brake
3. Sector-ETF momentum
4. Quality Momentum (stocks)

For each one it prints:

- CAGR, volatility, Sharpe, max drawdown and worst year;
- returns in each half of the period, plus year by year;
- a **PASS/FAIL** line against the stage-1 criteria.

CSVs are written to `results/`.

**Read the results honestly:**

- Strategy 4 uses *today's* S&P 500 members, which flatters the past (survivorship bias).
- The quality filter can't be backtested with free data.
- Strategy 3 (ETFs) has neither problem, so treat it as the honest check.
- Trades happen at the same day's close that generates the signal. That's a small simplification.

To check the code works without internet, run `python backtest.py --synthetic`. It uses random prices, so the numbers mean nothing.

## Step 2: monthly signals and paper trading

```bash
echo {} > holdings.json
python signals.py --capital 30000          # dry run: prints the list, writes orders.json
python signals.py --capital 30000 --ibkr-paper   # also sends the orders to IBKR paper
```

For `--ibkr-paper`, TWS or IB Gateway must be running, logged in to your **paper** account, with the API enabled on port 7497. The script refuses the live ports and any account that isn't a paper account (paper account ids start with `DU`).

After the orders fill, update `holdings.json`:

```json
{"AAPL": {"qty": 12, "entry": 231.40}}
```

## Step 3: TradingView alerts (optional)

To set up `tradingview_alerts.pine`:

1. In TradingView, open the Pine Editor, paste the script and add it to a chart.
2. **On SPY:** create alerts on "Closed below 200-day" and "Closed above 200-day". This is the crash brake.
3. **On each stock you hold:** enter your purchase price in the settings, then create an alert on "Stop hit".

Alerts can go to the TradingView app or email. They can also go by webhook to n8n (for a log or notification) or to SignalStack, which routes them to IBKR paper for free.

## Automating the monthly run with n8n (later)

There are two ways:

- n8n's Execute Command node runs `python signals.py` on the last trading day of the month. This only works on a self-hosted n8n, not n8n Cloud.
- A cron job on your computer runs the script and posts `orders.json` to an n8n webhook, which emails or messages you the list.

Keep a human approval step before anything is sent, even to paper.
