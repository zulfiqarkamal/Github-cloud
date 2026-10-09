"""Swing sleeve robot on its OWN Alpaca paper account (paper only, no live switch).

Runs once every weekday in the US after-hours session (see
.github/workflows/swing-trader.yml), so its limit orders can fill after hours,
in the next pre-market, or at the next regular session:
  1. sells: first stop, trailing stop, trend over (below 200-day), or no progress
  2. buys: dip in a strong bull-trend stock, priced $10-100, only while SPY is
     above its 200-day average, sized to risk ~1% of the sleeve, max 10% each
  3. news (optional): vetoes buys on fresh bad news, alerts on big headlines
Every action goes to your phone (ntfy) and the GitHub run summary.

It uses a SEPARATE Alpaca paper account so it can never touch the monthly
Quality Momentum robot's positions.

Settings (GitHub secrets / variables):
  ALPACA_SWING_KEY_ID, ALPACA_SWING_SECRET_KEY   keys of the second paper account
  NTFY_TOPIC                                     phone alerts (same as the monthly robot)
  SWING_CAPITAL                                  dollars the sleeve may use (default: whole account)
  ANTHROPIC_API_KEY                              optional, turns on the news layer

    python trader.py --dry-run     # decide and notify, place no orders
"""
import argparse
import os
import sys
from datetime import date

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "quality-momentum"))
from autotrader import Alpaca  # noqa: E402
from autotrader import notify as _notify  # noqa: E402
from rules import (BULL, RULES, STATE_NAME, download_ohlcv, entry_signals,  # noqa: E402
                   exit_reason, indicators, position_size, trend_state)

DATA_URL = "https://data.alpaca.markets"
TITLE = "Swing sleeve"

# Backtest result per trade, filled in from swing/backtest.py (see README).
# Shown in alerts as "what to expect", already cut in half as a safety margin.
BACKTEST_AVG_TRADE = None


def notify(lines, title=TITLE):
    _notify(lines, title)


class SwingAlpaca(Alpaca):
    def cancel_open_orders(self):
        return self._req("DELETE", "/v2/orders")

    def limit_order(self, ticker, qty, side, limit):
        """Extended-hours eligible day limit order (Alpaca only allows limit
        orders outside regular hours)."""
        return self._req("POST", "/v2/orders", json={
            "symbol": ticker.replace("-", "."), "qty": str(int(qty)), "side": side,
            "type": "limit", "limit_price": f"{limit:.2f}", "time_in_force": "day",
            "extended_hours": True})

    def entry_dates(self):
        """{ticker: date of the most recent filled buy}"""
        out = {}
        for o in self._req("GET", "/v2/orders", params={"status": "closed", "side": "buy",
                                                          "limit": 500, "direction": "desc"}):
            if o.get("filled_at"):
                t = o["symbol"].replace(".", "-")
                out.setdefault(t, pd.Timestamp(o["filled_at"]).tz_convert("America/New_York").date())
        return out

    def iex_quotes(self, tickers):
        """Top-of-book bid/ask size from the free IEX feed (about 2% of US volume:
        a hint, not the real order book)."""
        if not tickers:
            return {}
        try:
            r = self.s.get(DATA_URL + "/v2/stocks/quotes/latest", timeout=15, params={
                "symbols": ",".join(t.replace("-", ".") for t in tickers), "feed": "iex"})
            r.raise_for_status()
            return {k.replace(".", "-"): q for k, q in r.json().get("quotes", {}).items()}
        except Exception as e:  # informational only, never block trading on it
            print(f"(IEX quotes unavailable: {e})")
            return {}


