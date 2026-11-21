"""Daily morning trading briefing.

Pulls live data from Financial Modeling Prep (FMP) and prints a markdown
summary covering market health, open swing/QM positions, entry candidates,
and timing guidance.

    python briefing.py                     # print to stdout
    python briefing.py --holdings holdings.json

Environment:
    FMP_API_KEY   your FMP API key (required)
    NTFY_TOPIC    optional phone alert topic (same as the robots)

Designed to run as a morning GitHub Actions cron job (see
.github/workflows/briefing.yml) or as a Claude scheduled trigger.
"""
import argparse
import json
import os
import sys
from datetime import date, timedelta

import requests

BASE = "https://financialmodelingprep.com/stable"


def fmp(path, **params):
    params["apikey"] = os.environ["FMP_API_KEY"]
    r = requests.get(BASE + path, params=params, timeout=20)
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------

def market_health():
    """S&P 500 price vs MAs, VIX, sector snapshot."""
    spy = fmp("/quote/SPY")[0]
    vix = fmp("/quote/%5EVIX")[0]
    sectors = fmp("/sector-performance")
    # Sort sectors by today's change, best first
    sectors = sorted(sectors, key=lambda s: s.get("changesPercentage", 0), reverse=True)
    return spy, vix, sectors


def position_status(holdings: dict) -> list[dict]:
    """For each held ticker, fetch current price and evaluate stops."""
    if not holdings:
        return []
    tickers = ",".join(holdings.keys())
    quotes = {q["symbol"]: q for q in fmp(f"/quote/{tickers}")}
    rows = []
    for ticker, h in holdings.items():
        q = quotes.get(ticker, {})
        price = q.get("price", 0)
        entry = h["entry"]
        pct = (price / entry - 1) * 100 if entry else 0
        stop_pct = -20.0  # QM hard stop
        # Swing trailing stop: highest_close - 3x ATR (approximated; robot tracks this live)
        atr = q.get("priceAvg50", price) * 0.015  # rough 1.5% of price as proxy if no ATR
        trail_stop = h.get("highest", price) - 3 * atr
        hard_stop = entry * 0.80
        stop_hit = price < hard_stop
        action = "EXIT — stop hit" if stop_hit else ("TRIM — up >15%" if pct > 15 else "HOLD")
        rows.append({
            "ticker": ticker,
            "price": price,
            "entry": entry,
            "pct": pct,
            "trail_stop": max(hard_stop, trail_stop),
            "action": action,
            "change_pct": q.get("changesPercentage", 0),
        })
    return rows


def entry_candidates(spy_above_200: bool) -> list[dict]:
    """Top momentum stocks from FMP's gainers that also look technically sound."""
    if not spy_above_200:
        return []
    gainers = fmp("/stock-screener", marketCapMoreThan=2_000_000_000, priceMoreThan=10,
                  priceLowerThan=200, volumeMoreThan=500_000, limit=50)
    # For each, check basic momentum: 1-month return, above 200-day SMA
    cands = []
    for s in gainers[:30]:
        ticker = s.get("symbol")
        if not ticker:
            continue
        q = fmp(f"/quote/{ticker}")
        if not q:
            continue
        q = q[0]
        price = q.get("price", 0)
        sma200 = q.get("priceAvg200", 0)
        sma50 = q.get("priceAvg50", 0)
        if price <= 0 or sma200 <= 0:
            continue
        if price < sma200 or price < sma50:
            continue  # must be above both MAs
        mom_6m = (price / q.get("yearLow", price) - 1) * 100  # rough proxy
        cands.append({
            "ticker": ticker,
            "name": s.get("companyName", ""),
            "price": price,
            "sector": s.get("sector", ""),
            "mom_6m": mom_6m,
            "mktcap": s.get("marketCap", 0),
        })
        if len(cands) >= 5:
            break
    cands.sort(key=lambda x: -x["mom_6m"])
    return cands[:5]


def timing_guidance(vix: float, sectors: list) -> str:
    """Plain-language timing suggestion based on VIX and sector rotation."""
    if vix > 30:
        return "VIX above 30 — **wait for mid-day calm** before placing any new entries. Volatility is extreme."
    if vix > 20:
        leading = sectors[0]["sector"] if sectors else "unknown"
        if leading in ("Healthcare", "Utilities", "Consumer Defensive"):
            return (f"VIX {vix:.1f} with defensive rotation ({leading} leading). "
                    "**Mid-day entry** preferred — confirm price holds before buying.")
        return f"VIX {vix:.1f}, elevated but manageable. **Place orders mid-morning** after open settles."
    return f"VIX {vix:.1f}, calm. **Open or mid-morning entries** are fine."


# ---------------------------------------------------------------------------
# Markdown renderer
# ---------------------------------------------------------------------------

