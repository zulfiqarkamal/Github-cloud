"""Backtest of the Swing sleeve on daily bars.

Signals are computed on each close; buys and sells fill at the NEXT day's open
(that is what the robot's after-hours / pre-market limit orders approximate),
with 0.1% cost per side.

Three variants are compared so we can see which part of the idea carries the edge:
  trend   dip entry + trailing-stop / trend-over exit   (the proposed rules)
  quick   dip entry + sell on the bounce (close > 5-day average)
  nodip   same stocks and exits as "trend", but buy without waiting for a dip
          (control: does the dip timing add anything?)

It also splits trades by the free "order flow" proxies at entry (relative volume
and where the close sat in the day's range) to test whether they predict anything.

    python backtest.py                  # S&P 500, 2010 -> today
    python backtest.py --start 2015-01-01 --tickers AAPL MSFT NVDA

Caveat: the universe is TODAY's S&P 500 members, which flatters past results
(survivorship bias). Treat the numbers as an upper bound.
"""
import argparse
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "quality-momentum"))
from rules import (RULES, download_ohlcv, entry_signals, exit_reason, indicators,  # noqa: E402
                   position_size, trend_state)


def simulate(px, spy, style="trend", entry="dip", start="2010-01-01"):
    ind = indicators(px, spy)
    state = trend_state(ind)
    sig = entry_signals(ind, state, rsi_max=101.0 if entry == "nodip" else None)

    dates = px["close"].index
    tick = list(px["close"].columns)
    O, C = px["open"].values, ind["close"].values
    A, S200, Q = ind["atr"].values, ind["sma_slow"].values, ind["quick_sma"].values
    STR, RV, CL = ind["strength"].values, ind["rvol"].values, ind["close_location"].values
    SIG = sig.values
    cost = RULES["cost_per_side"]

    cash, pos = 1.0, {}             # pos[j] = dict(shares, entry, atr, high, day, rvol, clv)
    pend_buy, pend_sell = [], {}    # filled at next open
    equity, trades = [], []
    t0 = dates.searchsorted(pd.Timestamp(start))

    for t in range(t0, len(dates)):
        # 1) Fills at today's open.
        for j, why in pend_sell.items():
            p = pos.pop(j)
            px_out = O[t, j] if np.isfinite(O[t, j]) else C[t - 1, j]
            cash += p["shares"] * px_out * (1 - cost)
            trades.append({"ticker": tick[j], "entry_date": dates[p["day"]], "exit_date": dates[t],
                           "ret": px_out * (1 - cost) / (p["entry"] * (1 + cost)) - 1,
                           "days": t - p["day"], "why": why, "rvol": p["rvol"], "clv": p["clv"]})
        pend_sell = {}
        for j, amount in pend_buy:
            if j in pos or not np.isfinite(O[t, j]) or O[t, j] <= 0:
                continue
            amount = min(amount, cash)
            if amount < 1e-6:
                break
            pos[j] = {"shares": amount * (1 - cost) / O[t, j], "entry": O[t, j], "atr": A[t - 1, j],
                      "high": O[t, j], "day": t, "rvol": RV[t - 1, j], "clv": CL[t - 1, j]}
            cash -= amount
        pend_buy = []

        # 2) Mark to market on the close.
        value = cash + sum(p["shares"] * (C[t, j] if np.isfinite(C[t, j]) else p["entry"])
                           for j, p in pos.items())
        equity.append(value)

        # 3) Exit decisions for tomorrow's open.
        for j, p in pos.items():
            if not np.isfinite(C[t, j]):
                continue
            p["high"] = max(p["high"], C[t, j])
            why = exit_reason(style, C[t, j], p["entry"], p["atr"], p["high"], A[t, j],
                              S200[t, j], Q[t, j], t - p["day"])
            if why:
                pend_sell[j] = why

        # 4) New entries for tomorrow's open: strongest stocks first.
        free = RULES["max_positions"] - (len(pos) - len(pend_sell))
        if free > 0:
            cands = [j for j in np.flatnonzero(SIG[t]) if j not in pos]
            cands.sort(key=lambda j: -STR[t, j])
            for j in cands[:free]:
                pend_buy.append((j, position_size(value, C[t, j], A[t, j])))

    curve = pd.Series(equity, index=dates[t0:])
    return curve, pd.DataFrame(trades)


def curve_stats(curve):
    rets = curve.pct_change().dropna()
    years = (curve.index[-1] - curve.index[0]).days / 365.25
    cagr = (curve.iloc[-1] / curve.iloc[0]) ** (1 / years) - 1
    dd = (curve / curve.cummax() - 1).min()
    sharpe = rets.mean() / rets.std() * np.sqrt(252) if rets.std() > 0 else np.nan
    return {"CAGR": cagr, "MaxDD": dd, "Sharpe": sharpe}


