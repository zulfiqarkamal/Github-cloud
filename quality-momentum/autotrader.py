"""Fully automatic Quality Momentum trading on an Alpaca PAPER account.

Runs once every weekday after the US close (see .github/workflows/autotrader.yml):
  * every day: sell any holding 20% below its entry price (rule 12)
  * last trading day of the month: crash brake + full rebalance (rules 1-11)
  * every action is sent to your phone (ntfy) and the GitHub run summary

Orders are placed after the close, so they fill at the next market open.

Settings come from environment variables (GitHub secrets):
  ALPACA_KEY_ID, ALPACA_SECRET_KEY   Alpaca paper API keys
  NTFY_TOPIC                         your private ntfy topic name (phone alerts)
  STRATEGY_FRACTION                  share of the account the strategy may use (default 1.0)
  LIVE_TRADING                       leave unset. Only "yes-real-money" switches to a live account.

    python autotrader.py               # normal run
    python autotrader.py --dry-run     # decide and notify, but place no orders
    python autotrader.py --force-monthly --dry-run   # preview this month's rebalance today
"""
import argparse
import os
import sys
from datetime import date

import pandas as pd
import requests

from signals import quality_ok
from strategy import (RULES, download_prices, indicators, pick_portfolio,
                      rank_candidates, sp500_constituents)

PAPER_URL = "https://paper-api.alpaca.markets"
LIVE_URL = "https://api.alpaca.markets"


