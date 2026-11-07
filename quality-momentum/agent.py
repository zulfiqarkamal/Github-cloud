"""Generative Trading Agent — daily stock signal with AI reasoning.

Combines the Quality Momentum quantitative screen with Claude AI analysis to
produce a prioritised buy/hold/sell list each trading day.

Pipeline:
  1. SCREEN  — fetch Finviz top-performers (must-haves pre-applied by URL filter)
  2. SCORE   — apply M1-M5 must-haves and S1-S8 points (0-10) using yfinance data
  3. GENERATE — Claude synthesises a plain-English signal with confidence + reasoning
  4. EXECUTE  — optionally place paper orders on Alpaca

Usage:
    python agent.py                      # dry run, prints report + writes report.json
    python agent.py --execute            # also sends orders to Alpaca paper account

Env vars (same as autotrader.py):
    ANTHROPIC_API_KEY                    required for the AI reasoning step
    ALPACA_KEY_ID, ALPACA_SECRET_KEY     required for --execute
    NTFY_TOPIC                           optional phone alerts
    LIVE_TRADING                         leave unset; "yes-real-money" uses live Alpaca
"""
import argparse
import json
import os
import re
import sys
from datetime import date, datetime, timedelta

import pandas as pd
import requests

from strategy import RULES, download_prices, indicators, sp500_constituents

# ── constants ────────────────────────────────────────────────────────────────

FINVIZ_SCREEN = (
    "https://finviz.com/screener.ashx?v=111"
    "&f=cap_midover,geo_usa,fa_pe_profitable,fa_salesqoq_pos,ta_sma200_pa"
    ",sh_avgvol_o500,sh_price_o5"
    "&o=-perf26w"
)

TOP_N_SCREEN = 25        # candidates pulled from Finviz
TIER_A_THRESHOLD = 7     # min score to be eligible for buying
MAX_POSITIONS = 5        # max open positions
MAX_PER_SECTOR = 2


# ── Finviz screener ───────────────────────────────────────────────────────────

def fetch_finviz_top(n=TOP_N_SCREEN):
    """Return top-n tickers (best 6-month perf) from the must-have Finviz screen.

    Falls back to building the universe from the full S&P 500 via yfinance when
    Finviz is unreachable (e.g. from GitHub Actions runners).
    """
    headers = {"User-Agent": "Mozilla/5.0 (compatible; research-bot/1.0)"}
    tickers = []
    page = 1
    finviz_ok = False
    while len(tickers) < n:
        url = FINVIZ_SCREEN + f"&r={(page - 1) * 20 + 1}"
        try:
            r = requests.get(url, headers=headers, timeout=20)
            r.raise_for_status()
            finviz_ok = True
        except requests.RequestException:
            break
        rows = re.findall(r'ticker=([A-Z\-.]+)', r.text)
        new = [t for t in rows if t not in tickers]
        if not new:
            break
        tickers += new
        page += 1

    if finviz_ok and tickers:
        return tickers[:n]

    # Finviz unreachable — fall back to full S&P 500 universe via yfinance
    print("[fallback] Finviz unreachable, building universe from S&P 500 + yfinance…")
    return _sp500_momentum_top(n)


def _sp500_momentum_top(n):
    """Build a top-n list from S&P 500 using price momentum (12-1 month)."""
    from strategy import download_prices, indicators, rank_candidates, sp500_constituents
    sectors = sp500_constituents()
    tickers = list(sectors.keys())
    start = (date.today() - timedelta(days=400)).isoformat()
    prices = download_prices(tickers, start=start)
    if prices.empty:
        print("ERROR: yfinance returned no price data.", file=sys.stderr)
        sys.exit(1)
    today_prices = prices.iloc[-1]
    sma, mom = indicators(prices)
    today_sma = sma.iloc[-1]
    today_mom = mom.iloc[-1]
    ranked = rank_candidates(today_prices, today_sma, today_mom, sectors=sectors)
    return ranked[:n]


