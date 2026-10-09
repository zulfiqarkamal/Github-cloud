"""News layer: read free RSS headlines, have Claude score their likely impact on
US stocks, and drop anything that is already priced in.

How the robot uses it (deliberately cautious until the log proves an edge):
  * VETO: skip a planned buy if the stock has fresh medium/high "down" news.
  * ALERT: phone alert for high-impact headlines on stocks you hold or on the
    S&P 500 list. Alerts are information; the robot does not buy on news.
  * LOG: every scored headline is saved (news_log.jsonl) so news_validate.py
    can later measure whether the scores actually predicted the next days' move.

Needs ANTHROPIC_API_KEY. Without it the robot runs fine, just without news.
X/Twitter is not included: its API is paid, and the FT feed is headlines only.
"""
import json
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import List, Literal

import requests
from pydantic import BaseModel

GENERAL_FEEDS = {
    "FT": "https://www.ft.com/rss/home",
    "CNBC": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114",
    "MarketWatch": "https://feeds.content.dowjones.io/public/rss/mw_topstories",
    "Federal Reserve": "https://www.federalreserve.gov/feeds/press_all.xml",
}
TICKER_FEED = "https://feeds.finance.yahoo.com/rss/2.0/headline?s={}&region=US&lang=en-US"
MODEL = os.environ.get("NEWS_MODEL", "claude-opus-5-5")
MAX_AGE_HOURS = 36


class Impact(BaseModel):
    ticker: str
    direction: Literal["up", "down"]
    strength: Literal["low", "medium", "high"]
    reason: str


class Scored(BaseModel):
    index: int
    reports_past_move: bool
    impacts: List[Impact]


class Scores(BaseModel):
    items: List[Scored]


SYSTEM = """You judge whether news headlines will move specific US-listed stocks over the next 1-5 trading days.
For each headline, list only direct, material impacts on named US tickers (or obvious, specific beneficiaries or
losers, such as a sanction on a named company's main market). Use "high" only for events that change a company's
earnings outlook, such as guidance changes, big contract wins or losses, regulatory actions, M&A, or major
geopolitical shocks to a specific sector. Leave impacts empty for opinion pieces, generic market commentary, and
anything vague. Set reports_past_move=true when the headline mainly reports a price move that already happened
(for example "X shares jump 8% after ..."), because that news is already in the price."""


def fetch_rss(url, source, timeout=15):
    """[{title, link, published, source}] from one RSS feed; [] if it fails."""
    try:
        r = requests.get(url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0 swing-news"})
        r.raise_for_status()
        root = ET.fromstring(r.content)
    except (requests.RequestException, ET.ParseError) as e:
        print(f"(feed {source} unavailable: {e})")
        return []
    out = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        if not title:
            continue
        try:
            published = parsedate_to_datetime(item.findtext("pubDate") or "")
            if published.tzinfo is None:
                published = published.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            published = datetime.now(timezone.utc)
        out.append({"title": re.sub(r"\s+", " ", title), "link": item.findtext("link") or "",
                    "published": published.isoformat(), "source": source})
    return out


def collect(tickers, now=None):
    """Recent headlines from the general feeds plus one Yahoo feed per ticker."""
    now = now or datetime.now(timezone.utc)
    items = []
    for name, url in GENERAL_FEEDS.items():
        items += fetch_rss(url, name)
    for t in tickers:
        items += fetch_rss(TICKER_FEED.format(t.replace("-", ".")), f"Yahoo:{t}")
    cutoff = now - timedelta(hours=MAX_AGE_HOURS)
    seen, fresh = set(), []
    for it in items:
        key = it["title"].lower()
        if key in seen or datetime.fromisoformat(it["published"]) < cutoff:
            continue
        seen.add(key)
        fresh.append(it)
    return fresh


def score(headlines, universe):
    """Ask Claude to score headlines. Returns the headlines with an 'impacts' list."""
    import anthropic

    if not headlines:
        return []
    client = anthropic.Anthropic()
    scored = []
    for i in range(0, len(headlines), 60):
        batch = headlines[i:i + 60]
        listing = "\n".join(f"{n}. [{h['source']}] {h['title']}" for n, h in enumerate(batch))
        resp = client.messages.parse(
            model=MODEL,
            max_tokens=16000,
            output_config={"effort": "low"},
            system=SYSTEM,
            messages=[{"role": "user", "content": f"Headlines:\n{listing}"}],
            output_format=Scores,
        )
        if resp.stop_reason == "refusal" or resp.parsed_output is None:
            print("(news scoring declined or unparsable for this batch; skipped)")
            continue
        by_index = {s.index: s for s in resp.parsed_output.items}
        for n, h in enumerate(batch):
            s = by_index.get(n)
            impacts = []
            if s:
                for imp in s.impacts:
                    t = imp.ticker.upper().replace(".", "-")
                    if t in universe:
                        impacts.append({**imp.model_dump(), "ticker": t})
            scored.append({**h, "reports_past_move": bool(s and s.reports_past_move), "impacts": impacts})
    return scored


def priced_in(direction, day_return, atr_pct):
    """True if today's move already went at least one average daily range in the
    news direction. Chasing that is buying what everyone has already bought."""
    if day_return is None or atr_pct is None:
        return False
    move = day_return if direction == "up" else -day_return
    return move >= atr_pct


def analyse(tickers, universe, day_returns, atr_pcts, now=None):
    """Full pass. Returns (vetoes, alerts, log_rows).
    vetoes: {ticker: reason} for fresh medium/high down news
    alerts: [text] for high-impact items not already priced in"""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return {}, [], []
    headlines = collect(tickers, now)
    scored = score(headlines, universe)
    vetoes, alerts, rows = {}, [], []
    for h in scored:
        for imp in h["impacts"]:
            t = imp["ticker"]
            done = h["reports_past_move"] or priced_in(imp["direction"], day_returns.get(t), atr_pcts.get(t))
            rows.append({"logged": (now or datetime.now(timezone.utc)).isoformat(), "published": h["published"],
                         "source": h["source"], "title": h["title"], "ticker": t,
                         "direction": imp["direction"], "strength": imp["strength"], "priced_in": done})
            if imp["direction"] == "down" and imp["strength"] in ("medium", "high"):
                vetoes[t] = h["title"]
            if imp["strength"] == "high" and not done:
                arrow = "UP" if imp["direction"] == "up" else "DOWN"
                alerts.append(f"News {arrow} {t}: {h['title']} ({h['source']})")
    return vetoes, alerts, rows


def append_log(rows, path):
    if not rows:
        return
    with open(path, "a") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
