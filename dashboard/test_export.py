"""Offline tests for the dashboard export (fake broker, no network).

    python -m pytest dashboard/test_export.py -q      or      python dashboard/test_export.py
"""
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import export  # noqa: E402


def order(side, sym, qty, px, day):
    return {"side": side, "symbol": sym, "filled_qty": str(qty), "filled_avg_price": str(px),
            "filled_at": f"{day}T14:30:00Z", "submitted_at": f"{day}T14:29:00Z"}


def ts(day):
    return int(datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp())


class FakeBroker:
    def account(self):
        return {"equity": "10500", "last_equity": "10400", "cash": "4500"}

    def positions(self):
        return [{"symbol": "MSFT", "qty": "10", "avg_entry_price": "400", "current_price": "420",
                 "market_value": "4200", "cost_basis": "4000", "unrealized_pl": "200",
                 "unrealized_plpc": "0.05", "change_today": "0.01"},
                {"symbol": "NVDA", "qty": "15", "avg_entry_price": "120", "current_price": "120",
                 "market_value": "1800", "cost_basis": "1800", "unrealized_pl": "0",
                 "unrealized_plpc": "0", "change_today": "-0.02"}]

    def filled_orders(self):
        return [order("buy", "AAPL", 10, 100, "2026-01-02"),
                order("buy", "AAPL", 10, 110, "2026-01-05"),
                order("sell", "AAPL", 15, 120, "2026-01-12"),   # 10@100 + 5@110 -> +250
                order("buy", "MSFT", 10, 400, "2026-01-13"),
                order("sell", "AAPL", 5, 100, "2026-01-20"),    # 5@110 -> -50
                order("buy", "NVDA", 15, 120, "2026-01-21"),
                order("sell", "XYZ", 3, 50, "2026-01-22")]      # no recorded buy -> ignored

    def history(self):
        return {"timestamp": [ts("2025-12-31"), ts("2026-01-02"), ts("2026-01-05"), ts("2026-01-06")],
                "equity": [0, 10000, 11000, 9900]}

    def spy_closes(self, start):
        assert start == "2026-01-02"
        return {"2026-01-02": 500.0, "2026-01-06": 550.0}


def test_build():
    doc = export.build("monthly", "Quality Momentum", FakeBroker(), "2026-01-22T22:00:00Z")
    m = doc["metrics"]
    assert doc["status"] == "live" and doc["sample"] is False
    assert [r["d"] for r in doc["curve"]] == ["2026-01-02", "2026-01-05", "2026-01-06"]
    assert [r["spy"] for r in doc["curve"]] == [10000, 10000, 11000]  # carried forward, rescaled
    assert m["start_equity"] == 10000 and m["total_pl"] == 500 and m["total_return"] == 5.0
    assert m["today_pl"] == 100
    assert m["max_drawdown"] == -10.0
    assert m["spy_return"] == 10.0
    assert m["trades"] == 2 and m["win_rate"] == 50.0 and m["realized_pl"] == 200
    assert m["profit_factor"] == 5.0
    assert m["unrealized_pl"] == 200 and m["invested"] == 6000
    assert [t["pnl"] for t in doc["trades"]] == [-50, 250]  # newest first
    assert doc["trades"][1]["entry"] == round((1000 + 550) / 15, 2)
    assert doc["positions"][0]["symbol"] == "MSFT" and doc["positions"][0]["since"] == "2026-01-13"
    assert doc["activity"][0]["symbol"] == "XYZ"


def test_missing_keys(tmp_path=None):
    import json
    import tempfile
    out = tmp_path or tempfile.mkdtemp()
    for v in ("ALPACA_KEY_ID", "ALPACA_SECRET_KEY", "ALPACA_SWING_KEY_ID", "ALPACA_SWING_SECRET_KEY"):
        os.environ.pop(v, None)
    sys.argv = ["export.py", "--out", str(out)]
    export.main()
    for name in ("monthly", "swing"):
        with open(os.path.join(out, f"{name}.json")) as f:
            doc = json.load(f)
        assert doc["status"] == "not_connected"


def test_read_only():
    """The export must never be able to change the account."""
    src = open(export.__file__).read()
    for verb in ("s.post", "s.delete", "s.put", "s.patch", "s.request"):
        assert verb not in src


if __name__ == "__main__":
    test_build()
    test_missing_keys()
    test_read_only()
    print("all dashboard export tests passed")
