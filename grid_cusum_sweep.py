import os
import sqlite3
import argparse
import numpy as np
import pandas as pd
from iqts_standards import Direction

def cusum_online_delta_closes_with_z(
    closes: pd.Series,
    normalize_window: int = 50,
    eps: float = 0.5,
    h: float = 0.5,
    z_to_conf: float = 1.0,
    vol_adaptive: bool = True,
    absz_quantile: float | None = None  # None или число в (0,1), например 0.80
):
    """
    Онлайн-CUSUM по Δclose без look-ahead.
    Возвращает:
      s     : накопитель
      z     : z-score (нормализация по прошлому окну)
      state : 1=BUY, 2=SELL, 0=HOLD
      conf  : |z| * z_to_conf
    Параметры:
      vol_adaptive: порог k_t = h * rolling_sigma(Δclose) (если False -> k_t = h)
      absz_quantile: если задан (например, 0.80), то оставляем только топ-q по |z|
    """
    closes = closes.astype(float)
    diffs = closes.diff().fillna(0.0)

    # Порог k_t
    if vol_adaptive:
        roll_sigma = diffs.rolling(normalize_window, min_periods=normalize_window) \
                          .std(ddof=0).shift(1)
        k = (h * roll_sigma).fillna(0.0).to_numpy()
    else:
        k = np.full(len(diffs), float(h), dtype=float)

    # Онлайн-накопитель CUSUM
    s_up = 0.0
    s_dn = 0.0
    vals = []
    diffs_np = diffs.to_numpy()
    for x, k_i in zip(diffs_np, k):
        s_up = max(0.0, s_up + x - k_i)
        s_dn = min(0.0, s_dn + x + k_i)
        vals.append(s_up if abs(s_up) >= abs(s_dn) else s_dn)
    s = pd.Series(vals, index=closes.index, dtype=float)

    # Нормализация (анти look-ahead: shift(1))
    roll = s.rolling(normalize_window, min_periods=normalize_window)
    mean = roll.mean().shift(1)
    std  = roll.std(ddof=0).shift(1).replace(0.0, np.nan)
    z = ((s - mean) / std).fillna(0.0)

    # Базовая классификация: 1=BUY, 2=SELL, 0=HOLD
    state = pd.Series(
        np.where(z > eps, 1, np.where(z < -eps, 2, 0)).astype(np.int8),
        index=s.index
    )

    # (опционально) Фильтр сильных сигналов по |z| (квантиль)
    if absz_quantile is not None and 0.0 < float(absz_quantile) < 1.0:
        abs_z = z.abs()
        thr = np.quantile(abs_z.values, float(absz_quantile))
        state = state.where(abs_z >= thr, 0)

    conf = z.abs() * float(z_to_conf)
    return s, z, state, conf


def load_5m(con, symbol):
    df = pd.read_sql_query(
        "SELECT ts, open, high, low, close FROM candles_5m WHERE symbol=? ORDER BY ts ASC",
        con, params=[symbol]
    )
    df = df.dropna(subset=["ts","open","high","low","close"]).reset_index(drop=True)
    return df

def backtest_fixed_horizon(df, state, hold_bars=12, fee_bps=2.0):
    """
    Вход на следующей свече по open[t+1], выход через hold_bars по close[t+hold].
    fee_bps — комиссия (bps) на вход и на выход (будет применена дважды).
    Возвращает DataFrame сделок.
    """
    fee = fee_bps / 10000.0
    entries = []
    n = len(df)
    for t in range(n - hold_bars - 1):
        sig = state.iat[t]
        if sig == 0:
            continue
        entry_idx = t + 1
        exit_idx  = t + hold_bars
        entry_px = float(df["open"].iat[entry_idx])
        exit_px  = float(df["close"].iat[exit_idx])
        if sig == 1:  # LONG
            gross = (exit_px / entry_px) - 1.0
        else:         # SELL (short)
            gross = (entry_px / exit_px) - 1.0
        net = gross - 2.0 * fee
        entries.append((df["ts"].iat[t], sig, entry_idx, exit_idx, entry_px, exit_px, gross, net))
    trades = pd.DataFrame(entries, columns=["ts_sig","dir","i_entry","i_exit","px_in","px_out","gross","net"])
    return trades

