"""Shared rules for the Quality Momentum strategy.

Every number the strategy uses lives in RULES so the backtest, the monthly
signal script and the written plan all stay in sync.
"""
import io

import numpy as np
import pandas as pd

RULES = {
    "min_price": 10.0,          # rule 2: skip stocks under $10
    "sma_days": 200,            # rules 6, 9: 200-day moving average
    "mom_lookback": 252,        # rule 7: 12-month momentum...
    "mom_skip": 21,             # ...excluding the most recent month
    "top_n": 10,                # rule 8: hold 10 stocks
    "keep_rank": 30,            # rule 11: keep a holding while it ranks in the top 30
    "max_per_sector": 3,        # rule 8: max 3 stocks per sector
    "stop_loss": 0.20,          # rule 12: sell if 20% below entry
    "cost_per_trade": 0.001,    # 0.1% of traded value (commission + slippage)
    "max_debt_to_equity": 2.0,  # rule 5 (live signals only, not backtested)
}

SECTOR_ETFS = ["XLB", "XLE", "XLF", "XLI", "XLK", "XLP", "XLU", "XLV", "XLY"]


def sp500_constituents():
    """Current S&P 500 members with GICS sector, from Wikipedia."""
    import requests

    url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
    html = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=30).text
    table = pd.read_html(io.StringIO(html))[0]
    table["Symbol"] = table["Symbol"].str.replace(".", "-", regex=False)
    return dict(zip(table["Symbol"], table["GICS Sector"]))


def download_prices(tickers, start, end=None):
    """Adjusted daily closes from Yahoo Finance, one column per ticker."""
    import yfinance as yf

    frames = []
    tickers = list(tickers)
    for i in range(0, len(tickers), 100):
        chunk = tickers[i:i + 100]
        data = yf.download(chunk, start=start, end=end, auto_adjust=True,
                           progress=False, threads=True)["Close"]
        if isinstance(data, pd.Series):
            data = data.to_frame(chunk[0])
        frames.append(data)
    prices = pd.concat(frames, axis=1).sort_index()
    return prices.loc[:, ~prices.columns.duplicated()]


def indicators(prices):
    """200-day average and 12-1 momentum for every ticker."""
    sma = prices.rolling(RULES["sma_days"], min_periods=RULES["sma_days"]).mean()
    mom = prices.shift(RULES["mom_skip"]) / prices.shift(RULES["mom_lookback"]) - 1
    return sma, mom


def rank_candidates(close, sma, mom, sectors=None):
    """Tickers passing rules 2, 6 and 7, best momentum first."""
    ok = (close > RULES["min_price"]) & (close > sma) & mom.notna()
    ranked = mom[ok].sort_values(ascending=False)
    return list(ranked.index)


def pick_portfolio(held, ranked, close, sma, sectors=None,
                   top_n=None, keep_rank=None, max_per_sector=None):
    """Apply rules 8 and 11: keep holdings still ranked inside the buffer,
    then fill empty slots from the top of the list under the sector cap."""
    top_n = top_n or RULES["top_n"]
    keep_rank = keep_rank or RULES["keep_rank"]
    max_per_sector = max_per_sector or RULES["max_per_sector"]
    rank_of = {t: i for i, t in enumerate(ranked)}

    keep = [t for t in held
            if rank_of.get(t, 10**9) < keep_rank and close.get(t, np.nan) > sma.get(t, np.nan)]
    picks = list(keep)
    sector_count = {}
    if sectors:
        for t in picks:
            sector_count[sectors.get(t)] = sector_count.get(sectors.get(t), 0) + 1
    for t in ranked:
        if len(picks) >= top_n:
            break
        if t in picks:
            continue
        if sectors:
            s = sectors.get(t)
            if sector_count.get(s, 0) >= max_per_sector:
                continue
            sector_count[s] = sector_count.get(s, 0) + 1
        picks.append(t)
    return picks
