"""TradingView-based entry filter for Quality Momentum.

Before buying a new position, cross-check the momentum pick against live
TradingView technical data.  The goal is to cut the -40% maximum drawdown
from the backtest by avoiding purchases when a stock is already overextended.

Filter rules (applied to new buys only, never to existing holdings):
  1. RSI(14,daily) < 75          — not overbought at entry
  2. MACD line > 0               — confirmed upward momentum
  3. Price >= SMA200 * 1.02      — at least 2% above the trend line
  4. TradingView oscillator summary != STRONG_SELL  — broad technical health

Usage:
    from tv_entry_filter import tv_entry_ok
    if tv_entry_ok(ticker):
        # proceed with buy
"""

import os
import time
from typing import Optional

import requests

_TV_SESSION = None
_CACHE: dict[str, tuple[float, bool, str]] = {}   # ticker -> (timestamp, ok, reason)
_CACHE_TTL = 3600   # 1 hour


def _tradingview_technicals(ticker: str, interval: str = "1D") -> Optional[dict]:
    """Fetch TradingView technical snapshot via their free scanner endpoint."""
    exchange_map = {"BRK-B": "NYSE:BRK.B"}
    tv_symbol = exchange_map.get(ticker, ticker.replace("-", "."))
    url = "https://scanner.tradingview.com/america/scan"
    payload = {
        "symbols": {"tickers": [f"NASDAQ:{tv_symbol}", f"NYSE:{tv_symbol}"]},
        "columns": ["RSI", "MACD.macd", "close", "SMA200", "Recommend.Other"]
    }
    try:
        r = requests.post(url, json=payload, timeout=10,
                         headers={"Content-Type": "application/json",
                                  "User-Agent": "Mozilla/5.0"})
        if r.status_code != 200:
            return None
        data = r.json().get("data", [])
        if not data:
            return None
        row = data[0].get("d", [])
        if len(row) < 5:
            return None
        return {"rsi": row[0], "macd": row[1], "close": row[2],
                "sma200": row[3], "oscillator_rec": row[4]}
    except Exception:
        return None


def tv_entry_ok(ticker: str) -> tuple[bool, str]:
    """Return (ok, reason) for a proposed new buy.

    Returns (True, "") when the entry looks clean.
    Returns (False, reason) when the filter rejects it.
    Returns (True, "tv_unavailable") when TradingView data could not be fetched
    — the caller should still proceed, as the filter is advisory.
    """
    now = time.time()
    if ticker in _CACHE and now - _CACHE[ticker][0] < _CACHE_TTL:
        _, ok, reason = _CACHE[ticker]
        return ok, reason

    tv = _tradingview_technicals(ticker)
    if tv is None:
        result = (True, "tv_unavailable")
        _CACHE[ticker] = (now, *result)
        return result

    rsi = tv.get("rsi")
    macd = tv.get("macd")
    close = tv.get("close")
    sma200 = tv.get("sma200")
    osc = tv.get("oscillator_rec")

    if rsi is not None and rsi >= 75:
        result = (False, f"RSI {rsi:.1f} >= 75: overbought at entry")
    elif macd is not None and macd < 0:
        result = (False, f"MACD {macd:.3f} < 0: momentum not confirmed")
    elif close is not None and sma200 is not None and close < sma200 * 1.02:
        result = (False, f"Price {close:.2f} < SMA200*1.02 {sma200 * 1.02:.2f}: too close to trend line")
    elif osc == "STRONG_SELL":
        result = (False, "TradingView oscillators: STRONG_SELL")
    else:
        result = (True, "")

    _CACHE[ticker] = (now, *result)
    return result
