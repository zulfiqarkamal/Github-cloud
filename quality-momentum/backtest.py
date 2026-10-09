"""Backtest the Quality Momentum strategy against simple alternatives.

    pip install -r requirements.txt
    python backtest.py                 # 2005 -> today, writes results/
    python backtest.py --start 2010-01-01

Strategies compared:
  1. SPY buy-and-hold (the benchmark to beat)
  2. SPY with the 200-day crash brake only
  3. Sector-ETF momentum: top 3 of 9 sector ETFs + crash brake (no survivorship bias)
  4. Quality Momentum on S&P 500 stocks: top 10 + crash brake + 20% stop

Caveat: strategy 4 uses *today's* S&P 500 members, so it never holds a company
that later dropped out. That flatters its past results. The fundamental filter
(rules 3-5) is applied in live signals only, because free point-in-time
fundamentals are not available. Treat strategy 3 as the honest check.
"""
import argparse
import os

import numpy as np
import pandas as pd

from strategy import (RULES, SECTOR_ETFS, download_prices, indicators,
                      pick_portfolio, rank_candidates, sp500_constituents)


def month_ends(index):
    s = pd.Series(index, index=index)
    return set(s.groupby([index.year, index.month]).max())


def simulate(prices, spy, sectors=None, top_n=10, keep_rank=30,
             max_per_sector=3, stop_loss=0.20, use_brake=True,
             trend_exit=None, daily_brake=False, daily_refill=False):
    """Daily simulation with monthly decisions on the last trading day.

    The defaults are the current monthly robot. The daily-trend options:
      trend_exit=200    sell a holding the day it closes below its own N-day
                        average, instead of waiting for month end
      daily_brake=True  sell everything the day SPY closes below its 200-day
                        average (normally checked at month end only)
      daily_refill=True fill empty slots every day from the ranking (and buy
                        back the day SPY recovers) instead of at month end
    """
    cost = RULES["cost_per_trade"]
    prices = prices.ffill()  # carry the last price over gaps so sales never see NaN
    sma, mom = indicators(prices)
    exit_sma = prices.rolling(trend_exit, min_periods=trend_exit).mean() if trend_exit else None
    spy_sma = spy.rolling(RULES["sma_days"]).mean()
    rebal = month_ends(prices.index)

    cash, shares, entry = 1.0, {}, {}
    cost_basis, entry_day = {}, {}
    equity, trades, traded_value = [], 0, 0.0  # traded_value: sum of trade size / account value
    value = 1.0
    closed = []  # (return incl. costs, trading days held) per round trip

    def sell(t, price, day_i):
        nonlocal cash, traded_value, trades
        proceeds = shares.pop(t) * price
        entry.pop(t)
        cash += proceeds * (1 - cost)
        traded_value += proceeds / value
        trades += 1
        closed.append((proceeds * (1 - cost) / cost_basis.pop(t) - 1, day_i - entry_day.pop(t)))

    def buy(new, close, day_i):
        nonlocal cash, traded_value, trades, value
        # Equal 1/top_n slice of the account (rule 8).
        value = cash + sum(n * close[t] for t, n in shares.items())
        for t in new:
            spend = min(value / top_n, cash / max(len(new), 1) if cash > 0 else 0)
            if spend <= 0 or np.isnan(close[t]):
                continue
            shares[t] = spend * (1 - cost) / close[t]
            entry[t] = close[t]
            cost_basis[t] = spend
            entry_day[t] = day_i
            cash -= spend
            traded_value += spend / value
            trades += 1

    for day_i, day in enumerate(prices.index):
        close = prices.loc[day]
        value = cash + sum(n * close[t] for t, n in shares.items() if not np.isnan(close[t]))
        spy_ok = not np.isnan(spy_sma.get(day, np.nan))
        brake_on = use_brake and spy_ok and spy[day] < spy_sma[day]

        # Rule 12: emergency stop, checked every day.
        for t in list(shares):
            if stop_loss and close[t] <= entry[t] * (1 - stop_loss):
                sell(t, close[t], day_i)

        # Daily trend exits: the stock's own trend, then the whole market.
        if trend_exit:
            for t in list(shares):
                if close[t] < exit_sma.at[day, t]:
                    sell(t, close[t], day_i)
        if daily_brake and brake_on:
            for t in list(shares):
                sell(t, close[t], day_i)

        if day in rebal and spy_ok:
            if brake_on:
                targets = []
            else:
                ranked = rank_candidates(close, sma.loc[day], mom.loc[day])
                targets = pick_portfolio(list(shares), ranked, close, sma.loc[day],
                                         sectors, top_n, keep_rank, max_per_sector)
            # Sell what is no longer wanted (rules 10, 11).
            for t in [t for t in shares if t not in targets]:
                sell(t, close[t], day_i)
            buy([t for t in targets if t not in shares], close, day_i)
        elif daily_refill and spy_ok and not brake_on and len(shares) < top_n:
            ranked = rank_candidates(close, sma.loc[day], mom.loc[day])
            targets = pick_portfolio(list(shares), ranked, close, sma.loc[day],
                                     sectors, top_n, keep_rank, max_per_sector)
            buy([t for t in targets if t not in shares][:top_n - len(shares)], close, day_i)

        value = cash + sum(n * close[t] for t, n in shares.items() if not np.isnan(close[t]))
        equity.append(value)

    curve = pd.Series(equity, index=prices.index)
    years = len(curve) / 252
    rets = np.array([r for r, _ in closed]) if closed else np.array([np.nan])
    return curve, {
        "trades": trades,
        "turnover_per_year": traded_value / years,
        "round_trips": len(closed),
        "win_rate": float(np.mean(rets > 0)) if closed else np.nan,
        "avg_trade": float(np.mean(rets)) if closed else np.nan,
        "avg_days_held": float(np.mean([d for _, d in closed])) if closed else np.nan,
    }