def plan(held, entries, px, spy, sleeve_equity, cash, vetoes=None):
    """Pure decision step (no broker calls), so it can be tested offline.
    held: {ticker: {"qty", "entry"}}, entries: {ticker: entry date}
    Returns (sells, buys, notes): sells [(ticker, qty, limit, why)], buys [(ticker, qty, limit, info)]"""
    vetoes = vetoes or {}
    ind = indicators(px, spy)
    state = trend_state(ind)
    sig = entry_signals(ind, state)
    last = ind["close"].index[-1]
    c, atr = ind["close"].iloc[-1], ind["atr"].iloc[-1]
    notes, sells, buys = [], [], []

    spy_ok = bool(ind["spy_ok"].iloc[-1])
    notes.append(f"Market filter: SPY {'above' if spy_ok else 'BELOW'} its 200-day average"
                 + ("" if spy_ok else ", so no new buys."))

    for t, h in held.items():
        if t not in c.index or not np.isfinite(c[t]):
            notes.append(f"No price data for {t}; holding.")
            continue
        d0 = pd.Timestamp(entries.get(t, last.date()))
        since = ind["close"][t].loc[d0:]
        days = max(len(since) - 1, 0)
        before = ind["atr"][t].loc[:d0 - pd.Timedelta(days=1)].dropna()
        entry_atr = before.iloc[-1] if len(before) else atr[t]
        high = max(float(since.max()) if len(since) else c[t], h["entry"])
        why = exit_reason("trend", c[t], h["entry"], entry_atr, high, atr[t],
                          ind["sma_slow"][t].iloc[-1], ind["quick_sma"][t].iloc[-1], days)
        stop = max(h["entry"] - RULES["stop_atr"] * entry_atr, high - RULES["trail_atr"] * atr[t])
        st = STATE_NAME.get(state[t].iloc[-1], "?")
        if why:
            sells.append((t, h["qty"], c[t] * (1 - RULES["exit_limit_buffer"]), why))
        else:
            notes.append(f"Hold {t}: {c[t]:.2f}, {st} trend, stop now {stop:.2f} "
                         f"({(c[t] / h['entry'] - 1) * 100:+.1f}% since entry)")

    free = RULES["max_positions"] - (len(held) - len(sells))
    cands = [t for t in sig.columns[sig.iloc[-1].values] if t not in held]
    cands.sort(key=lambda t: -ind["strength"][t].iloc[-1])
    for t in cands:
        if free <= 0:
            break
        if t in vetoes:
            notes.append(f"Skipped {t}: fresh bad news ({vetoes[t]})")
            continue
        limit = c[t] * (1 + RULES["entry_limit_buffer"])
        amount = min(position_size(sleeve_equity, c[t], atr[t]), cash)
        qty = int(amount // limit)
        if qty < 1:
            notes.append(f"Skipped {t}: not enough cash for one share.")
            continue
        stop = c[t] - RULES["stop_atr"] * atr[t]
        risk = qty * (limit - stop)
        info = (f"{STATE_NAME[BULL]} trend, 2-day RSI {ind['rsi'][t].iloc[-1]:.0f}, "
                f"6-month return {ind['strength'][t].iloc[-1] * 100:+.0f}%, "
                f"volume {ind['rvol'][t].iloc[-1]:.1f}x normal; first stop {stop:.2f} "
                f"(max loss about ${risk:,.0f})")
        if BACKTEST_AVG_TRADE is not None:
            info += f"; expect about ${qty * limit * BACKTEST_AVG_TRADE / 2:+,.2f} on average (backtest, halved)"
        buys.append((t, qty, limit, info))
        cash -= qty * limit
        free -= 1
    return sells, buys, notes


def run(broker, today, dry_run=False, data=None, news_fn=None):
    if not broker.is_trading_day(today):
        print("Market closed today, nothing to do.")
        return [], []
    acct = broker.account()
    if acct.get("trading_blocked") or acct.get("account_blocked"):
        notify(["Account is blocked by the broker. No trades placed."], f"{TITLE}: problem")
        return [], []
    equity, cash = float(acct["equity"]), float(acct["cash"])
    cap = os.environ.get("SWING_CAPITAL")
    sleeve = min(equity, float(cap)) if cap else equity
    held = broker.positions()
    held_value = sum(h["qty"] * h["entry"] for h in held.values())
    cash = max(0.0, min(cash, sleeve - held_value))

    if data is None:
        from strategy import sp500_constituents
        universe = sorted(set(sp500_constituents()) | set(held))
        start = (pd.Timestamp(today) - pd.DateOffset(months=14)).strftime("%Y-%m-%d")
        px = download_ohlcv(universe + ["SPY"], start)
        spy = px["close"]["SPY"].ffill()
        px = {k: v.drop(columns="SPY") for k, v in px.items()}
        px["close"] = px["close"].ffill(limit=3)
    else:
        px, spy = data

    vetoes, news_alerts = {}, []
    if news_fn is not None:
        c = px["close"]
        rets = (c.iloc[-1] / c.iloc[-2] - 1).to_dict()
        atr_pct = (indicators(px, spy)["atr"].iloc[-1] / c.iloc[-1]).to_dict()
        pre_sells, pre_buys, _ = plan(held, broker.entry_dates(), px, spy, sleeve, cash)
        watch = sorted(set(held) | {b[0] for b in pre_buys})
        vetoes, news_alerts = news_fn(watch, set(c.columns), rets, atr_pct)

    sells, buys, notes = plan(held, broker.entry_dates(), px, spy, sleeve, cash, vetoes)
    quotes = broker.iex_quotes([b[0] for b in buys])

    if not dry_run:
        broker.cancel_open_orders()
    actions = []
    for t, qty, limit, why in sells:
        actions.append(f"SELL {qty:g} {t} limit {limit:.2f} ({why})")
        if not dry_run:
            broker.limit_order(t, qty, "sell", limit)
    for t, qty, limit, info in buys:
        q = quotes.get(t)
        book = f"; IEX quote bid size {q.get('bs')} / ask size {q.get('as')}" if q else ""
        actions.append(f"BUY {qty} {t} limit {limit:.2f} (~${qty * limit:,.0f}) - {info}{book}")
        if not dry_run:
            broker.limit_order(t, qty, "buy", limit)

    header = [f"{today}  sleeve ${sleeve:,.0f}  cash ${cash:,.0f}  {len(held)} held"
              + ("  [DRY RUN, no orders]" if dry_run else "")]
    notify(header + notes + (actions or ["No trades today."]) + news_alerts,
           f"{TITLE}: trades" if actions else TITLE)
    return sells, buys


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args()

    key, secret = os.environ.get("ALPACA_SWING_KEY_ID"), os.environ.get("ALPACA_SWING_SECRET_KEY")
    if not key or not secret:
        sys.exit("Missing ALPACA_SWING_KEY_ID / ALPACA_SWING_SECRET_KEY (a second Alpaca paper account).")
    if key == os.environ.get("ALPACA_KEY_ID"):
        sys.exit("The swing sleeve must use a different Alpaca paper account than the monthly robot, "
                 "otherwise the monthly rebalance would sell its positions.")
    broker = SwingAlpaca(key, secret, live=False)

    news_fn = None
    if os.environ.get("ANTHROPIC_API_KEY"):
        import news

        def news_fn(tickers, universe, rets, atr_pct):
            vetoes, alerts, rows = news.analyse(tickers, universe, rets, atr_pct)
            news.append_log(rows, os.path.join(HERE, "news_log.jsonl"))
            return vetoes, alerts

    try:
        run(broker, date.today(), a.dry_run, news_fn=news_fn)
    except Exception as e:
        notify([f"The run failed and placed no further orders: {e}"], f"{TITLE}: error")
        raise


if __name__ == "__main__":
    main()
