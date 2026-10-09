"""Offline tests for the swing sleeve (synthetic prices, fake broker).

    python -m pytest swing/test_swing.py -q      or      python swing/test_swing.py
"""
import os
import sys
from datetime import date

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import news  # noqa: E402
import trader  # noqa: E402
from rules import BULL, BEAR, indicators, trend_state  # noqa: E402

DAYS = pd.bdate_range("2024-01-01", periods=320)


def make_px(paths):
    """paths: {ticker: close array}. Builds OHLCV with a 1% daily range."""
    c = pd.DataFrame(paths, index=DAYS)
    return {"open": c.shift(1).fillna(c), "high": c * 1.01, "low": c * 0.99, "close": c,
            "volume": pd.DataFrame(1e6, index=DAYS, columns=c.columns)}


def uptrend(n=320, start=40.0, daily=0.003):
    return start * np.cumprod(np.full(n, 1 + daily))


def spy_up():
    return pd.Series(uptrend(daily=0.001, start=400), index=DAYS)


def with_dip(path, days=3, drop=0.03):
    p = path.copy()
    for k in range(days):
        p[-days + k] = p[-days - 1] * (1 - drop) ** (k + 1)
    return p


def test_trend_states():
    up, down = uptrend(), uptrend(daily=-0.003, start=90)
    st = trend_state(indicators(make_px({"UP": up, "DN": down}), spy_up()))
    assert st["UP"].iloc[-1] == BULL
    assert st["DN"].iloc[-1] == BEAR


def test_dip_in_strong_stock_is_bought():
    px = make_px({"DIP": with_dip(uptrend()), "FLAT": np.full(320, 50.0)})
    sells, buys, notes = trader.plan({}, {}, px, spy_up(), sleeve_equity=2000, cash=2000)
    assert [b[0] for b in buys] == ["DIP"]
    t, qty, limit, info = buys[0]
    assert qty * limit <= 2000 * 0.10 + 1e-6      # max 10% of the sleeve
    assert "first stop" in info


def test_trend_entry_mode_buys_without_dip():
    px = make_px({"STRONG": uptrend(start=30)})
    assert trader.plan({}, {}, px, spy_up(), 2000, 2000)[1] == []
    os.environ["SWING_ENTRY"] = "trend"
    try:
        assert [b[0] for b in trader.plan({}, {}, px, spy_up(), 2000, 2000)[1]] == ["STRONG"]
    finally:
        os.environ.pop("SWING_ENTRY")


def test_no_buys_when_spy_below_200_day():
    spy = pd.Series(uptrend(daily=-0.002, start=500), index=DAYS)
    px = make_px({"DIP": with_dip(uptrend())})
    _, buys, notes = trader.plan({}, {}, px, spy, 2000, 2000)
    assert buys == [] and any("BELOW" in n for n in notes)


def test_price_above_100_is_skipped():
    px = make_px({"BIG": with_dip(uptrend(start=150))})
    _, buys, _ = trader.plan({}, {}, px, spy_up(), 100000, 100000)
    assert buys == []


def test_first_stop_sells():
    path = uptrend()
    path[-1] = path[-2] * 0.85   # 15% crash, far below entry - 2 ATR
    px = make_px({"X": path})
    held = {"X": {"qty": 3, "entry": path[-2]}}
    sells, _, _ = trader.plan(held, {"X": DAYS[-2].date()}, px, spy_up(), 2000, 0)
    assert sells and sells[0][3] == "first stop hit"


def test_trailing_stop_locks_profit():
    path = uptrend()
    path[-5:] = path[-6] * np.array([0.99, 0.97, 0.95, 0.93, 0.90])
    px = make_px({"X": path})
    held = {"X": {"qty": 3, "entry": path[-80]}}
    sells, _, _ = trader.plan(held, {"X": DAYS[-80].date()}, px, spy_up(), 2000, 0)
    assert sells and sells[0][3].startswith("trailing stop (profit locked)")


def test_news_veto_blocks_buy():
    px = make_px({"DIP": with_dip(uptrend())})
    _, buys, notes = trader.plan({}, {}, px, spy_up(), 2000, 2000, vetoes={"DIP": "Probe launched"})
    assert buys == [] and any("bad news" in n for n in notes)


def test_priced_in():
    assert news.priced_in("up", 0.05, 0.02)
    assert not news.priced_in("up", 0.005, 0.02)
    assert news.priced_in("down", -0.03, 0.02)
    assert not news.priced_in("down", 0.03, 0.02)


def test_news_off_without_key(monkeypatch=None):
    os.environ.pop("ANTHROPIC_API_KEY", None)
    assert news.analyse(["AAPL"], {"AAPL"}, {}, {}) == ({}, [], [])


