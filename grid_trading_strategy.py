"""
grid_backtest.py
================
Standalone backtester for price_grid_strategy.py.

WHY THIS EXISTS
---------------
You asked "is the current config better?" That can't be answered by reading the
config — only by replaying real candles. This script does that and compares the
three slope-gate settings (off / rising / falling) head to head, holding
everything else fixed (TP 450, SL 300, 6 levels, ADX range gate < 35).

RUN IT ON YOUR OWN MACHINE
--------------------------
It fetches candles from Delta's PUBLIC history endpoint. Run where Delta is
reachable (your EC2 box / laptop), not in a sandbox:

    pip install requests pandas pandas_ta
    python3 grid_backtest.py

If you hit a host error, switch DELTA_HOST below between the global and India
domains — your live setup looks India-based, so that's the default.

IMPORTANT LIMITATIONS (read before trusting the numbers)
--------------------------------------------------------
1. A live grid trades on a ~3s price tick. A backtest only has candle OHLC.
   We approximate intrabar behaviour using each 15m candle's HIGH and LOW:
     - a level is "filled" if the candle's range touched its price
     - TP / SL is "hit" if the candle's range touched the target
2. SAME-BAR AMBIGUITY: if one candle touches BOTH the TP and the SL of an open
   position, we cannot know which came first from OHLC alone. We resolve this
   CONSERVATIVELY: assume the SL was hit first. This makes results pessimistic,
   never rosy — better than the reverse.
3. Multiple level fills inside one bar are processed at that bar's relevant
   extreme, not at a live tick price. Treat fill prices as approximate.
4. Fees use a simple taker model matching the live TAKER_FEE.

Net: use this to RANK the three variants against each other, not as a promise of
live PnL. The ranking is far more trustworthy than the absolute number.
"""

import sys
import time
from datetime import datetime, timedelta, timezone

import requests
import pandas as pd
import pandas_ta as ta

# ================= CONFIG (mirrors the live bot) =================

DELTA_HOST = "https://api.india.delta.exchange"   # swap to https://api.delta.exchange if needed
SYMBOL = "BTCUSD"
DAYS = 15

# Strategy params — kept identical to price_grid_strategy.py
GRID_STEP = 300
GRID_LEVELS = 20
GRID_TP = GRID_STEP * 1.5          # 450
GRID_SL = GRID_STEP                # 300
GRID_BOUNDARY = GRID_STEP * GRID_LEVELS

ADX_TIMEFRAME = "15m"              # we backtest on 15m bars throughout
ADX_PERIOD = 14
ADX_THRESHOLD = 35.0
ADX_MODE = "range"                 # < threshold = calm = grid-friendly
ADX_AVG_PERIOD = 5

CONTRACT_SIZE = 0.001
QTY = 1000
TAKER_FEE = 0.0005
START_BALANCE = 10000

DAILY_TARGET = 200000
MAX_DAILY_LOSS = -100000

# Slope variants we compare. This is the whole point of the run.
SLOPE_VARIANTS = ["off", "rising", "falling"]


# ================= DATA =================