def stats(curve):
    rets = curve.pct_change().dropna()
    years = len(rets) / 252
    cagr = curve.iloc[-1] ** (1 / years) - 1
    vol = rets.std() * np.sqrt(252)
    dd = (curve / curve.cummax() - 1).min()
    yearly = curve.resample("YE").last().pct_change()
    yearly.iloc[0] = curve.resample("YE").last().iloc[0] / curve.iloc[0] - 1
    return {
        "CAGR": cagr,
        "Volatility": vol,
        "Sharpe (rf=0)": rets.mean() / rets.std() * np.sqrt(252) if rets.std() else np.nan,
        "Max drawdown": dd,
        "Worst year": yearly.min(),
        "Losing years": int((yearly < 0).sum()),
        "Years": round(years, 1),
    }, yearly


def load_data(start, synthetic=False):
    """Stock and sector-ETF closes from 14 months before start (indicator warm-up)."""
    warmup_start = (pd.Timestamp(start) - pd.DateOffset(years=1, months=2)).strftime("%Y-%m-%d")
    if synthetic:
        return synthetic_data(warmup_start)
    sectors = sp500_constituents()
    print(f"Downloading {len(sectors)} S&P 500 stocks + ETFs from {warmup_start}...")
    stocks = download_prices(sectors.keys(), warmup_start)
    etfs = download_prices(SECTOR_ETFS + ["SPY"], warmup_start)
    spy = etfs.pop("SPY")
    return stocks, etfs, spy, sectors