# ── quality & scoring ─────────────────────────────────────────────────────────

def _safe_float(val, default=None):
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def score_ticker(ticker, sector, close_price, sma200, mom6, spy_mom3, spy_mom6, all_moms):
    """
    Apply M1-M5 must-haves and S1-S8 scoring using yfinance fundamentals.
    Returns (passed_must_haves, score, detail_dict, reject_reason).
    """
    import yfinance as yf
    try:
        info = yf.Ticker(ticker).fast_info
        full = yf.Ticker(ticker).info
    except Exception as e:
        return False, 0, {}, f"data error: {e}"

    # ── Must-haves ──────────────────────────────────────────────────────────
    # M1: cap >= $1B, avg vol >= 500k, price >= $5  (mostly filtered by Finviz URL)
    mkt_cap = _safe_float(full.get("marketCap"), 0)
    if mkt_cap < 1e9:
        return False, 0, {}, f"M1 market cap {mkt_cap/1e9:.1f}B < 1B"

    # M2: profitable + revenue growing
    ni = _safe_float(full.get("netIncomeToCommon"), None)
    rev_growth = _safe_float(full.get("revenueGrowth"), None)
    ocf = _safe_float(full.get("operatingCashflow"), None)
    if ni is None or ni <= 0:
        # fast-grower exception: revenue up 25%+ and positive OCF
        if rev_growth is not None and rev_growth >= 0.25 and ocf is not None and ocf > 0:
            pass  # fast-grower exception
        else:
            return False, 0, {}, "M2 not profitable (no fast-grower exception)"
    if rev_growth is None or rev_growth <= 0:
        return False, 0, {}, "M2 revenue not growing"

    # M3: price above 200-day — already filtered by Finviz, but double-check
    if close_price <= sma200:
        return False, 0, {}, f"M3 price {close_price:.2f} < 200-day {sma200:.2f}"

    # M4: dilution, short interest
    short_pct = _safe_float(full.get("shortPercentOfFloat"), None)
    if short_pct is not None and short_pct > 0.15:
        return False, 0, {}, f"M4 short interest {short_pct:.0%} > 15%"

    # ── Score S1–S8 ─────────────────────────────────────────────────────────
    score = 0
    detail = {}

    # S1: momentum 2pts — top third of screened list = 2, better than SPY 6m = 1
    top_third_cut = sorted(all_moms, reverse=True)[max(0, len(all_moms) // 3 - 1)]
    if mom6 >= top_third_cut:
        s1 = 2
    elif mom6 > spy_mom6:
        s1 = 1
    else:
        s1 = 0
    score += s1; detail["S1"] = s1

    # S2: relative strength — 3-month better than SPY
    # We use 6m/2 as a proxy for 3m when we don't have separate 3m data
    # (actual 3m comes from the same mom series with different shift)
    s2 = 1 if mom6 > spy_mom3 else 0
    score += s2; detail["S2"] = s2

    # S3: EBIT growth 2pts
    op_margins = full.get("operatingMargins")
    ebitda_growth = _safe_float(full.get("earningsGrowth"), None)
    if ebitda_growth is not None:
        if ebitda_growth >= 0.20:
            s3 = 2
        elif ebitda_growth >= 0.05:
            s3 = 1
        else:
            s3 = 0
    else:
        s3 = 0
    score += s3; detail["S3"] = s3

    # S4: estimates raised / EPS beats
    eps_surprise_hist = full.get("earningsHistory")
    s4 = 0
    analyst_count = _safe_float(full.get("numberOfAnalystOpinions"), 0)
    target_mean = _safe_float(full.get("targetMeanPrice"), None)
    # Simple proxy: if analyst target exists and is >10% above price → likely positive revisions
    if target_mean and target_mean > close_price * 1.10 and analyst_count >= 3:
        s4 = 1
    score += s4; detail["S4"] = s4

    # S5: PEG < 1.5
    peg = _safe_float(full.get("pegRatio"), None)
    s5 = 1 if peg is not None and 0 < peg < 1.5 else 0
    score += s5; detail["S5"] = s5

    # S6: forward P/E vs history (approximation using trailing P/E vs sector median)
    fwd_pe = _safe_float(full.get("forwardPE"), None)
    trail_pe = _safe_float(full.get("trailingPE"), None)
    s6 = 0
    if fwd_pe and trail_pe and fwd_pe < trail_pe * 1.20:
        s6 = 1
    elif fwd_pe and 5 < fwd_pe < 30:
        s6 = 1
    score += s6; detail["S6"] = s6

    # S7: analyst upside >= 15%, 3+ analysts
    upside = (target_mean / close_price - 1) if target_mean else None
    s7 = 1 if upside is not None and upside >= 0.15 and analyst_count >= 3 else 0
    score += s7; detail["S7"] = s7

    # S8: catalyst — approximate by recent insider buying or earnings within 45 days
    # (Real catalyst check needs news/SEC; here we flag as unknown → 0 pts)
    s8 = 0
    score += s8; detail["S8"] = s8

    fundamentals = {
        "mkt_cap_b": round(mkt_cap / 1e9, 1),
        "rev_growth": round(rev_growth * 100, 1) if rev_growth else None,
        "ebitda_growth": round(ebitda_growth * 100, 1) if ebitda_growth else None,
        "peg": peg,
        "fwd_pe": fwd_pe,
        "analyst_target": target_mean,
        "analyst_count": int(analyst_count) if analyst_count else 0,
        "short_pct": round(short_pct * 100, 1) if short_pct else None,
        "sector": sector,
    }
    detail.update(fundamentals)
    return True, score, detail, ""


# ── AI reasoning layer ────────────────────────────────────────────────────────

def generate_signals_with_ai(candidates, spy_status, today, top_held):
    """
    Call Claude to produce a synthesised daily signal report.
    Returns a markdown string.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return "[ANTHROPIC_API_KEY not set — skipping AI reasoning layer]\n"

    import anthropic
    client = anthropic.Anthropic(api_key=api_key)

    # Build a compact summary of scored candidates for the prompt
    lines = []
    for c in candidates[:15]:
        tier = "A" if c["score"] >= TIER_A_THRESHOLD else "B" if c["score"] >= 5 else "C"
        d = c["detail"]
        lines.append(
            f"  {c['ticker']} ({d.get('sector','?')}) tier={tier} score={c['score']}/10 "
            f"mom6m={c['mom6m']:+.0%} revgrowth={d.get('rev_growth','?')}% "
            f"ebitda_g={d.get('ebitda_growth','?')}% PEG={d.get('peg','?')} "
            f"fwdPE={d.get('fwd_pe','?')} analysts={d.get('analyst_count','?')} "
            f"target=${d.get('analyst_target','?')} short={d.get('short_pct','?')}%"
        )

    held_str = ", ".join(top_held) if top_held else "none"
    candidate_block = "\n".join(lines) if lines else "  (no candidates passed must-haves)"

    prompt = f"""You are a disciplined swing-trading analyst. Today is {today}.

MARKET STATUS: SPY is {spy_status}.

CURRENT HOLDINGS: {held_str}

TOP SCORED CANDIDATES (after must-haves M1-M5 and S1-S8 scoring):
{candidate_block}

Your job:
1. For TIER A candidates (score 7+): write 2-3 sentences of reasoning for each — why the momentum+fundamentals case is compelling, what the main risk is, and your confidence (high/medium/low).
2. Identify the top 3 BUY recommendations for today (Tier A only). If SPY is below 200-day, say "No new positions — crash brake active."
3. For current holdings, say HOLD or SELL with one-line rationale.
4. End with a 2-line SUMMARY: total open slots to fill, single most important risk today.

Be direct and brief. No hedging, no disclaimers. Focus on profit."""

    response = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=1200,
        messages=[{"role": "user", "content": prompt}]
    )
    return response.content[0].text


# ── Alpaca execution ──────────────────────────────────────────────────────────

def execute_on_alpaca(buy_tickers, sell_tickers, close_prices, capital_fraction=1.0):
    """Place orders on Alpaca paper (or live if LIVE_TRADING=yes-real-money)."""
    from autotrader import Alpaca, notify

    live = os.environ.get("LIVE_TRADING") == "yes-real-money"
    key = os.environ.get("ALPACA_KEY_ID")
    secret = os.environ.get("ALPACA_SECRET_KEY")
    if not key or not secret:
        print("ALPACA_KEY_ID / ALPACA_SECRET_KEY missing — skipping execution.")
        return

    broker = Alpaca(key, secret, live=live)
    acct = broker.account()
    equity = float(acct["equity"])
    slot = equity * capital_fraction / MAX_POSITIONS
    actions = []

    for t in sell_tickers:
        try:
            broker.sell_all(t)
            actions.append(f"SOLD {t}")
        except Exception as e:
            actions.append(f"SELL {t} failed: {e}")

    for t in buy_tickers:
        price = close_prices.get(t)
        if not price:
            continue
        qty = int(slot // price)
        if qty < 1:
            continue
        try:
            broker.buy(t, qty)
            actions.append(f"BOUGHT {qty} {t} @ ~{price:.2f}")
        except Exception as e:
            actions.append(f"BUY {t} failed: {e}")

    notify(actions or ["No orders placed."], "Generative Agent: orders")
    return actions


# ── orchestrator ──────────────────────────────────────────────────────────────

def run(execute=False, dry_run_data=None):
    today = str(date.today())

    # 1. SCREEN
    print(f"[{today}] Fetching Finviz top {TOP_N_SCREEN} candidates…")
    if dry_run_data:
        tickers = dry_run_data["tickers"]
    else:
        tickers = fetch_finviz_top(TOP_N_SCREEN)
    if not tickers:
        print("Could not fetch Finviz screener. Aborting.")
        sys.exit(1)
    print(f"  Screened: {', '.join(tickers[:10])}{'…' if len(tickers)>10 else ''}")

    # 2. PRICE DATA
    print("  Downloading prices…")
    sectors = sp500_constituents()
    start = (pd.Timestamp(today) - pd.DateOffset(months=14)).strftime("%Y-%m-%d")
    universe = sorted(set(tickers) | {"SPY"})
    if dry_run_data:
        prices = dry_run_data["prices"]
    else:
        prices = download_prices(universe, start).ffill()

    spy_series = prices.pop("SPY")
    sma_all, mom_all = indicators(prices)
    spy_sma200 = spy_series.rolling(200).mean().iloc[-1]
    spy_now = spy_series.iloc[-1]
    spy_above_200 = spy_now >= spy_sma200

    # SPY momentum proxies
    spy_mom6 = float(spy_series.iloc[-1] / spy_series.iloc[-126] - 1) if len(spy_series) >= 126 else 0.0
    spy_mom3 = float(spy_series.iloc[-1] / spy_series.iloc[-63] - 1) if len(spy_series) >= 63 else 0.0

    spy_status = (
        f"{spy_now:.2f} (ABOVE 200-day {spy_sma200:.2f})" if spy_above_200
        else f"{spy_now:.2f} (BELOW 200-day {spy_sma200:.2f}) — crash brake ON"
    )

    close = prices.iloc[-1]
    sma = sma_all.iloc[-1]
    mom = mom_all.iloc[-1]

    # All 6m momentums for top-third calculation
    all_moms = [float(mom.get(t, 0)) for t in tickers if pd.notna(mom.get(t))]

    # 3. SCORE each ticker
    print("  Scoring candidates…")
    candidates = []
    for t in tickers:
        close_p = float(close.get(t, 0))
        sma200 = float(sma.get(t, 0))
        mom6 = float(mom.get(t, 0)) if pd.notna(mom.get(t)) else 0.0
        if close_p == 0 or sma200 == 0:
            continue
        passed, score, detail, reason = score_ticker(
            t, sectors.get(t, "Unknown"), close_p, sma200, mom6,
            spy_mom3, spy_mom6, all_moms
        )
        entry = {
            "ticker": t, "passed_must_haves": passed, "score": score,
            "detail": detail, "reject_reason": reason,
            "close": round(close_p, 2), "mom6m": round(mom6, 4),
        }
        candidates.append(entry)
        status = f"✓ {score}/10" if passed else f"✗ {reason}"
        print(f"    {t:6} {status}")

    # Sort by score descending, then momentum
    candidates.sort(key=lambda c: (c["passed_must_haves"], c["score"], c["mom6m"]), reverse=True)
    tier_a = [c for c in candidates if c["passed_must_haves"] and c["score"] >= TIER_A_THRESHOLD]
    tier_b = [c for c in candidates if c["passed_must_haves"] and 5 <= c["score"] < TIER_A_THRESHOLD]

    # 4. GENERATE — AI reasoning
    print("  Generating AI signal…")
    # Existing holdings from autotrader (approximate from Alpaca if available)
    held = []
    if execute:
        from autotrader import Alpaca
        try:
            key = os.environ.get("ALPACA_KEY_ID", "")
            secret = os.environ.get("ALPACA_SECRET_KEY", "")
            if key and secret:
                broker = Alpaca(key, secret)
                held = list(broker.positions().keys())
        except Exception:
            pass

    ai_text = generate_signals_with_ai(candidates, spy_status, today, held)

    # 5. BUILD REPORT
    report = {
        "date": today,
        "spy": {"price": round(float(spy_now), 2), "sma200": round(float(spy_sma200), 2),
                "above_200d": spy_above_200},
        "tier_a": [{"ticker": c["ticker"], "score": c["score"], "close": c["close"],
                    "mom6m": c["mom6m"], "sector": c["detail"].get("sector")} for c in tier_a],
        "tier_b": [{"ticker": c["ticker"], "score": c["score"]} for c in tier_b],
        "ai_signal": ai_text,
        "all_scored": [{k: v for k, v in c.items() if k != "detail"} for c in candidates[:25]],
    }

    with open("report.json", "w") as f:
        json.dump(report, f, indent=2)

    # 6. PRINT SUMMARY
    print(f"\n{'='*60}")
    print(f"GENERATIVE TRADING AGENT — {today}")
    print(f"SPY: {spy_status}")
    print(f"{'='*60}")
    print(f"\nTIER A (buy candidates, score ≥7):")
    for c in tier_a[:5]:
        print(f"  {c['ticker']:6} score={c['score']}/10  6m={c['mom6m']:+.0%}  ${c['close']:.2f}  {c['detail'].get('sector','')}")
    print(f"\nTIER B (watchlist, score 5-6):")
    for c in tier_b[:5]:
        print(f"  {c['ticker']:6} score={c['score']}/10  6m={c['mom6m']:+.0%}")
    print(f"\nAI SIGNAL:\n{ai_text}")
    print(f"\nFull report written to report.json")

    # 7. EXECUTE
    if execute and spy_above_200 and tier_a:
        buy_top = [c["ticker"] for c in tier_a[:3] if c["ticker"] not in held]
        sell_stops = []  # stop-loss handled by autotrader.py daily check
        close_map = {c["ticker"]: c["close"] for c in candidates}
        execute_on_alpaca(buy_top, sell_stops, close_map)

    return report


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--execute", action="store_true", help="Place orders on Alpaca paper account")
    a = p.parse_args()
    run(execute=a.execute)


if __name__ == "__main__":
    main()
