# Swing sleeve: buy dips in strong stocks, ride the trend, exit when it ends (paper only)

A second, shorter-term robot next to the monthly Quality Momentum robot. It trades a **separate Alpaca paper account**, so it can never touch the monthly robot's positions. There is no live-money switch.

The research behind it, and the backtest results, are in the project file `research/swing-sleeve.md`. Backtest 2010–2026 in short: about +2.6% per trade on average (40% winners averaging +15%, losers averaging −5.6%). The trailing stop carries the profit. Waiting for the dip did not improve timing. All results are flattered by using today's S&P 500 members.

## The rules (checked every weekday after the US close)

**Trend state of each stock**

| State | Meaning |
|---|---|
| Bull | Price above its 200-day average, 50-day above 200-day, and the 50-day rising |
| Bear | Price below its 200-day average and 50-day below 200-day |
| Range | Anything in between |

**Buy** (all must be true)

1. SPY is above its 200-day average (same crash brake as the monthly robot).
2. The stock is in a **bull** state and its 6-month return beats SPY's.
3. It just had a sharp short dip: **2-day RSI below 10**.
4. Price between **$10 and $100**, and at least $20m traded per day.
5. No fresh medium/high "bad news" for it (only if the news layer is on).
6. Strongest 6-month performers first, at most **10 positions**.

**Size: the safety margin**

- First stop = entry price minus **2 × ATR** (ATR = the stock's average daily range over 14 days).
- Buy only so much that hitting that stop loses about **1% of the sleeve**, and never more than **10%** of the sleeve in one stock.
- Example: a $2,000 sleeve buying a $50 stock with a $1.50 ATR. Stop = $47. A $200 position (4 shares) risks about $12.

**Sell** (first one that happens)

1. **First stop:** close below entry minus 2 × ATR.
2. **Trailing stop:** close below the highest close since entry minus 3 × ATR. This follows a winner up and locks in profit.
3. **Trend over:** close below the 200-day average.
4. **No progress:** still not in profit after 60 trading days.

A stock that turns from bull to **bear** is never bought. It goes back on the list only once it is a bull again, then waits for a dip.

**Orders: pre- and after-hours**

The robot runs at 21:45 UTC, which is inside the US after-hours session. It sends **limit orders marked extended-hours eligible** (Alpaca only allows limit orders outside regular hours), so they can fill that evening, in the next pre-market, or in the next regular session:

- buys: limit 0.5% above the last close
- sells: limit 1% below the last close

The robot buys **fractional shares** (rounded down to 4 decimals) when Alpaca allows it for that stock, otherwise whole shares. If Alpaca refuses a fractional order outside regular hours, the robot resends it for the next regular session.

Unfilled orders are cancelled at the next run and re-decided. Spreads are wider and volume is thin outside regular hours, so fills can be worse than in the backtest.

## News layer (optional)

With an `ANTHROPIC_API_KEY` secret, each run reads free RSS headlines (FT, CNBC, MarketWatch, the Federal Reserve, and Yahoo Finance per stock), and Claude scores each one for likely impact on named US stocks.

- **Veto:** fresh medium/high "down" news blocks a planned buy.
- **Alert:** high-impact headlines on stocks you hold or might buy go to your phone. The robot does **not** buy on news.
- **Priced in:** a headline that reports a move that already happened, or a stock that already moved more than one ATR in the news direction that day, is marked "priced in" and never alerted.
- **Log:** every scored headline is appended to `news_log.jsonl` on the `swing-log` branch. After a few months, run *Swing sleeve research → news-validate* to see whether the scores predicted anything. Only then consider letting news trigger buys.

X/Twitter is not included: its API is paid. The FT feed is headlines only.

## Set up (once)

1. In Alpaca, open a **second paper account** (paper dashboard → account menu → *Open new paper account*) and create API keys for it.
2. In GitHub, *Settings → Secrets and variables → Actions*, add:
   - `ALPACA_SWING_KEY_ID` and `ALPACA_SWING_SECRET_KEY`: the second account's keys
   - `ANTHROPIC_API_KEY`: optional, turns on the news layer (costs cents a day)
   - `NTFY_TOPIC` is shared with the monthly robot
3. Optional variables:
   - `SWING_CAPITAL`, e.g. `2000`. The robot then sizes as if the sleeve were $2,000, giving about $200 positions, even though the paper account holds $100,000.
   - `SWING_MAX_PRICE`, e.g. `1000`, to allow pricier stocks such as LLY. The default of $100 is what the backtest used.
   - `SWING_ENTRY=trend` buys strong bull-trend stocks without waiting for a dip. In the backtest this did better than waiting for the dip; the default (`dip`) is the design you asked for.
4. Run *Actions → Swing sleeve robot (paper) → Run workflow* with "Preview only" ticked to see what it would do today.

Until the secrets exist, the daily run just skips. The schedule only runs once this folder is on the repository's default branch.

## Files

| File | What it does |
|---|---|
| `rules.py` | Every number and rule: trend state, entry, sizing, exits |
| `trader.py` | The daily paper robot |
| `backtest.py` | Backtest from 2010, with two control variants |
| `news.py` | RSS headlines, Claude scoring, priced-in check, log |
| `news_validate.py` | Measures whether logged news scores predicted returns |
| `test_swing.py` | Offline tests (synthetic prices, fake broker, mocked Claude) |