def run(start, out_dir, synthetic=False):
    stocks, etfs, spy, sectors = load_data(start, synthetic)

    def trim(x):
        return x.loc[start:]

    results, curves = {}, {}
    spy_bh = trim(spy) / trim(spy).iloc[0]
    curves["1. SPY buy-and-hold"] = spy_bh

    spy_only = spy.to_frame("SPY")
    c, _ = simulate(spy_only, spy, top_n=1, keep_rank=1, stop_loss=0)
    curves["2. SPY + crash brake"] = trim(c) / trim(c).iloc[0]

    c, info3 = simulate(etfs, spy, top_n=3, keep_rank=3, stop_loss=0)
    curves["3. Sector-ETF momentum"] = trim(c) / trim(c).iloc[0]

    c, info4 = simulate(stocks, spy, sectors=sectors, top_n=RULES["top_n"],
                        keep_rank=RULES["keep_rank"], max_per_sector=RULES["max_per_sector"],
                        stop_loss=RULES["stop_loss"])
    curves["4. Quality Momentum (stocks)"] = trim(c) / trim(c).iloc[0]

    yearly_all = {}
    for name, curve in curves.items():
        results[name], yearly_all[name] = stats(curve)
        mid = curve.index[len(curve) // 2]
        first, second = curve.loc[:mid], curve.loc[mid:]
        results[name]["CAGR 1st half"] = stats(first / first.iloc[0])[0]["CAGR"]
        results[name]["CAGR 2nd half"] = stats(second / second.iloc[0])[0]["CAGR"]

    table = pd.DataFrame(results).T
    yearly = pd.DataFrame(yearly_all)
    yearly.index = yearly.index.year

    os.makedirs(out_dir, exist_ok=True)
    pd.DataFrame(curves).to_csv(os.path.join(out_dir, "equity_curves.csv"))
    yearly.to_csv(os.path.join(out_dir, "yearly_returns.csv"))
    table.to_csv(os.path.join(out_dir, "summary.csv"))

    pct = ["CAGR", "Volatility", "Max drawdown", "Worst year", "CAGR 1st half", "CAGR 2nd half"]
    shown = table.copy()
    for col in pct:
        shown[col] = shown[col].map(lambda v: f"{v:6.1%}")
    shown["Sharpe (rf=0)"] = shown["Sharpe (rf=0)"].map(lambda v: f"{v:.2f}")
    print("\n" + shown.to_string())
    print("\nYearly returns:\n" + yearly.map(lambda v: f"{v:6.1%}").to_string())
    print(f"\nTrades: ETF strategy {info3['trades']}, stock strategy {info4['trades']}")
    print(verdict(table))
    print(f"\nSaved CSVs to {out_dir}/")
    return table


def verdict(table):
    """Stage-1 pass criteria from strategy-plan.md."""
    spy = table.loc["1. SPY buy-and-hold"]
    lines = ["\nPass check (vs SPY):"]
    for name in table.index[1:]:
        row = table.loc[name]
        better_sharpe = row["Sharpe (rf=0)"] > spy["Sharpe (rf=0)"]
        smaller_dd = row["CAGR"] >= spy["CAGR"] - 0.01 and row["Max drawdown"] > spy["Max drawdown"] * 2 / 3
        both_halves = row["CAGR 1st half"] > 0 and row["CAGR 2nd half"] > 0
        ok = (better_sharpe or smaller_dd) and both_halves
        lines.append(f"  {'PASS' if ok else 'FAIL'}  {name}"
                     f"  (Sharpe better: {better_sharpe}, same return with 1/3 smaller drawdown: {smaller_dd},"
                     f" positive in both halves: {both_halves})")
    return "\n".join(lines)


def synthetic_data(start, n_stocks=60, seed=7):
    """Random-walk prices, only for checking that the code runs."""
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(start, pd.Timestamp.today().normalize())
    def walk(n, drift=0.0003, vol=0.015):
        return pd.DataFrame(50 * np.exp(np.cumsum(rng.normal(drift, vol, (len(idx), n)), axis=0)), index=idx)
    stocks = walk(n_stocks, vol=0.02)
    stocks.columns = [f"S{i:02d}" for i in range(n_stocks)]
    etfs = walk(len(SECTOR_ETFS))
    etfs.columns = SECTOR_ETFS
    spy = walk(1)[0].rename("SPY")
    sectors = {t: f"Sector{i % 8}" for i, t in enumerate(stocks.columns)}
    return stocks, etfs, spy, sectors


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--start", default="2005-01-01")
    p.add_argument("--out", default="results")
    p.add_argument("--synthetic", action="store_true", help="fake data, to test the code offline")
    a = p.parse_args()
    run(a.start, a.out, a.synthetic)