def eval_trades(trades: pd.DataFrame):
    if trades.empty:
        return dict(signals=0, winrate=0.0, mean_pnl=0.0, total_pnl=0.0, avg_win=0.0, avg_loss=0.0, pf=0.0)
    wins = trades["net"] > 0
    signals = len(trades)
    winrate = wins.mean()
    mean_pnl = trades["net"].mean()
    total_pnl = trades["net"].sum()
    avg_win = trades.loc[wins, "net"].mean() if wins.any() else 0.0
    avg_loss = trades.loc[~wins, "net"].mean() if (~wins).any() else 0.0
    gross_pos = trades.loc[wins, "net"].sum()
    gross_neg = -trades.loc[~wins, "net"].sum()
    pf = (gross_pos / gross_neg) if gross_neg > 0 else np.inf
    return dict(signals=signals, winrate=winrate, mean_pnl=mean_pnl, total_pnl=total_pnl,
                avg_win=avg_win, avg_loss=avg_loss, pf=pf)

def backtest_triple_barrier(
    df: pd.DataFrame,
    state: pd.Series,
    sigma_window: int = 48,
    tp_sigma: float = 0.6,
    sl_sigma: float = 0.5,
    timeout_bars: int = 12,
    fee_bps: float = 2.0,
    cooldown: int = 6
) -> pd.DataFrame:
    """
    Triple-Barrier:
      - σ считается по лог-доходностям (rolling std) и сдвигается на 1 бар (anti-lookahead)
      - уровни TP/SL в ЦЕНАХ ставятся логнормально: px_in * exp(± k * σ)
      - вход: на следующем баре после сигнала по open[t+1]
      - выход: первое достижение TP/SL по high/low, иначе таймаут по close[t+timeout]
      - комиссии: fee_bps на вход и fee_bps на выход
      - запрет overlap через cooldown баров после выхода
    Возвращает DataFrame трейдов.
    """
    fee = fee_bps / 10000.0

    # σ по прошлому окну (anti-lookahead) + без деприкейта
    ret = np.log(df["close"]).diff()
    sig = ret.rolling(sigma_window, min_periods=sigma_window).std(ddof=0).shift(1)
    sig = sig.bfill().ffill()

    entries = []
    last_exit = -10**9
    n = len(df)

    for t in range(n - timeout_bars - 1):
        if t < last_exit + cooldown:
            continue

        d = int(state.iat[t])  # 0/1/2  (1=BUY long, 2=SELL short)
        if d == 0:
            continue

        entry_i = t + 1
        px_in = float(df["open"].iat[entry_i])
        s = float(sig.iat[entry_i])

        # логнормальные множители для уровней
        up_mult = float(np.exp(tp_sigma * s))
        dn_mult = float(np.exp(-sl_sigma * s))

        if d == Direction.BUY:  # LONG
            tp_price = px_in * up_mult      # вверх = прибыль
            sl_price = px_in * dn_mult      # вниз  = убыток
        else:       # SHORT (2)
            # для шорта TP — это движение вниз (exp(-tp*σ)), SL — вверх (exp(+sl*σ))
            tp_price = px_in * dn_mult
            sl_price = px_in * up_mult

        hit = None
        exit_i = entry_i

        # Пробег по барам до таймаута
        end_i = min(n - 1, entry_i + timeout_bars)
        for k in range(entry_i, end_i + 1):
            hi = float(df["high"].iat[k])
            lo = float(df["low"].iat[k])

            if d == 1:  # LONG
                if hi >= tp_price:
                    hit, exit_i = "tp", k
                    break
                if lo <= sl_price:
                    hit, exit_i = "sl", k
                    break
            else:       # SHORT
                if lo <= tp_price:          # цена упала до TP-уровня
                    hit, exit_i = "tp", k
                    break
                if hi >= sl_price:          # цена выросла до SL-уровня
                    hit, exit_i = "sl", k
                    break

        # Если не сработало ни TP, ни SL — выходим по таймауту
        if hit is None:
            exit_i = entry_i + timeout_bars
            hit = "to"

        px_out = float(df["close"].iat[exit_i])

        # Доходность сделки (net после обеих комиссий)
        if d == 1:   # long
            gross = (px_out / px_in) - 1.0
        else:        # short
            gross = (px_in / px_out) - 1.0

        net = gross - 2.0 * fee

        entries.append((
            df["ts"].iat[t], d, entry_i, exit_i, px_in, px_out, gross, net, hit
        ))
        last_exit = exit_i

    return pd.DataFrame(
        entries,
        columns=["ts_sig", "dir", "i_entry", "i_exit", "px_in", "px_out", "gross", "net", "exit"]
    )


