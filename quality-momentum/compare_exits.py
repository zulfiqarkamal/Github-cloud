"""Monthly robot vs daily-trend exits: same entries, different selling.

    python compare_exits.py                 # 2005 -> today, writes results/compare_exits.*
    python compare_exits.py --synthetic     # fake prices, only checks the code runs

Every variant buys exactly like the monthly robot (top 10 by 12-1 momentum,
above the 200-day average, max 3 per sector, only while SPY is above its
200-day average) and keeps the 20% stop. They differ only in when they sell
and how fast they buy back:

  A  Monthly (current robot): trend and brake checked on the last trading day
  B  + sell a stock the day it closes below its own 200-day average
  C  + sell a stock the day it closes below its own 50-day average (shorter trend)
  D  B + sell everything the day SPY closes below its 200-day average
  E  D + refill empty slots every day (fully daily robot)

The sector-ETF rows repeat A, B and E on 9 sector ETFs, which has no
survivorship bias, as the honest check.
"""
import argparse
import os

import pandas as pd

from backtest import load_data, simulate, stats
from strategy import RULES

STOCK_VARIANTS = {
    "A Monthly (current)": {},
    "B Daily 200-day exit": {"trend_exit": 200},
    "C Daily 50-day exit": {"trend_exit": 50},
    "D 200-day exit + daily SPY brake": {"trend_exit": 200, "daily_brake": True},
    "E Fully daily (D + daily refill)": {"trend_exit": 200, "daily_brake": True, "daily_refill": True},
}
ETF_VARIANTS = {k: v for k, v in STOCK_VARIANTS.items() if k[0] in "ABE"}


def row(curve, info, start):
    curve = curve.loc[start:]
    curve = curve / curve.iloc[0]
    s, _ = stats(curve)
    mid = curve.index[len(curve) // 2]
    first, second = curve.loc[:mid], curve.loc[mid:]
    years = s["Years"]
    return {
        "CAGR": s["CAGR"],
        "Max drawdown": s["Max drawdown"],
        "Sharpe": s["Sharpe (rf=0)"],
        "CAGR 1st half": stats(first / first.iloc[0])[0]["CAGR"],
        "CAGR 2nd half": stats(second / second.iloc[0])[0]["CAGR"],
        "Win rate": info["win_rate"],
        "Avg trade": info["avg_trade"],
        "Round trips": info["round_trips"],
        "Trades/yr": info["trades"] / years,
        "Avg days held": info["avg_days_held"],
        "Turnover/yr": info["turnover_per_year"],
        "Cost drag/yr": info["turnover_per_year"] * RULES["cost_per_trade"],
    }


def fmt(table):
    shown = table.copy()
    for col in ["CAGR", "Max drawdown", "CAGR 1st half", "CAGR 2nd half", "Win rate",
                "Avg trade", "Cost drag/yr"]:
        shown[col] = shown[col].map(lambda v: f"{v:.1%}")
    shown["Sharpe"] = shown["Sharpe"].map(lambda v: f"{v:.2f}")
    shown["Turnover/yr"] = shown["Turnover/yr"].map(lambda v: f"{v:.1f}x")
    for col in ["Round trips", "Trades/yr", "Avg days held"]:
        shown[col] = shown[col].map(lambda v: f"{v:.0f}")
    return shown


def main(start, out_dir, synthetic):
    stocks, etfs, spy, sectors = load_data(start, synthetic)
    tables = {}
    for label, data, variants, kw in [
        ("stocks", stocks, STOCK_VARIANTS,
         dict(sectors=sectors, top_n=RULES["top_n"], keep_rank=RULES["keep_rank"],
              max_per_sector=RULES["max_per_sector"], stop_loss=RULES["stop_loss"])),
        ("sector ETFs", etfs, ETF_VARIANTS, dict(top_n=3, keep_rank=3, stop_loss=0)),
    ]:
        rows = {}
        for name, opts in variants.items():
            curve, info = simulate(data, spy, **kw, **opts)
            rows[name] = row(curve, info, start)
            print(f"done: {label} / {name}", flush=True)
        tables[label] = pd.DataFrame(rows).T

    spy_curve = spy.loc[start:] / spy.loc[start:].iloc[0]
    spy_stats, _ = stats(spy_curve)

    os.makedirs(out_dir, exist_ok=True)
    md = [f"# Monthly vs daily-trend exits ({start} to {spy_curve.index[-1]:%Y-%m-%d})", "",
          f"SPY buy-and-hold: CAGR {spy_stats['CAGR']:.1%}, max drawdown {spy_stats['Max drawdown']:.1%},"
          f" Sharpe {spy_stats['Sharpe (rf=0)']:.2f}", ""]
    for label, table in tables.items():
        table.to_csv(os.path.join(out_dir, f"compare_exits_{label.replace(' ', '_')}.csv"))
        md += [f"## {label}", "", fmt(table).to_markdown(), ""]
    md.append("Costs: 0.1% per trade, already inside every return. Trades fill at the signal day's close.")
    text = "\n".join(md)
    with open(os.path.join(out_dir, "compare_exits.md"), "w") as f:
        f.write(text + "\n")
    print("\n" + text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as f:
            f.write(text + "\n")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--start", default="2005-01-01")
    p.add_argument("--out", default="results")
    p.add_argument("--synthetic", action="store_true", help="fake data, to test the code offline")
    a = p.parse_args()
    main(a.start, a.out, a.synthetic)