class Alpaca:
    """The handful of Alpaca REST calls we need."""

    def __init__(self, key, secret, live=False):
        self.base = LIVE_URL if live else PAPER_URL
        self.s = requests.Session()
        self.s.headers.update({"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret})

    def _req(self, method, path, **kw):
        r = self.s.request(method, self.base + path, timeout=30, **kw)
        if r.status_code >= 400:
            raise RuntimeError(f"Alpaca {method} {path} failed: {r.status_code} {r.text[:200]}")
        return r.json() if r.text else {}

    def account(self):
        return self._req("GET", "/v2/account")

    def positions(self):
        """{ticker: {"qty": float, "entry": float}}"""
        return {p["symbol"].replace(".", "-"): {"qty": float(p["qty"]), "entry": float(p["avg_entry_price"])}
                for p in self._req("GET", "/v2/positions")}

    def open_order_symbols(self):
        return {o["symbol"].replace(".", "-") for o in self._req("GET", "/v2/orders", params={"status": "open"})}

    def is_last_trading_day_of_month(self, today):
        start = today.replace(day=1).isoformat()
        end = (pd.Timestamp(today) + pd.offsets.MonthEnd(0)).date().isoformat()
        days = [d["date"] for d in self._req("GET", "/v2/calendar", params={"start": start, "end": end})]
        return bool(days) and days[-1] == today.isoformat()

    def is_trading_day(self, today):
        days = self._req("GET", "/v2/calendar", params={"start": today.isoformat(), "end": today.isoformat()})
        return bool(days)

    def sell_all(self, ticker):
        return self._req("DELETE", f"/v2/positions/{ticker.replace('-', '.')}")

    def buy(self, ticker, qty):
        return self._req("POST", "/v2/orders", json={
            "symbol": ticker.replace("-", "."), "qty": str(int(qty)), "side": "buy",
            "type": "market", "time_in_force": "day"})


def notify(lines, title="Quality Momentum"):
    """Phone push via ntfy.sh plus the GitHub Actions run summary."""
    text = "\n".join(lines)
    print(f"\n== {title} ==\n{text}")
    topic = os.environ.get("NTFY_TOPIC")
    if topic:
        try:
            requests.post(f"https://ntfy.sh/{topic}", data=text.encode(),
                          headers={"Title": title}, timeout=15)
        except requests.RequestException as e:
            print(f"(notification failed: {e})")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as f:
            f.write(f"### {title}\n\n" + "\n".join(f"- {l}" for l in lines) + "\n")


def plan_monthly(held, close, sma, mom, spy, spy_sma, sectors, quality=None):
    """Return (targets, notes) for the month-end rebalance."""
    quality = quality or quality_ok
    if spy < spy_sma:
        return [], [f"Crash brake ON: SPY {spy:.2f} is below its 200-day average {spy_sma:.2f}. Selling everything."]
    ranked = rank_candidates(close, sma, mom)
    passed = []
    for t in ranked[:RULES["keep_rank"] * 2]:
        if t in held or quality(t, sectors.get(t))[0]:
            passed.append(t)
    targets = pick_portfolio(list(held), passed, close, sma, sectors)
    return targets, [f"Crash brake off: SPY {spy:.2f} above 200-day {spy_sma:.2f}."]


def run(broker, today, dry_run=False, force_monthly=False, fraction=1.0, data=None):
    if not broker.is_trading_day(today):
        print("Market closed today, nothing to do.")
        return []

    acct = broker.account()
    if acct.get("trading_blocked") or acct.get("account_blocked"):
        notify(["Account is blocked by the broker. No trades placed."], "Quality Momentum: problem")
        return []
    equity = float(acct["equity"])
    cash = float(acct["cash"])
    held = broker.positions()
    pending = broker.open_order_symbols()
    monthly = force_monthly or broker.is_last_trading_day_of_month(today)

    # Prices and indicators.
    if data is None:
        sectors = sp500_constituents()
        start = (pd.Timestamp(today) - pd.DateOffset(months=15)).strftime("%Y-%m-%d")
        prices = download_prices(sorted(set(sectors) | set(held) | {"SPY"}), start).ffill()
    else:
        prices, sectors = data
    spy_series = prices.pop("SPY")
    sma_all, mom_all = indicators(prices)
    close, sma, mom = prices.iloc[-1], sma_all.iloc[-1], mom_all.iloc[-1]
    spy, spy_sma = spy_series.iloc[-1], spy_series.rolling(RULES["sma_days"]).mean().iloc[-1]

    actions, notes = [], []
    to_sell = set()

    # Rule 12: daily 20% stop.
    for t, h in held.items():
        price = close.get(t)
        if price is not None and price <= h["entry"] * (1 - RULES["stop_loss"]):
            to_sell.add(t)
            notes.append(f"Stop: {t} at {price:.2f} is 20%+ below entry {h['entry']:.2f}.")

    targets = None
    if monthly:
        targets, n = plan_monthly(held, close, sma, mom, spy, spy_sma, sectors)
        notes += n
        targets = [t for t in targets if t not in to_sell]
        to_sell |= {t for t in held if t not in targets}
    elif spy < spy_sma:
        notes.append(f"Heads-up: SPY is below its 200-day average. The brake is checked at month end.")

    # Sells first.
    for t in sorted(to_sell):
        if t in pending:
            notes.append(f"Skipped {t}: an order is already waiting.")
            continue
        actions.append(f"SELL all {held[t]['qty']:g} {t}")
        cash += held[t]["qty"] * float(close.get(t, 0))
        if not dry_run:
            broker.sell_all(t)

    # Buys: 10% of the strategy's share of equity each, never more than cash (no leverage).
    if targets:
        slot = equity * fraction / RULES["top_n"]
        budget = min(cash, equity * fraction - sum(
            h["qty"] * float(close.get(t, 0)) for t, h in held.items() if t not in to_sell))
        for t in [t for t in targets if t not in held and t not in pending]:
            price = float(close[t])
            qty = int(min(slot, budget) // price)
            if qty * price < slot / 2:  # skip leftover crumbs smaller than half a slot
                notes.append(f"Not enough cash left to buy {t}.")
                continue
            budget -= qty * price
            actions.append(f"BUY {qty} {t} (~${qty * price:,.0f}, {sectors.get(t, '')})")
            if not dry_run:
                broker.buy(t, qty)

    header = [f"{today}  equity ${equity:,.0f}  {'MONTHLY rebalance' if monthly else 'daily check'}"
              + ("  [DRY RUN, no orders]" if dry_run else "")]
    notify(header + notes + (actions or ["No trades today."]),
           "Quality Momentum: trades" if actions else "Quality Momentum")
    return actions


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--force-monthly", action="store_true")
    a = p.parse_args()

    live = os.environ.get("LIVE_TRADING") == "yes-real-money"
    key, secret = os.environ.get("ALPACA_KEY_ID"), os.environ.get("ALPACA_SECRET_KEY")
    if not key or not secret:
        sys.exit("Missing ALPACA_KEY_ID / ALPACA_SECRET_KEY.")
    if live:
        notify(["LIVE_TRADING is on: this run uses REAL money."], "Quality Momentum: LIVE")
    fraction = min(max(float(os.environ.get("STRATEGY_FRACTION", "1.0")), 0.0), 1.0)
    broker = Alpaca(key, secret, live=live)
    try:
        run(broker, date.today(), a.dry_run, a.force_monthly, fraction)
    except Exception as e:
        notify([f"The run failed and placed no further orders: {e}"], "Quality Momentum: error")
        raise


if __name__ == "__main__":
    main()