def fetch_candles(symbol, resolution, days):
    """Pull `days` of candles from Delta's public history endpoint."""
    end = int(time.time())
    start = end - days * 24 * 3600
    url = f"{DELTA_HOST}/v2/history/candles"
    params = {"resolution": resolution, "symbol": symbol,
              "start": start, "end": end}
    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()
    payload = r.json()
    rows = payload.get("result", payload) if isinstance(payload, dict) else payload
    df = pd.DataFrame(rows)
    if df.empty:
        raise RuntimeError("No candles returned — check symbol/host/resolution.")
    # Delta returns: time (epoch s), open, high, low, close, volume
    df = df.rename(columns={"time": "ts"})
    df["ts"] = pd.to_datetime(df["ts"], unit="s", utc=True)
    for c in ("open", "high", "low", "close"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.sort_values("ts").drop_duplicates("ts").reset_index(drop=True)
    return df[["ts", "open", "high", "low", "close"]]


def add_adx(df):
    """Append ADX and its rolling average, computed on CLOSED bars."""
    adx_df = ta.adx(high=df["high"], low=df["low"], close=df["close"],
                    length=ADX_PERIOD)
    col = f"ADX_{ADX_PERIOD}"
    df["adx"] = adx_df[col]
    df["adx_avg"] = df["adx"].rolling(ADX_AVG_PERIOD).mean()
    return df


# ================= GATES =================

def threshold_ok(adx_now):
    if pd.isna(adx_now):
        return False
    if ADX_MODE == "trend":
        return adx_now > ADX_THRESHOLD
    if ADX_MODE == "range":
        return adx_now < ADX_THRESHOLD
    return True


def slope_ok(adx_now, adx_avg, slope_mode):
    if slope_mode == "off":
        return True
    if pd.isna(adx_now) or pd.isna(adx_avg):
        return False
    if slope_mode == "rising":
        return adx_now > adx_avg
    if slope_mode == "falling":
        return adx_now < adx_avg
    return True


def build_grid(anchor):
    grid = []
    for n in range(1, GRID_LEVELS + 1):
        grid.append({"index": -n, "price": anchor - GRID_STEP * n,
                     "side": "long", "filled": False})
    for n in range(1, GRID_LEVELS + 1):
        grid.append({"index": n, "price": anchor + GRID_STEP * n,
                     "side": "short", "filled": False})
    return grid


def commission(price):
    return price * CONTRACT_SIZE * QTY * TAKER_FEE


# ================= BACKTEST ENGINE =================

def run_backtest(df, slope_mode):
    """
    Replay 15m bars. Returns a dict of metrics + the trade list.
    Daily reset keyed to 02:30 IST (matches live), via the session-date idea.
    """
    balance = START_BALANCE
    daily_pnl = 0.0
    positions = []      # each: side, entry, tp, sl, level_index
    grid = None
    anchor = None
    grid_active = False
    trading_enabled = True
    last_session = None

    ist = timezone(timedelta(hours=5, minutes=30))
    RESET_MIN = 2 * 60 + 30  # 02:30 in minutes

    trades = []
    equity_curve = []
    peak = START_BALANCE
    max_dd = 0.0

    def session_id(ts):
        local = ts.astimezone(ist)
        mins = local.hour * 60 + local.minute
        d = local.date()
        if mins >= RESET_MIN:
            return d
        return d - timedelta(days=1)

    def close(posn, exit_price, reason):
        nonlocal balance, daily_pnl
        if posn["side"] == "long":
            gross = (exit_price - posn["entry"]) * CONTRACT_SIZE * QTY
        else:
            gross = (posn["entry"] - exit_price) * CONTRACT_SIZE * QTY
        net = gross - commission(posn["entry"]) - commission(exit_price)
        balance += net
        daily_pnl += net
        for lvl in grid:
            if lvl["index"] == posn["level_index"]:
                lvl["filled"] = False
        trades.append({"side": posn["side"], "entry": posn["entry"],
                       "exit": exit_price, "net": net, "reason": reason})
        return net

    for _, bar in df.iterrows():
        ts = bar["ts"]
        o, hi, lo, c = bar["open"], bar["high"], bar["low"], bar["close"]
        adx_now, adx_avg = bar["adx"], bar["adx_avg"]
        if pd.isna(c):
            continue

        calm = threshold_ok(adx_now)
        sid = session_id(ts)

        # ---- daily reset (session anchor) ----
        new_session = (last_session is None) or (sid != last_session)
        if new_session and calm:
            # flatten stragglers at this bar's open before re-anchoring
            for p in list(positions):
                close(p, o, "SESSION-FLATTEN")
            positions.clear()
            anchor = o
            grid = build_grid(o)
            grid_active = True
            trading_enabled = True
            daily_pnl = 0.0
            last_session = sid
        elif grid is None and calm:
            anchor = o
            grid = build_grid(o)
            grid_active = True
            last_session = sid

        if grid is None:
            equity_curve.append(balance)
            continue

        # ---- regime check: ADX left calm => stop new entries ----
        if not calm:
            grid_active = False
        elif not grid_active:
            # calm returned -> rebuild fresh at this bar's open
            for p in list(positions):
                close(p, o, "REBUILD-FLATTEN")
            positions.clear()
            anchor = o
            grid = build_grid(o)
            grid_active = True

        # ---- EXIT open positions using this bar's range ----
        for p in list(positions):
            hit_tp = hit_sl = False
            if p["side"] == "long":
                hit_tp = hi >= p["tp"]
                hit_sl = lo <= p["sl"]
            else:
                hit_tp = lo <= p["tp"]
                hit_sl = hi >= p["sl"]
            if not (hit_tp or hit_sl):
                continue
            # CONSERVATIVE same-bar resolution: SL wins ties.
            if hit_sl:
                close(p, p["sl"], "SL")
            else:
                close(p, p["tp"], "TP")
            positions.remove(p)

        # daily target / loss halts
        if daily_pnl >= DAILY_TARGET:
            trading_enabled = False
        if daily_pnl <= MAX_DAILY_LOSS:
            trading_enabled = False

        # ---- ENTRY ----
        can_enter = (
            trading_enabled and grid_active and balance >= 1000
            and abs(c - anchor) <= GRID_BOUNDARY
            and threshold_ok(adx_now)
            and slope_ok(adx_now, adx_avg, slope_mode)
        )
        if can_enter:
            for lvl in grid:
                if lvl["filled"]:
                    continue
                touched = (lvl["side"] == "long" and lo <= lvl["price"]) or \
                          (lvl["side"] == "short" and hi >= lvl["price"])
                if not touched:
                    continue
                lvl["filled"] = True
                entry = lvl["price"]   # approximate fill at level price
                if lvl["side"] == "long":
                    tp, sl = entry + GRID_TP, entry - GRID_SL
                else:
                    tp, sl = entry - GRID_TP, entry + GRID_SL
                positions.append({"side": lvl["side"], "entry": entry,
                                  "tp": tp, "sl": sl,
                                  "level_index": lvl["index"]})

        # equity + drawdown bookkeeping
        equity_curve.append(balance)
        peak = max(peak, balance)
        dd = balance - peak
        max_dd = min(max_dd, dd)

    # ---- metrics ----
    wins = [t for t in trades if t["net"] > 0]
    losses = [t for t in trades if t["net"] <= 0]
    n = len(trades)
    win_rate = (len(wins) / n * 100) if n else 0.0
    avg_win = (sum(t["net"] for t in wins) / len(wins)) if wins else 0.0
    avg_loss = (sum(t["net"] for t in losses) / len(losses)) if losses else 0.0
    net_pnl = balance - START_BALANCE

    return {
        "slope": slope_mode,
        "net_pnl": net_pnl,
        "end_balance": balance,
        "trades": n,
        "win_rate": win_rate,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "max_dd": max_dd,
    }


# ================= REPORT =================

def main():
    print(f"Fetching {DAYS}d of {SYMBOL} {ADX_TIMEFRAME} candles from {DELTA_HOST} ...")
    try:
        df = fetch_candles(SYMBOL, ADX_TIMEFRAME, DAYS)
    except Exception as e:
        print(f"\nFETCH FAILED: {e}")
        print("If this is a host/allowlist error, edit DELTA_HOST at the top "
              "(try https://api.delta.exchange) and re-run.")
        sys.exit(1)

    df = add_adx(df)
    print(f"Got {len(df)} bars: {df['ts'].iloc[0]} -> {df['ts'].iloc[-1]}")
    calm_pct = (df["adx"] < ADX_THRESHOLD).mean() * 100
    print(f"Bars calm (ADX<{ADX_THRESHOLD}): {calm_pct:.1f}%\n")

    results = [run_backtest(df.copy(), s) for s in SLOPE_VARIANTS]

    # table
    hdr = (f"{'slope':<9}{'net_pnl':>11}{'trades':>8}{'win%':>8}"
           f"{'avg_win':>10}{'avg_loss':>10}{'max_dd':>11}")
    print(hdr)
    print("-" * len(hdr))
    for r in results:
        print(f"{r['slope']:<9}{r['net_pnl']:>11.2f}{r['trades']:>8}"
              f"{r['win_rate']:>7.1f}%{r['avg_win']:>10.2f}"
              f"{r['avg_loss']:>10.2f}{r['max_dd']:>11.2f}")

    print("\nNotes:")
    print("- SL wins same-bar TP/SL ties (conservative), so PnL is pessimistic.")
    print("- Fill prices approximate the level price, not a live 3s tick.")
    print("- Use this to RANK off/rising/falling, not as a live-PnL promise.")
    best = max(results, key=lambda r: r["net_pnl"])
    print(f"\nBest by net PnL over this window: slope='{best['slope']}' "
          f"({best['net_pnl']:.2f}). Validate on more history before trusting it.")


if __name__ == "__main__":
    main()