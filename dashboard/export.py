"""Daily results export for the trading dashboard (read-only, no orders).

Reads each robot's Alpaca PAPER account and writes one JSON file per robot:
  out/monthly.json   Quality Momentum robot (secrets ALPACA_KEY_ID / ALPACA_SECRET_KEY)
  out/swing.json     Swing sleeve robot     (secrets ALPACA_SWING_KEY_ID / ALPACA_SWING_SECRET_KEY)

The files hold balances, open positions, closed trades and the equity curve,
never any keys. .github/workflows/dashboard-export.yml runs this after both
robots and saves the files on the dashboard-data branch, where the dashboard
sync picks them up.

    python export.py --out out
"""
import argparse
import json
import os
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

import requests

PAPER_URL = "https://paper-api.alpaca.markets"
DATA_URL = "https://data.alpaca.markets"

ROBOTS = {
    "monthly": ("Quality Momentum", "ALPACA_KEY_ID", "ALPACA_SECRET_KEY"),
    "swing": ("Swing sleeve", "ALPACA_SWING_KEY_ID", "ALPACA_SWING_SECRET_KEY"),
}


class ReadOnlyAlpaca:
    """GET requests only, so this script can never place or cancel an order."""

    def __init__(self, key, secret):
        self.s = requests.Session()
        self.s.headers.update({"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret})

    def get(self, url, **params):
        r = self.s.get(url, params=params, timeout=30)
        if r.status_code >= 400:
            raise RuntimeError(f"Alpaca GET {url} failed: {r.status_code} {r.text[:200]}")
        return r.json()

    def account(self):
        return self.get(PAPER_URL + "/v2/account")

    def positions(self):
        return self.get(PAPER_URL + "/v2/positions")

    def history(self):
        return self.get(PAPER_URL + "/v2/account/portfolio/history", period="1A", timeframe="1D")

    def filled_orders(self):
        """All filled orders, oldest first."""
        out, after = [], None
        while True:
            params = {"status": "closed", "limit": 500, "direction": "asc"}
            if after:
                params["after"] = after
            page = self.get(PAPER_URL + "/v2/orders", **params)
            out += [o for o in page if float(o.get("filled_qty") or 0) > 0]
            if len(page) < 500:
                return out
            after = page[-1]["submitted_at"]

    def open_orders(self):
        """Orders placed but not filled yet (the robots trade after the close)."""
        return self.get(PAPER_URL + "/v2/orders", status="open", limit=500, direction="asc")

    def trading_days(self, start, end):
        return [d["date"] for d in self.get(PAPER_URL + "/v2/calendar", start=start, end=end)]

    def spy_closes(self, start):
        """{YYYY-MM-DD: close} for SPY from the free IEX feed (benchmark only)."""
        try:
            bars = self.get(DATA_URL + "/v2/stocks/bars", symbols="SPY", timeframe="1Day",
                            start=start, feed="iex", limit=10000).get("bars", {}).get("SPY", [])
            return {b["t"][:10]: float(b["c"]) for b in bars}
        except Exception as e:  # the benchmark is nice to have, never fatal
            print(f"(SPY benchmark unavailable: {e})")
            return {}


    def closes(self, symbols, start):
        """{symbol: [daily closes]} from the free IEX feed, for the small price graphs."""
        out, token = {}, None
        if not symbols:
            return out
        try:
            while True:
                params = {"symbols": ",".join(sorted(symbols)), "timeframe": "1Day", "start": start,
                          "feed": "iex", "limit": 10000}
                if token:
                    params["page_token"] = token
                page = self.get(DATA_URL + "/v2/stocks/bars", **params)
                for sym, bars in (page.get("bars") or {}).items():
                    out.setdefault(sym, []).extend(round(float(b["c"]), 2) for b in bars)
                token = page.get("next_page_token")
                if not token:
                    return out
        except Exception as e:  # graphs are nice to have, never fatal
            print(f"(price history unavailable: {e})")
            return out


def usd_dkk():
    """(rate, date) from the European Central Bank via frankfurter.app, or (None, None)."""
    try:
        r = requests.get("https://api.frankfurter.app/latest", params={"from": "USD", "to": "DKK"}, timeout=15)
        r.raise_for_status()
        j = r.json()
        return round(float(j["rates"]["DKK"]), 4), j.get("date")
    except Exception as e:
        print(f"(USD/DKK rate unavailable: {e})")
        return None, None


SPARK_DAYS = 63  # about three months of trading days


def _f(x, nd=2):
    return round(float(x), nd) if x not in (None, "") else None


def equity_curve(hist, spy):
    """[{d, equity, spy}] from portfolio history; spy is SPY rescaled to the
    starting equity, so both lines share one dollar axis."""
    rows = []
    for ts, eq in zip(hist.get("timestamp") or [], hist.get("equity") or []):
        if eq is None or float(eq) <= 0:
            continue  # days before the account was funded
        rows.append({"d": datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d"),
                     "equity": _f(eq)})
    base_spy, last = None, None
    for r in rows:
        last = spy.get(r["d"], last)
        if last is not None and base_spy is None:
            base_spy = last
        r["spy"] = _f(rows[0]["equity"] * last / base_spy) if base_spy else None
    return rows


def max_drawdown(values):
    peak, worst = None, 0.0
    for v in values:
        peak = v if peak is None else max(peak, v)
        worst = min(worst, v / peak - 1)
    return worst


def closed_trades(orders):
    """Match sells to earlier buys first-in first-out. One row per sell order."""
    lots = defaultdict(deque)
    trades = []
    for o in orders:
        sym, qty, px = o["symbol"], float(o["filled_qty"]), float(o["filled_avg_price"])
        when = o["filled_at"][:10]
        decided = (o.get("submitted_at") or o["filled_at"])[:10]
        if o["side"] == "buy":
            lots[sym].append([qty, px, when])
            continue
        left, cost, opened = qty, 0.0, None
        while left > 1e-9 and lots[sym]:
            lot = lots[sym][0]
            take = min(left, lot[0])
            cost += take * lot[1]
            opened = opened or lot[2]
            lot[0] -= take
            left -= take
            if lot[0] <= 1e-9:
                lots[sym].popleft()
        matched = qty - left
        if matched <= 0:
            continue  # sell with no recorded buy (e.g. position from before this history)
        entry = cost / matched
        pnl = (px - entry) * matched
        days = (datetime.fromisoformat(when) - datetime.fromisoformat(opened)).days
        trades.append({"symbol": sym, "qty": _f(matched, 4), "entry": _f(entry), "exit": _f(px),
                       "pnl": _f(pnl), "pct": _f((px / entry - 1) * 100), "opened": opened,
                       "closed": when, "decided": decided, "days": days})
    return trades


def month_ends(days):
    """Last trading day of each month in a sorted list of YYYY-MM-DD days."""
    ends = {}
    for d in days:
        ends[d[:7]] = d
    return ends


def exit_reason(robot, trade, ends, buy_days):
    """Why a position was sold, worked out from the robot's fixed rules (the
    broker does not record reasons). The robots decide after the close, so we
    use the day the sell order was placed, not the day it filled."""
    d = trade["decided"]
    if robot == "monthly":
        if ends.get(d[:7]) == d:
            if d not in buy_days:
                return "Crash brake: market fell below its 200-day average"
            return "Monthly rotation: dropped out of the top 30 or below its 200-day average"
        return "20% stop-loss"
    if trade["pnl"] > 0:
        return "Trailing stop: profit locked in"
    if trade["days"] >= 84:  # about 60 trading days
        return "Time stop: no progress after 60 trading days"
    return "Stop: first stop, trailing stop or trend break"


def trade_stats(trades):
    wins = [t["pnl"] for t in trades if t["pnl"] > 0]
    losses = [t["pnl"] for t in trades if t["pnl"] <= 0]
    n = len(trades)
    return {
        "trades": n,
        "win_rate": _f(len(wins) / n * 100, 1) if n else None,
        "avg_win": _f(sum(wins) / len(wins)) if wins else None,
        "avg_loss": _f(sum(losses) / len(losses)) if losses else None,
        "profit_factor": _f(sum(wins) / -sum(losses)) if wins and sum(losses) < 0 else None,
        "avg_days": _f(sum(t["days"] for t in trades) / n, 1) if n else None,
        "realized_pl": _f(sum(t["pnl"] for t in trades)),
    }


def build(name, label, broker, now):
    acct = broker.account()
    positions = broker.positions()
    orders = broker.filled_orders()
    hist = broker.history()
    first_ts = next((ts for ts, eq in zip(hist.get("timestamp") or [], hist.get("equity") or [])
                     if eq and float(eq) > 0), None)
    start = (datetime.fromtimestamp(first_ts, timezone.utc).strftime("%Y-%m-%d")
             if first_ts else now[:10])
    curve = equity_curve(hist, broker.spy_closes(start))

    equity, last_equity = float(acct["equity"]), float(acct["last_equity"])
    start_equity = curve[0]["equity"] if curve else equity
    trades = closed_trades(orders)
    today = now[:10]
    horizon = (datetime.fromisoformat(today) + timedelta(days=45)).strftime("%Y-%m-%d")
    days = broker.trading_days(start, horizon)
    ends = month_ends(days)
    buy_days = {(o.get("submitted_at") or o["filled_at"])[:10] for o in orders if o["side"] == "buy"}
    for t in trades:
        t["reason"] = exit_reason(name, t, ends, buy_days)
    next_run = next((d for d in sorted(ends.values()) if d >= today), None) if name == "monthly" \
        else next((d for d in days if d >= today), None)
    pending = [{"symbol": o["symbol"], "side": o["side"], "qty": _f(o.get("qty") or 0, 4),
                "type": o.get("type"), "limit": _f(o.get("limit_price")),
                "placed": (o.get("submitted_at") or "")[:10]} for o in broker.open_orders()]
    spark_start = (datetime.fromisoformat(today) - timedelta(days=100)).strftime("%Y-%m-%d")
    spark = {k: v[-SPARK_DAYS:] for k, v in broker.closes(
        {p["symbol"] for p in positions} | {o["symbol"] for o in pending}, spark_start).items()}
    opened_on = {}
    for o in orders:
        if o["side"] == "buy":
            opened_on[o["symbol"]] = o["filled_at"][:10]

    pos = sorted(({
        "symbol": p["symbol"], "qty": _f(p["qty"], 4), "entry": _f(p["avg_entry_price"]),
        "price": _f(p["current_price"]), "value": _f(p["market_value"]),
        "cost": _f(p["cost_basis"]), "pl": _f(p["unrealized_pl"]),
        "pct": _f(float(p["unrealized_plpc"]) * 100), "today_pct": _f(float(p["change_today"]) * 100),
        "since": opened_on.get(p["symbol"])} for p in positions), key=lambda p: -(p["value"] or 0))

    spy_ret = (curve[-1]["spy"] / curve[0]["spy"] - 1) * 100 if curve and curve[-1].get("spy") and curve[0].get("spy") else None
    metrics = {
        "equity": _f(equity), "cash": _f(acct["cash"]), "start_equity": _f(start_equity),
        "start_date": curve[0]["d"] if curve else start,
        "total_pl": _f(equity - start_equity),
        "total_return": _f((equity / start_equity - 1) * 100) if start_equity else None,
        "today_pl": _f(equity - last_equity),
        "today_return": _f((equity / last_equity - 1) * 100) if last_equity else None,
        "unrealized_pl": _f(sum(p["pl"] or 0 for p in pos)),
        "invested": _f(sum(p["value"] or 0 for p in pos)),
        "max_drawdown": _f(max_drawdown([r["equity"] for r in curve]) * 100) if curve else None,
        "spy_return": _f(spy_ret),
        **trade_stats(trades),
    }
    activity = [{"date": o["filled_at"][:10], "side": o["side"], "symbol": o["symbol"],
                 "qty": _f(o["filled_qty"], 4), "price": _f(o["filled_avg_price"])}
                for o in orders[-40:]][::-1]
    return {"robot": name, "label": label, "status": "live", "sample": False, "updated_at": now,
            "metrics": metrics, "curve": curve, "positions": pos, "pending": pending,
            "next_run": next_run, "spark": spark, "trades": trades[-200:][::-1], "activity": activity}


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", default="out")
    a = p.parse_args()
    os.makedirs(a.out, exist_ok=True)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    failed = False
    rate, rate_date = usd_dkk()
    for name, (label, key_var, secret_var) in ROBOTS.items():
        key, secret = os.environ.get(key_var), os.environ.get(secret_var)
        if not key or not secret:
            doc = {"robot": name, "label": label, "status": "not_connected", "sample": False,
                   "updated_at": now, "note": f"Add the {key_var} and {secret_var} secrets to connect it."}
        else:
            try:
                doc = build(name, label, ReadOnlyAlpaca(key, secret), now)
            except Exception as e:
                failed = True
                doc = {"robot": name, "label": label, "status": "error", "sample": False,
                       "updated_at": now, "note": f"Export failed: {str(e)[:200]}"}
        doc["usd_dkk"], doc["fx_date"] = rate, rate_date
        with open(os.path.join(a.out, f"{name}.json"), "w") as f:
            json.dump(doc, f, separators=(",", ":"))
        print(f"{name}: {doc['status']}")
    if failed:
        raise SystemExit("At least one robot could not be exported (see the files for details).")


if __name__ == "__main__":
    main()
