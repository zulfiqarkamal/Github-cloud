# Trading dashboard export

`export.py` reads both Alpaca **paper** accounts (GET requests only, it cannot trade)
and writes one JSON file per robot: balances, open positions, closed trades
(sells matched to buys first-in first-out), the equity curve and SPY over the same days.

`.github/workflows/dashboard-export.yml` runs it at 22:05 UTC on weekdays, after both
robots, and saves `monthly.json` and `swing.json` on the `dashboard-data` branch.
A daily Claude routine copies those files into the Robot Trading Desk dashboard.

Secrets used (the same ones the robots use): `ALPACA_KEY_ID`, `ALPACA_SECRET_KEY`,
`ALPACA_SWING_KEY_ID`, `ALPACA_SWING_SECRET_KEY`. A robot without keys is reported
as "not connected". Keys never appear in the output files.

Note: this repository is public, so the `dashboard-data` branch (paper balances and
positions, no keys) is public too.

    python dashboard/test_export.py     # offline tests
    python dashboard/export.py --out out
