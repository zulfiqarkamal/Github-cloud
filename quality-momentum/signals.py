"""Monthly Quality Momentum signals: what to buy, keep and sell this month.

    python signals.py                      # dry run: prints the list, writes orders.json
    python signals.py --capital 100000     # size orders for a $100k paper account
    python signals.py --ibkr-paper         # also send orders to IBKR *paper* (TWS/Gateway on port 7497)

Run it on the last trading day of the month, after the US close.
Current holdings are read from holdings.json (create it as {} to start).
Orders are only ever sent to an IBKR paper account: the script refuses the
live port (7496/4001) and any account whose id does not start with "DU".
"""
import argparse
import json
import os
from datetime import date

import pandas as pd

from strategy import (RULES, download_prices, indicators, pick_portfolio,
                      rank_candidates, sp500_constituents)

PAPER_PORTS = {7497, 4002}


def quality_ok(ticker, sector):
    """Rules 3-5: profitable, growing revenue, debt/equity below 2."""
    import yfinance as yf

    try:
        info = yf.Ticker(ticker).info
    except Exception:
        return False, "no data"
    ni = info.get("netIncomeToCommon")
    growth = info.get("revenueGrowth")
    de = info.get("debtToEquity")  # Yahoo reports this in percent
    if ni is None or ni <= 0:
        return False, "not profitable"
    if growth is None or growth <= 0:
        return False, "revenue not growing"
    if sector != "Financials" and de is not None and de / 100 > RULES["max_debt_to_equity"]:
        return False, f"debt/equity {de / 100:.1f}"
    return True, ""


def build_signals(holdings, capital):
    sectors = sp500_constituents()
    start = (pd.Timestamp.today() - pd.DateOffset(months=15)).strftime("%Y-%m-%d")
    prices = download_prices(list(sectors) + ["SPY"], start).ffill()
    spy = prices.pop("SPY")
    sma, mom = indicators(prices)
    day = prices.index[-1]
    spy_sma = spy.rolling(RULES["sma_days"]).mean().iloc[-1]
    close = prices.loc[day]

    report = {"date": str(day.date()), "spy": round(float(spy.iloc[-1]), 2),
              "spy_sma200": round(float(spy_sma), 2), "notes": []}

    # Rules 9-10: crash brake.
    if spy.iloc[-1] < spy_sma:
        report["brake"] = True
        report["notes"].append("SPY is below its 200-day average: sell everything, hold cash.")
        targets = []
    else:
        report["brake"] = False
        ranked = rank_candidates(close, sma.loc[day], mom.loc[day])
        held = list(holdings)
        # Quality filter on the top of the list only (enough to fill 10 slots).
        passed, rejected = [], {}
        for t in ranked[:RULES["keep_rank"] * 2]:
            if t in held:
                passed.append(t)
                continue
            ok, why = quality_ok(t, sectors.get(t))
            if ok:
                passed.append(t)
            else:
                rejected[t] = why
        report["rejected_by_quality"] = rejected
        targets = pick_portfolio(held, passed, close, sma.loc[day], sectors)

    # Rule 12: stops on current holdings.
    for t, h in holdings.items():
        price = close.get(t)
        if price is not None and price <= h["entry"] * (1 - RULES["stop_loss"]):
            report["notes"].append(f"{t} hit the 20% stop.")
            if t in targets:
                targets.remove(t)

    sells = [t for t in holdings if t not in targets]
    buys = [t for t in targets if t not in holdings]
    slot = capital / RULES["top_n"]
    report["orders"] = (
        [{"action": "SELL", "ticker": t, "qty": holdings[t]["qty"]} for t in sells] +
        [{"action": "BUY", "ticker": t, "qty": int(slot // close[t]),
          "price": round(float(close[t]), 2), "sector": sectors.get(t),
          "momentum_12_1": round(float(mom.loc[day, t]), 3)} for t in buys]
    )
    report["keep"] = [t for t in targets if t in holdings]
    return report


def send_to_ibkr_paper(orders, port, client_id=17):
    if port not in PAPER_PORTS:
        raise SystemExit(f"Refusing port {port}: only paper ports {sorted(PAPER_PORTS)} are allowed.")
    from ib_insync import IB, MarketOrder, Stock

    ib = IB()
    ib.connect("127.0.0.1", port, clientId=client_id)
    accounts = ib.managedAccounts()
    if not accounts or not all(a.startswith("DU") for a in accounts):
        ib.disconnect()
        raise SystemExit(f"Refusing: account {accounts} is not an IBKR paper account (DU...).")
    for o in orders:
        if o["qty"] <= 0:
            continue
        contract = Stock(o["ticker"].replace("-", " "), "SMART", "USD")
        ib.qualifyContracts(contract)
        trade = ib.placeOrder(contract, MarketOrder(o["action"], o["qty"]))
        print(f"Sent {o['action']} {o['qty']} {o['ticker']} -> {trade.orderStatus.status}")
    ib.disconnect()


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--holdings", default="holdings.json")
    p.add_argument("--capital", type=float, default=100_000)
    p.add_argument("--ibkr-paper", action="store_true")
    p.add_argument("--port", type=int, default=7497)
    a = p.parse_args()

    holdings = {}
    if os.path.exists(a.holdings):
        with open(a.holdings) as f:
            holdings = json.load(f)  # {"AAPL": {"qty": 10, "entry": 190.5}, ...}

    report = build_signals(holdings, a.capital)
    with open("orders.json", "w") as f:
        json.dump(report, f, indent=2)

    print(f"\n{report['date']}  SPY {report['spy']}  200-day {report['spy_sma200']}"
          f"  crash brake: {'ON' if report['brake'] else 'off'}")
    for n in report["notes"]:
        print("  ! " + n)
    print("Keep:", ", ".join(report["keep"]) or "-")
    for o in report["orders"]:
        print(f"  {o['action']:4} {o['qty']:>6} {o['ticker']}")
    print("\nWritten to orders.json. After the orders fill, update holdings.json.")

    if a.ibkr_paper:
        send_to_ibkr_paper(report["orders"], a.port)


if __name__ == "__main__":
    main()
