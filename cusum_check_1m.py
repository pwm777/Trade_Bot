#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Incremental (online) CUSUM integrity checker for `candles_1m` in a SQLite DB.

The checker recomputes CUSUM strictly incrementally (bar-by-bar), reproducing an
"online" regime, and compares with stored columns: cusum, cusum_state, cusum_zscore,
cusum_conf, cusum_reason (if present).

USAGE (examples):
  python cusum_check_1m.py --db data/market_data.sqlite --table candles_1m --symbols BTCUSDT ETHUSDT \
      --mode two-sided --signal deltaclose --k 0.0 --h 5.0 --tol 1e-9 --out out_reports
  python cusum_check_1m.py --db data/market_data.sqlite --symbols BTCUSDT --start 2025-10-01 --end 2025-10-07 \
      --mode one-sided-up --signal logret --k 0.0001 --h 0.01 --tol 1e-8

Outputs:
  - <out>/cusum_issues.csv     : detailed issues per symbol/timestamp (first ~1e6 rows max)
  - <out>/cusum_summary.json   : overall summary and counts by issue type
"""

from __future__ import annotations

import argparse
import json
import sys
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import numpy as np
import pandas as pd
default_db = Path("data/market_data.sqlite")
default_table = "candles_1m"
default_symbol = "ETHUSDT"

# ----------------------------- CLI / Config ----------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Incremental CUSUM checker for candles_1m")
    p.add_argument("--db", type=str, required=True, help="Path to SQLite DB (e.g., data/market_data.sqlite)")
    p.add_argument("--table", type=str, default="candles_1m", help="Table name (default: candles_1m)")
    p.add_argument("--symbols", nargs="*", default=None, help="Symbols to check (default: all)")
    p.add_argument("--start", type=str, default=None, help="Start date (YYYY-MM-DD) UTC filter by ts")
    p.add_argument("--end", type=str, default=None, help="End date (YYYY-MM-DD) UTC filter by ts (inclusive)")
    p.add_argument("--limit", type=int, default=250_000, help="Max rows per symbol (take most recent N if exceeded)")
    p.add_argument("--mode", type=str, default="two-sided",
                   choices=["two-sided", "one-sided-up", "one-sided-down"],
                   help="CUSUM mode")
    p.add_argument("--signal", type=str, default="deltaclose",
                   choices=["deltaclose", "logret", "close"],
                   help="Input signal x_t definition")
    p.add_argument("--k", type=float, default=0.0, help="CUSUM drift (k)")
    p.add_argument("--h", type=float, default=1.0, help="CUSUM threshold (h)")
    p.add_argument("--reset", type=str, default="at_hit", choices=["at_hit", "next_bar"],
                   help="Reset moment: at_hit (reset immediately) or next_bar (reset on the bar after hit)")
    p.add_argument("--tol", type=float, default=1e-9, help="Tolerance for numeric equality vs stored cusum")
    p.add_argument("--out", type=str, default="cusum_reports", help="Output directory")
    return p.parse_args()


# ----------------------------- Utilities -------------------------------------

def human_time(ts: Optional[int]) -> Optional[str]:
    if ts is None or (isinstance(ts, float) and (np.isnan(ts) or np.isinf(ts))):
        return None
    try:
        # Совместимо со всеми версиями Python
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()
    except Exception:
        return None


def to_unix_day(date_str: str, end: bool = False) -> int:
    """Convert YYYY-MM-DD to unix seconds (start or end of day in UTC)."""
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    if end:
        dt = dt.replace(hour=23, minute=59, second=59, microsecond=0)
    return int(dt.replace(tzinfo=timezone.utc).timestamp())


def has_table(con: sqlite3.Connection, table: str) -> bool:
    cur = con.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
    return cur.fetchone() is not None


def get_columns(con: sqlite3.Connection, table: str) -> List[str]:
    cur = con.cursor()
    cur.execute(f"PRAGMA table_info({table})")
    return [r[1] for r in cur.fetchall()]


def read_symbols(con: sqlite3.Connection, table: str, only: Optional[List[str]]) -> List[str]:
    cur = con.cursor()
    if only:
        q = f"SELECT DISTINCT symbol FROM {table} WHERE symbol IN ({','.join(['?']*len(only))}) ORDER BY symbol"
        cur.execute(q, only)
    else:
        q = f"SELECT DISTINCT symbol FROM {table} ORDER BY symbol"
        cur.execute(q)
    return [r[0] for r in cur.fetchall()]


def load_symbol_df(con: sqlite3.Connection, table: str, symbol: str,
                   start_ts: Optional[int], end_ts: Optional[int], limit: int) -> pd.DataFrame:
    where = ["symbol = ?"]
    params: List[Any] = [symbol]
    if start_ts is not None:
        where.append("ts >= ?")
        params.append(start_ts)
    if end_ts is not None:
        where.append("ts <= ?")
        params.append(end_ts)
    where_clause = " AND ".join(where)

    base_cols = [
        "symbol", "ts", "close", "cusum", "cusum_state",
        "cusum_zscore", "cusum_conf", "cusum_reason", "finalized"
    ]
    col_list = ", ".join(base_cols)

    cur = con.cursor()
    cur.execute(f"SELECT COUNT(*) FROM {table} WHERE {where_clause}", params)
    n = cur.fetchone()[0]

    if n > limit:
        # pull the most recent N rows
        df = pd.read_sql_query(
            f"""
            SELECT {col_list}
            FROM {table}
            WHERE {where_clause}
            ORDER BY ts DESC
            LIMIT {limit}
            """,
            con, params=params
        ).sort_values("ts")
    else:
        df = pd.read_sql_query(
            f"""
            SELECT {col_list}
            FROM {table}
            WHERE {where_clause}
            ORDER BY ts ASC
            """,
            con, params=params
        )

    return df.reset_index(drop=True)


# -------------------------- CUSUM Recompute (Online) -------------------------

@dataclass
class CusumStep:
    ts: int
    x: float           # input signal value used
    s_up: float        # running S+ after update (two-sided/up mode)
    s_dn: float        # running S- after update (two-sided/down mode)
    state: str         # derived state: 'up','down','flat' (or 'NA' if undefined)
    hit: Optional[str] # 'up' or 'down' if threshold was hit, else None


def compute_signal(prev_close: Optional[float], close: float, mode: str) -> float:
    if mode == "deltaclose":
        if prev_close is None or np.isnan(prev_close):
            return 0.0
        return float(close - prev_close)
    elif mode == "logret":
        if prev_close is None or prev_close <= 0 or close <= 0:
            return 0.0
        return float(np.log(close / prev_close))
    elif mode == "close":
        return float(close)
    else:
        raise ValueError(f"Unknown signal mode: {mode}")


def recompute_cusum_online(closes: Iterable[float], tss: Iterable[int],
                           mode: str, k: float, h: float,
                           scheme: str) -> List[CusumStep]:
    """
    Strictly incremental CUSUM:
      two-sided:
        S+_t = max(0, S+_{t-1} + (x_t - k))
        S-_t = min(0, S-_{t-1} + (x_t + k))
        hit if S+_t >= h  => 'up'
             if S-_t <= -h => 'down'
      one-sided-up:
        S_t = max(0, S_{t-1} + (x_t - k)); hit if S_t >= h
      one-sided-down:
        S_t = min(0, S_{t-1} + (x_t + k)); hit if S_t <= -h

    scheme: 'at_hit' (reset immediately on the same bar) or 'next_bar' (reset on next bar).
    """
    prev_close = None
    s_up = 0.0
    s_dn = 0.0
    out: List[CusumStep] = []

    pending_reset = False  # for next_bar scheme

    for close, ts in zip(closes, tss):
        # optional reset if previous bar hit and scheme=next_bar
        if pending_reset:
            s_up, s_dn = 0.0, 0.0
            pending_reset = False

        x_t = compute_signal(prev_close, close, mode)
        prev_close = close

        hit: Optional[str] = None

        if mode not in ("deltaclose", "logret", "close"):
            raise ValueError("Invalid signal mode internal")

        # update
        # two-sided default
        s_up = max(0.0, s_up + (x_t - k))
        s_dn = min(0.0, s_dn + (x_t + k))

        # determine hits depending on active side(s)
        # in one-sided modes, we treat the other side as 0 (not active)
        if MODE_ACTIVE == "two":
            if s_up >= h:
                hit = "up"
            elif s_dn <= -h:
                hit = "down"
        elif MODE_ACTIVE == "up":
            s_dn = 0.0
            if s_up >= h:
                hit = "up"
        else:  # "down"
            s_up = 0.0
            if s_dn <= -h:
                hit = "down"

        # reset logic
        if hit is not None:
            if scheme == "at_hit":
                # reset right away after registering the hit on this bar
                s_up, s_dn = 0.0, 0.0
            else:
                # mark to reset on the next bar
                pending_reset = True

        # state label for comparison convenience
        if abs(s_up) < 1e-18 and abs(s_dn) < 1e-18:
            state = "flat"
        elif s_up > 0 and abs(s_dn) < 1e-18:
            state = "up"
        elif s_dn < 0 and abs(s_up) < 1e-18:
            state = "down"
        else:
            # shouldn't happen if logic is consistent, still mark as mixed
            state = "mixed"

        out.append(CusumStep(ts=int(ts), x=x_t, s_up=s_up, s_dn=s_dn, state=state, hit=hit))

    return out


# NOTE: MODE_ACTIVE is global to simplify branch logic inside recompute (set in main())
MODE_ACTIVE = "two"  # 'two' | 'up' | 'down'


# ------------------------------ Main Routine ---------------------------------

def main():
    # --- AUTO DEFAULT MODE ---
    if len(sys.argv) == 1:
        print("🚀 Автоматический запуск CUSUM-проверки (ETHUSDT, two-sided, deltaclose)")
        from types import SimpleNamespace
        args = SimpleNamespace(
            db="data/market_data.sqlite",
            table="candles_1m",
            symbols=["ETHUSDT"],
            start=None,
            end=None,
            limit=250_000,
            mode="two-sided",
            signal="deltaclose",
            k=0.0,
            h=5.0,
            reset="at_hit",
            tol=1e-9,
            out="cusum_reports",
        )
    else:
        args = parse_args()

    out_dir = Path(args.out); out_dir.mkdir(parents=True, exist_ok=True)

    db_path = Path(args.db)
    if not db_path.exists():
        print(json.dumps({"error": f"DB not found: {db_path}"}, ensure_ascii=False, indent=2))
        return

    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row

    if not has_table(con, args.table):
        print(json.dumps({"error": f"Table not found: {args.table}"}, ensure_ascii=False, indent=2))
        return

    cols = get_columns(con, args.table)
    required = {"symbol", "ts", "close", "cusum", "cusum_state"}
    missing = sorted(list(required - set(cols)))
    if missing:
        print(json.dumps({"error": f"Missing columns in {args.table}: {missing}"}, ensure_ascii=False, indent=2))
        return

    # map mode to active sides
    global MODE_ACTIVE
    if args.mode == "two-sided":
        MODE_ACTIVE = "two"
    elif args.mode == "one-sided-up":
        MODE_ACTIVE = "up"
    else:
        MODE_ACTIVE = "down"

    start_ts = to_unix_day(args.start, end=False) if args.start else None
    end_ts = to_unix_day(args.end, end=True) if args.end else None

    symbols = read_symbols(con, args.table, args.symbols)
    if not symbols:
        print(json.dumps({"error": "No symbols found to process"}, ensure_ascii=False, indent=2))
        return

    all_issues: List[Dict[str, Any]] = []
    summary: Dict[str, Any] = {"symbols": [], "params": vars(args)}

    for sym in symbols:
        df = load_symbol_df(con, args.table, sym, start_ts, end_ts, args.limit)
        if df.empty:
            continue
        # sanity: order by ts strictly asc
        df = df.sort_values("ts").reset_index(drop=True)

        closes = df["close"].astype(float).tolist()
        tss = df["ts"].astype(int).tolist()

        steps = recompute_cusum_online(
            closes=closes, tss=tss,
            mode=args.signal, k=float(args.k), h=float(args.h),
            scheme=args.reset
        )

        # Compare vs DB: numeric cusum and qualitative 'state'
        tol = float(args.tol)
        mism_num = 0
        mism_state = 0
        resets = 0
        issues_local: List[Dict[str, Any]] = []

        for i, st in enumerate(steps):
            ts = int(tss[i])
            rec_cusum = float(df.at[i, "cusum"]) if pd.notna(df.at[i, "cusum"]) else None
            rec_state = str(df.at[i, "cusum_state"]) if pd.notna(df.at[i, "cusum_state"]) else "NA"

            # choose comparable numeric CUSUM depending on active side(s)
            if MODE_ACTIVE == "two":
                # use magnitude with sign preference (up positive, down negative)
                alg_val = st.s_up if abs(st.s_up) >= abs(st.s_dn) else st.s_dn
            elif MODE_ACTIVE == "up":
                alg_val = st.s_up
            else:
                alg_val = st.s_dn

            # numeric compare
            if rec_cusum is None or (not np.isfinite(rec_cusum)):
                issues_local.append({
                    "symbol": sym, "ts": ts, "time": human_time(ts),
                    "issue": "NULL or non-finite cusum in DB",
                    "details": ""
                })
                mism_num += 1
            else:
                if not np.isfinite(alg_val) or abs(rec_cusum - alg_val) > tol:
                    issues_local.append({
                        "symbol": sym, "ts": ts, "time": human_time(ts),
                        "issue": "CUSUM numeric mismatch",
                        "details": f"db={rec_cusum:.12g} vs alg={alg_val:.12g} (tol={tol})"
                    })
                    mism_num += 1

            # state compare
            # normalize db states to small set for comparison
            s_db = rec_state.strip().lower()
            if s_db in ("up", "long", "pos", "positive"):
                s_db = "up"
            elif s_db in ("down", "short", "neg", "negative"):
                s_db = "down"
            elif s_db in ("flat", "hold", "neutral", "na", ""):
                s_db = "flat"
            # algorithmic state is already one of {"up","down","flat","mixed"}
            s_alg = st.state if st.state in ("up","down","flat") else "flat"
            if s_db != s_alg:
                issues_local.append({
                    "symbol": sym, "ts": ts, "time": human_time(ts),
                    "issue": "CUSUM state mismatch",
                    "details": f"db={rec_state} vs alg={s_alg}"
                })
                mism_state += 1

            if st.hit is not None:
                resets += 1

        # Monotonicity checks inside segments (based on recomputed sequence)
        # split by sign/state transitions
        alg_seq = [st.s_up if MODE_ACTIVE!="down" else st.s_dn for st in steps]
        sign_seq = np.sign(alg_seq)
        seg_start = np.zeros(len(alg_seq), dtype=bool)
        if len(seg_start) > 0:
            seg_start[0] = True
        for i in range(1, len(alg_seq)):
            if sign_seq[i] != sign_seq[i-1] or (steps[i-1].hit is not None):
                seg_start[i] = True
        idxs = list(np.where(seg_start)[0]) + [len(alg_seq)]
        for a, b in zip(idxs[:-1], idxs[1:]):
            seg = alg_seq[a:b]
            if len(seg) <= 2:
                continue
            if seg[0] >= 0:
                dec = np.where(np.diff(seg) < -1e-12)[0]
                if dec.size:
                    i0 = a + dec[0] + 1
                    issues_local.append({
                        "symbol": sym, "ts": int(tss[i0]), "time": human_time(tss[i0]),
                        "issue": "Monotonicity violation in positive segment",
                        "details": f"Δ={seg[dec[0]+1]-seg[dec[0]]:.6g}"
                    })
            else:
                inc = np.where(np.diff(seg) > 1e-12)[0]
                if inc.size:
                    i0 = a + inc[0] + 1
                    issues_local.append({
                        "symbol": sym, "ts": int(tss[i0]), "time": human_time(tss[i0]),
                        "issue": "Monotonicity violation in negative segment",
                        "details": f"Δ={seg[inc[0]+1]-seg[inc[0]]:.6g}"
                    })

        # Aggregate
        all_issues.extend(issues_local)
        summary["symbols"].append({
            "symbol": sym,
            "rows": int(len(df)),
            "resets_detected": int(resets),
            "numeric_mismatches": int(mism_num),
            "state_mismatches": int(mism_state)
        })

    # Save outputs
    issues_df = pd.DataFrame(all_issues, columns=["symbol","ts","time","issue","details"])
    issues_csv = out_dir / "cusum_issues.csv"
    issues_df.to_csv(issues_csv, index=False, encoding="utf-8")

    summary["counts_by_issue"] = issues_df.groupby(["issue"]).size().reset_index(name="count").to_dict(orient="records") if not issues_df.empty else []
    summary_path = out_dir / "cusum_summary.json"
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    print(json.dumps({
        "saved": {
            "issues_csv": str(issues_csv),
            "summary_json": str(summary_path)
        },
        "symbols_processed": [s["symbol"] for s in summary["symbols"]],
        "params": summary["params"]
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