def main():
    ap = argparse.ArgumentParser("Grid sweep CUSUM (eps, h)")
    ap.add_argument("--db", default=os.path.abspath("data/market_data.sqlite"))
    ap.add_argument("--symbol", default="ETHUSDT")
    ap.add_argument("--norm", type=int, default=50, help="normalize_window")
    ap.add_argument("--hold", type=int, default=12, help="выход через N баров (5м бары → 12=час)")
    ap.add_argument("--fee", type=float, default=2.0, help="комиссия в bps на вход и выход")
    ap.add_argument("--eps", default="0.4,0.5,0.6,0.8,1.0", help="список eps через запятую")
    ap.add_argument("--h",   default="0.3,0.5,0.7,0.9,1.2", help="список h через запятую")
    ap.add_argument("--fixed-k", action="store_true", help="использовать фиксированный порог k=h (иначе k=h*σ)")
    args = ap.parse_args()

    con = sqlite3.connect(args.db)
    try:
        df = load_5m(con, args.symbol)
    finally:
        con.close()

    if df.empty:
        print("❌ Нет данных из candles_5m"); return

    eps_list = [float(x) for x in args.eps.split(",")]
    h_list   = [float(x) for x in args.h.split(",")]

    rows = []
    for eps in eps_list:
        for h in h_list:
            s, z, state, conf = cusum_online_delta_closes_with_z(
                df["close"], normalize_window=args.norm, eps=eps, h=h,
                z_to_conf=1.0, vol_adaptive=(not args.fixed_k),
                absz_quantile=0.90  # топ-10% по силе
            )
            if "adx_14" in df.columns:
                trend_mask = (df["adx_14"].fillna(0) >= 15)  # порог тренда
                state = state.where(trend_mask, 0)

            # === Фильтр сильных сигналов CUSUM (топ-20% по |z|) ===
            abs_z = z.abs()
            thr = np.quantile(abs_z.values, 0.80)  # порог силы сигнала (80-й перцентиль)
            state = state.where(abs_z >= thr, 0)

            trades = backtest_triple_barrier(
                df, state,
                sigma_window=72,  # более сглаженная σ
                tp_sigma=1.4,  # дальше тейк
                sl_sigma=0.3,  # ближе стоп
                timeout_bars=24,  # больше времени
                fee_bps=args.fee,
                cooldown=10  # меньше оверлапа
            )

            m = eval_trades(trades)
            rows.append({
                "eps": eps, "h": h, "vol_adaptive": (not args.fixed_k),
                "signals": m["signals"], "winrate": round(100*m["winrate"], 2),
                "mean_pnl_%": round(100*m["mean_pnl"], 3),
                "total_pnl_%": round(100*m["total_pnl"], 2),
                "avg_win_%": round(100*m["avg_win"], 3),
                "avg_loss_%": round(100*m["avg_loss"], 3),
                "pf": round(m["pf"], 3)
            })

    res = pd.DataFrame(rows).sort_values(["total_pnl_%","pf","winrate"], ascending=[False, False, False])
    # печатаем топ-15
    print("\n=== TOP-15 configs by total_pnl_% ===")
    print(res.head(15).to_string(index=False))

    # и сводка по winrate
    print("\n=== TOP-15 configs by winrate ===")
    print(res.sort_values(["winrate","pf"], ascending=[False, False]).head(15).to_string(index=False))

if __name__ == "__main__":
    main()
