"""Shared rules for the Swing sleeve: buy a dip in a strong, up-trending stock,
then ride it with a trailing stop until the trend is over.

Every number lives in RULES so the backtest and the paper robot stay in sync.
Prices come as a dict of DataFrames {"open", "high", "low", "close", "volume"},
one column per ticker.
"""
import os

import numpy as np
import pandas as pd

RULES = {
    # Universe
    "min_price": 10.0,            # skip penny-ish stocks
    "max_price": float(os.environ.get("SWING_MAX_PRICE", 100.0)),  # your preference; the backtest used $100
    "min_dollar_volume": 20e6,    # average daily trading at least $20m (easy to get in and out)
    # Trend state
    "sma_fast": 50,
    "sma_slow": 200,
    "slope_days": 10,             # 50-day average must be rising over 10 days
    "strength_days": 126,         # 6-month return must beat SPY's
    # Entry: the dip
    "rsi_days": 2,
    "rsi_entry": 10.0,            # 2-day RSI below 10 = a sharp short pullback
    "max_positions": 10,
    "max_position_pct": 0.10,     # never more than 10% of the sleeve in one stock
    "risk_per_trade": 0.01,       # lose at most ~1% of the sleeve if the first stop is hit
    "entry_limit_buffer": 0.005,  # buy limit = last close + 0.5%
    "exit_limit_buffer": 0.01,    # sell limit = last close - 1%
    # Exit
    "atr_days": 14,
    "stop_atr": 2.0,              # first stop: entry - 2 x ATR (the safety margin)
    "trail_atr": 3.0,             # trailing stop: highest close since entry - 3 x ATR
    "max_hold_days": 60,          # sell if still not in profit after 60 trading days
    "quick_exit_sma": 5,          # "quick" exit style only: sell when close > 5-day average
    "quick_max_hold": 10,
    # Costs
    "cost_per_side": 0.001,       # 0.1% commission + slippage per buy or sell
}

BULL, RANGE, BEAR = 1, 0, -1
STATE_NAME = {BULL: "bull", RANGE: "range", BEAR: "bear"}


def rsi(close, days):
    """Wilder RSI."""
    delta = close.diff()
    up = delta.clip(lower=0).ewm(alpha=1 / days, adjust=False).mean()
    down = (-delta.clip(upper=0)).ewm(alpha=1 / days, adjust=False).mean()
    rs = up / down.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).where(down > 0, 100.0)


def indicators(px, spy_close):
    """All per-stock indicators the rules need, as DataFrames."""
    r = RULES
    o, h, l, c, v = (px[k] for k in ("open", "high", "low", "close", "volume"))
    sma_f = c.rolling(r["sma_fast"]).mean()
    sma_s = c.rolling(r["sma_slow"]).mean()
    prev_c = c.shift(1)
    tr = np.maximum(h - l, np.maximum((h - prev_c).abs(), (l - prev_c).abs()))
    atr = tr.rolling(r["atr_days"]).mean()
    strength = c / c.shift(r["strength_days"]) - 1
    spy_strength = spy_close / spy_close.shift(r["strength_days"]) - 1
    rng = (h - l).replace(0, np.nan)
    return {
        "close": c,
        "sma_fast": sma_f,
        "sma_slow": sma_s,
        "slope": sma_f / sma_f.shift(r["slope_days"]) - 1,
        "atr": atr,
        "rsi": rsi(c, r["rsi_days"]),
        "strength": strength,
        "beats_spy": strength.gt(spy_strength, axis=0),
        "dollar_volume": (c * v).rolling(20).mean(),
        # Order-flow proxies (free data): how unusual today's volume is, and
        # where the close sits in the day's range (1 = closed at the high).
        "rvol": v / v.rolling(20).mean().shift(1),
        "close_location": (c - l) / rng,
        "quick_sma": c.rolling(r["quick_exit_sma"]).mean(),
        "spy_ok": spy_close > spy_close.rolling(r["sma_slow"]).mean(),
    }


def trend_state(ind):
    """bull / range / bear per stock and day.
    bull: price above the 200-day, 50-day above 200-day and rising.
    bear: price below the 200-day and 50-day below 200-day.
    range: anything in between."""
    c, f, s, slope = ind["close"], ind["sma_fast"], ind["sma_slow"], ind["slope"]
    state = pd.DataFrame(RANGE, index=c.index, columns=c.columns)
    state[(c > s) & (f > s) & (slope > 0)] = BULL
    state[(c < s) & (f < s)] = BEAR
    return state.where(s.notna())


def entry_signals(ind, state, rsi_max=None):
    """Boolean DataFrame: buy at the next session. rsi_max overrides the dip
    threshold (the backtest uses 101 for its "no dip" control)."""
    r = RULES
    rsi_max = r["rsi_entry"] if rsi_max is None else rsi_max
    c = ind["close"]
    return (
        (state == BULL)
        & ind["beats_spy"]
        & (ind["rsi"] < rsi_max)
        & (c >= r["min_price"]) & (c <= r["max_price"])
        & (ind["dollar_volume"] >= r["min_dollar_volume"])
        & ind["atr"].notna()
    ).mul(ind["spy_ok"], axis=0).astype(bool)


def position_size(sleeve_equity, price, atr):
    """Dollar amount to buy: risk ~1% of the sleeve down to the first stop,
    capped at 10% of the sleeve."""
    stop_dist = RULES["stop_atr"] * atr / price
    by_risk = sleeve_equity * RULES["risk_per_trade"] / max(stop_dist, 1e-9)
    return min(by_risk, sleeve_equity * RULES["max_position_pct"])


def exit_reason(style, close, entry_price, entry_atr, highest_close, atr_now,
                sma_slow, quick_sma, days_held):
    """Why to sell today (or None). Checked on each daily close."""
    r = RULES
    hard_stop = entry_price - r["stop_atr"] * entry_atr
    if close < hard_stop:
        return "first stop hit"
    if style == "quick":
        if close > quick_sma:
            return "bounce done (close above 5-day average)"
        if days_held >= r["quick_max_hold"]:
            return "time stop"
        return None
    if close < highest_close - r["trail_atr"] * atr_now:
        return "trailing stop" + (" (profit locked)" if close > entry_price else "")
    if close < sma_slow:
        return "trend over (below 200-day)"
    if days_held >= r["max_hold_days"] and close <= entry_price:
        return "time stop (no progress)"
    return None


def download_ohlcv(tickers, start, end=None):
    """Daily adjusted OHLCV from Yahoo Finance as a dict of DataFrames."""
    import yfinance as yf

    parts = {k: [] for k in ("open", "high", "low", "close", "volume")}
    tickers = list(tickers)
    for i in range(0, len(tickers), 100):
        chunk = tickers[i:i + 100]
        d = yf.download(chunk, start=start, end=end, auto_adjust=True, progress=False,
                        threads=True, group_by="column")
        for k in parts:
            frame = d[k.capitalize()]
            if isinstance(frame, pd.Series):
                frame = frame.to_frame(chunk[0])
            parts[k].append(frame)
    out = {}
    for k, frames in parts.items():
        f = pd.concat(frames, axis=1).sort_index()
        out[k] = f.loc[:, ~f.columns.duplicated()]
    return out