def test_score_and_analyse_with_mocked_claude():
    import anthropic

    class FakeResp:
        stop_reason = "end_turn"
        parsed_output = news.Scores(items=[
            news.Scored(index=0, reports_past_move=False, impacts=[
                news.Impact(ticker="ACME", direction="down", strength="high", reason="probe")]),
            news.Scored(index=1, reports_past_move=True, impacts=[
                news.Impact(ticker="BOOM", direction="up", strength="high", reason="beat")]),
            news.Scored(index=2, reports_past_move=False, impacts=[
                news.Impact(ticker="NOTUS", direction="up", strength="high", reason="x")])])

    class FakeClient:
        def __init__(self, *a, **k):
            self.messages = self

        def parse(self, **kw):
            assert kw["output_format"] is news.Scores
            return FakeResp()

    heads = [{"title": t, "link": "", "published": "2025-03-03T15:00:00+00:00", "source": "T"}
             for t in ("Regulator opens probe into Acme", "Boom shares jump 9% after beat", "Other")]
    orig_client, orig_collect = anthropic.Anthropic, news.collect
    anthropic.Anthropic, news.collect = FakeClient, lambda tickers, now=None: heads
    os.environ["ANTHROPIC_API_KEY"] = "test"
    try:
        vetoes, alerts, rows = news.analyse(["ACME"], {"ACME", "BOOM"}, {"BOOM": 0.09}, {"BOOM": 0.02})
    finally:
        anthropic.Anthropic, news.collect = orig_client, orig_collect
        os.environ.pop("ANTHROPIC_API_KEY")
    assert vetoes == {"ACME": "Regulator opens probe into Acme"}
    assert len(alerts) == 1 and "DOWN ACME" in alerts[0]          # BOOM is priced in, NOTUS not in universe
    assert {r["ticker"]: r["priced_in"] for r in rows} == {"ACME": False, "BOOM": True}


def test_news_validate_signs_returns(tmp_path=None):
    import json
    import tempfile

    import news_validate
    path = os.path.join(tempfile.mkdtemp(), "log.jsonl")
    with open(path, "w") as f:
        f.write(json.dumps({"logged": f"{DAYS[100].date()}T22:00:00+00:00", "ticker": "UP",
                            "direction": "up", "strength": "high", "priced_in": False}) + "\n")
        f.write(json.dumps({"logged": f"{DAYS[100].date()}T22:00:00+00:00", "ticker": "UP",
                            "direction": "down", "strength": "medium", "priced_in": False}) + "\n")
    px = make_px({"UP": uptrend()})
    orig = news_validate.download_ohlcv
    news_validate.download_ohlcv = lambda tickers, start: px
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            news_validate.main(path)
    finally:
        news_validate.download_ohlcv = orig
    out = buf.getvalue()
    assert "| high, not priced in | 1 | 100% |" in out
    assert "| medium, not priced in | 1 | 0% |" in out


class FakeBroker:
    def __init__(self):
        self.orders, self.cancelled = [], False

    def is_trading_day(self, d):
        return True

    def account(self):
        return {"equity": "100000", "cash": "100000"}

    def positions(self):
        return {}

    def entry_dates(self):
        return {}

    def iex_quotes(self, tickers):
        return {t: {"bs": 3, "as": 5} for t in tickers}

    def cancel_open_orders(self):
        self.cancelled = True

    def limit_order(self, t, qty, side, limit):
        self.orders.append((t, qty, side, round(limit, 2)))


def test_run_places_extended_hours_limit_orders():
    os.environ["SWING_CAPITAL"] = "2000"
    px = make_px({"DIP": with_dip(uptrend())})
    b = FakeBroker()
    trader.run(b, date(2025, 3, 3), dry_run=True, data=(px, spy_up()))
    assert b.orders == [] and not b.cancelled
    trader.run(b, date(2025, 3, 3), dry_run=False, data=(px, spy_up()))
    assert b.cancelled and len(b.orders) == 1 and b.orders[0][2] == "buy"
    assert b.orders[0][1] * b.orders[0][3] <= 200 + 1e-6   # 10% of a $2,000 sleeve
    os.environ.pop("SWING_CAPITAL")


def test_same_account_guard():
    os.environ.update(ALPACA_SWING_KEY_ID="k", ALPACA_SWING_SECRET_KEY="s", ALPACA_KEY_ID="k")
    sys.argv = ["trader.py", "--dry-run"]
    try:
        trader.main()
        raise AssertionError("should have refused")
    except SystemExit as e:
        assert "different Alpaca paper account" in str(e)
    finally:
        for k in ("ALPACA_SWING_KEY_ID", "ALPACA_SWING_SECRET_KEY", "ALPACA_KEY_ID"):
            os.environ.pop(k, None)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