def build_briefing(spy, vix_q, sectors, positions, candidates, today) -> str:
    spy_price = spy["price"]
    spy_sma200 = spy.get("priceAvg200", 0)
    spy_sma50 = spy.get("priceAvg50", 0)
    spy_chg = spy.get("changesPercentage", 0)
    vix = vix_q["price"]
    vix_chg = vix_q.get("changesPercentage", 0)

    above_200 = spy_price > spy_sma200
    above_50 = spy_price > spy_sma50
    brake = "🛑 **CRASH BRAKE ON** — below 200-day, hold cash" if not above_200 else "✅ above 200-day"
    trend_note = "above 50-day" if above_50 else "⚠️ below 50-day (short-term pullback)"

    top3 = [f"{s['sector']} {s['changesPercentage']:+.1f}%" for s in sectors[:3]]
    bot2 = [f"{s['sector']} {s['changesPercentage']:+.1f}%" for s in sectors[-2:]]

    lines = [
        f"# 📊 Daily Trading Briefing — {today}",
        "",
        "## 1. Market Health",
        "",
        f"| | Value | Change | Signal |",
        "|---|---|---|---|",
        f"| S&P 500 | {spy_price:,.2f} | {spy_chg:+.2f}% | {brake}, {trend_note} |",
        f"| VIX | {vix:.2f} | {vix_chg:+.2f}% | {'🔴 High fear' if vix > 25 else '🟡 Elevated' if vix > 18 else '🟢 Calm'} |",
        f"| SPY 50-day | {spy_sma50:,.0f} | | {'Above ✓' if above_50 else 'Below ⚠️'} |",
        f"| SPY 200-day | {spy_sma200:,.0f} | | {'Above ✓' if above_200 else 'BELOW — no new buys'} |",
        "",
        f"**Sector rotation:** Leading — {', '.join(top3)}  |  Lagging — {', '.join(bot2)}",
        "",
    ]

    # Open positions
    lines += ["## 2. Open Positions", ""]
    if positions:
        lines += [
            "| Ticker | Entry | Now | P&L | Action |",
            "|---|---|---|---|---|",
        ]
        for p in positions:
            arrow = "📈" if p["pct"] > 0 else "📉"
            lines.append(f"| **{p['ticker']}** | ${p['entry']:.2f} | ${p['price']:.2f} "
                         f"| {arrow} {p['pct']:+.1f}% | {p['action']} |")
    else:
        lines.append("_No holdings loaded. Create a `holdings.json` file — "
                     "see [format](#holdings-format) below._")
    lines.append("")

    # Entry candidates
    lines += ["## 3. Entry Candidates (Quality Momentum swing strategy)", ""]
    if not above_200:
        lines.append("⛔ Crash brake active — no new entries until SPY reclaims its 200-day average.")
    elif candidates:
        lines += [
            "| # | Ticker | Sector | Price | 6m Momentum |",
            "|---|---|---|---|---|",
        ]
        for i, c in enumerate(candidates, 1):
            lines.append(f"| {i} | **{c['ticker']}** | {c['sector']} | ${c['price']:.2f} | +{c['mom_6m']:.0f}% |")
        lines.append("")
        lines.append("> Confirm: price above 200-day SMA, 2-day RSI < 10 for a dip entry. Size at ~1% portfolio risk per trade.")
    else:
        lines.append("_No strong candidates today — market breadth or momentum is weak._")
    lines.append("")

    # Timing
    lines += [
        "## 4. Timing",
        "",
        timing_guidance(vix, sectors),
        "",
        "---",
        f"_Generated {today}. Source: FMP live data._",
    ]

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Holdings format note
# ---------------------------------------------------------------------------

HOLDINGS_NOTE = """
## Holdings Format

Create `holdings.json` next to this script:

```json
{
  "AAPL": {"qty": 10, "entry": 190.50, "highest": 210.00},
  "MSFT": {"qty": 5,  "entry": 420.00, "highest": 440.00}
}
```

`highest` is the highest closing price since entry (used for trailing stop calc).
If omitted, current price is used as the high.
"""


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--holdings", default="holdings.json")
    p.add_argument("--out", help="write markdown to this file instead of stdout")
    a = p.parse_args()

    if not os.environ.get("FMP_API_KEY"):
        sys.exit("Missing FMP_API_KEY environment variable.")

    holdings = {}
    if os.path.exists(a.holdings):
        with open(a.holdings) as f:
            holdings = json.load(f)

    today = str(date.today())
    spy, vix_q, sectors = market_health()
    spy_price = spy["price"]
    spy_sma200 = spy.get("priceAvg200", 0)
    above_200 = spy_price > spy_sma200

    positions = position_status(holdings)
    candidates = entry_candidates(above_200)

    md = build_briefing(spy, vix_q, sectors, positions, candidates, today)

    if a.out:
        with open(a.out, "w") as f:
            f.write(md)
        print(f"Written to {a.out}")
    else:
        print(md)

    # Optional phone alert via ntfy (same as the robots)
    topic = os.environ.get("NTFY_TOPIC")
    if topic:
        import requests as _r
        vix = vix_q["price"]
        chg = spy.get("changesPercentage", 0)
        msg = (f"SPY {spy_price:,.0f} ({chg:+.1f}%)  VIX {vix:.1f}\n"
               f"{'Crash brake ON' if not above_200 else f'{len(candidates)} entry cands'}")
        _r.post(f"https://ntfy.sh/{topic}", data=msg.encode(),
                headers={"Title": f"Briefing {today}"}, timeout=10)


if __name__ == "__main__":
    main()
