"""Check whether the news scores predicted anything, before trusting them.

Reads news_log.jsonl (written by the robot every day), then for each scored
headline measures the stock's return from the NEXT session's open to the close
5 sessions later, signed in the predicted direction (positive = the call was right).

    python news_validate.py news_log.jsonl

Rule of thumb before letting news trigger buys (it only vetoes today):
at least 200 unpriced medium/high calls, average signed return clearly above the
0.2% round-trip cost, and a t-stat above 2. Until then it is noise.
"""
import json
import sys

import numpy as np
import pandas as pd

from rules import download_ohlcv

HOLD = 5


def main(path):
    rows = [json.loads(l) for l in open(path) if l.strip()]
    if not rows:
        sys.exit("The log is empty.")
    df = pd.DataFrame(rows)
    df["day"] = pd.to_datetime(df["logged"], utc=True).dt.tz_convert("America/New_York").dt.normalize().dt.tz_localize(None)
    df = df.drop_duplicates(["ticker", "day", "direction"])
    start = (df["day"].min() - pd.Timedelta(days=5)).strftime("%Y-%m-%d")
    px = download_ohlcv(sorted(df["ticker"].unique()), start)
    o, c = px["open"], px["close"]
    out = []
    for r in df.itertuples():
        if r.ticker not in c.columns:
            continue
        i = c.index.searchsorted(r.day, side="right")   # next session after the log day
        if i + HOLD >= len(c.index):
            continue
        entry, exit_ = o[r.ticker].iloc[i], c[r.ticker].iloc[i + HOLD]
        if not (np.isfinite(entry) and np.isfinite(exit_)):
            continue
        ret = exit_ / entry - 1
        out.append({**r._asdict(), "signed": ret if r.direction == "up" else -ret})
    res = pd.DataFrame(out)
    if res.empty:
        sys.exit("Not enough history yet: each call needs 6 more trading days.")
    print(f"{len(res)} scored calls with a full {HOLD}-day outcome\n")
    print("| Group | Calls | Right direction | Avg signed return | t-stat |")
    print("|---|---|---|---|---|")
    for (strength, priced), g in res.groupby(["strength", "priced_in"]):
        t = g["signed"].mean() / g["signed"].std() * np.sqrt(len(g)) if len(g) > 1 else np.nan
        print(f"| {strength}, {'already priced in' if priced else 'not priced in'} | {len(g)} "
              f"| {(g['signed'] > 0).mean() * 100:.0f}% | {g['signed'].mean() * 100:+.2f}% | {t:.1f} |")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "news_log.jsonl")