def trade_stats(tr):
    if tr.empty:
        return {"trades": 0}
    win = tr["ret"] > 0
    avg_win = tr.loc[win, "ret"].mean() if win.any() else 0.0
    avg_loss = tr.loc[~win, "ret"].mean() if (~win).any() else 0.0
    return {"trades": len(tr), "win_rate": win.mean(), "avg_win": avg_win, "avg_loss": avg_loss,
            "expectancy": tr["ret"].mean(), "median_days": tr["days"].median(),
            "worst_5pct": tr["ret"].quantile(0.05),
            "t_stat": tr["ret"].mean() / tr["ret"].std() * np.sqrt(len(tr)) if len(tr) > 1 else np.nan}


def pct(x):
    return "n/a" if x is None or not np.isfinite(x) else f"{x * 100:.1f}%"


def report(results, spy, periods):
    out = ["## Swing sleeve backtest", "",
           "Fills at next open, 0.1% cost per side. Universe = today's S&P 500 (survivorship bias: "
           "results are flattered).", ""]
    for name, (a, b) in periods.items():
        out += [f"### {name}", "",
                "| Variant | CAGR | Max drawdown | Sharpe | Trades | Win rate | Avg win | Avg loss | "
                "Avg per trade | t-stat | Per $200 trade |",
                "|---|---|---|---|---|---|---|---|---|---|---|"]
        s = curve_stats(spy.loc[a:b])
        out.append(f"| SPY buy & hold | {pct(s['CAGR'])} | {pct(s['MaxDD'])} | {s['Sharpe']:.2f} "
                   "| | | | | | | |")
        for v, (curve, tr) in results.items():
            c = curve_stats(curve.loc[a:b])
            sub = tr[(tr["entry_date"] >= a) & (tr["entry_date"] <= b)] if not tr.empty else tr
            t = trade_stats(sub)
            if not t["trades"]:
                out.append(f"| {v} | {pct(c['CAGR'])} | {pct(c['MaxDD'])} | {c['Sharpe']:.2f} | 0 | | | | | | |")
                continue
            out.append(f"| {v} | {pct(c['CAGR'])} | {pct(c['MaxDD'])} | {c['Sharpe']:.2f} | {t['trades']} "
                       f"| {pct(t['win_rate'])} | {pct(t['avg_win'])} | {pct(t['avg_loss'])} "
                       f"| {pct(t['expectancy'])} | {t['t_stat']:.1f} | ${t['expectancy'] * 200:+.2f} |")
        out.append("")

    tr = results["trend"][1]
    if not tr.empty:
        out += ["### Do the free order-flow proxies predict anything? (trend variant, all trades)", "",
                "| Bucket at entry | Trades | Win rate | Avg per trade |", "|---|---|---|---|"]
        for label, col in (("Relative volume", "rvol"), ("Close location in day's range", "clv")):
            q = pd.qcut(tr[col].rank(method="first"), 3, labels=["low", "mid", "high"])
            for b in ("low", "mid", "high"):
                g = tr[q == b]
                out.append(f"| {label}: {b} third | {len(g)} | {pct((g['ret'] > 0).mean())} "
                           f"| {pct(g['ret'].mean())} |")
        out += ["", "### Why trades closed (trend variant)", "", "| Exit reason | Trades | Avg |", "|---|---|---|"]
        for why, g in tr.groupby("why"):
            out.append(f"| {why} | {len(g)} | {pct(g['ret'].mean())} |")
    return "\n".join(out)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--start", default="2010-01-01")
    p.add_argument("--tickers", nargs="*")
    a = p.parse_args()

    if a.tickers:
        tickers = a.tickers
    else:
        from strategy import sp500_constituents
        tickers = sorted(sp500_constituents())
    warm = (pd.Timestamp(a.start) - pd.DateOffset(years=1)).strftime("%Y-%m-%d")
    print(f"Downloading {len(tickers)} tickers from {warm}...", flush=True)
    px = download_ohlcv(tickers + ["SPY"], warm)
    spy = px["close"]["SPY"].ffill()
    px = {k: v.drop(columns="SPY") for k, v in px.items()}
    px["close"] = px["close"].ffill(limit=3)

    results = {}
    for name, style, entry in (("trend", "trend", "dip"), ("quick", "quick", "dip"),
                               ("nodip", "trend", "nodip")):
        print(f"Simulating {name}...", flush=True)
        results[name] = simulate(px, spy, style, entry, a.start)

    spy_curve = spy.loc[a.start:]
    last = str(spy_curve.index[-1].date())
    periods = {f"Full period {a.start[:4]}-{last[:4]}": (a.start, last),
               "First half 2010-2017": ("2010-01-01", "2017-12-31"),
               f"Second half 2018-{last[:4]}": ("2018-01-01", last)}
    text = report(results, spy_curve, periods)
    print(text)
    os.makedirs("results", exist_ok=True)
    with open("results/swing_backtest.md", "w") as f:
        f.write(text + "\n")
    results["trend"][1].to_csv("results/swing_trades.csv", index=False)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write(text + "\n")


if __name__ == "__main__":
    main()
