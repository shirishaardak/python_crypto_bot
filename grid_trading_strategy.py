"""
price_grid_strategy.py  (paper mode)

A mean-reversion price grid.

DESIGN (agreed):
  RE-ANCHOR (rebuild levels) happens ONLY around the 30 line:
    1. Every day at 02:30 IST          -> SESSION-ANCHOR
    2. ADX goes ABOVE 30 then back BELOW 30 -> CALM-RETURN (re-center at new price)

  ENTRY (both required):
    ADX < 30            (calm)
    ADX < its average   (falling)

  EXITS:
    1. TP / SL          -> per-trade
    2. TARGET-LOCK      -> daily target hit, close anything in profit
    3. TREND-EXIT       -> ADX >= 32 (real trend) OR ADX rising (adx_now > adx_avg)
                           -> flatten everything
"""

import os
import time
import traceback
from datetime import datetime, timedelta
from datetime import time as dt_time
from zoneinfo import ZoneInfo

import pandas as pd
import pandas_ta as ta
from dotenv import load_dotenv

from utils import TradingUtils

load_dotenv()

# ================= CORE CONFIG =================

BOT_NAME = "price_grid_strategy"
SYMBOLS = ["BTCUSD"]

DEFAULT_CONTRACTS = {"BTCUSD": 1000}
CONTRACT_SIZE = {"BTCUSD": 0.001}

TAKER_FEE = 0.0005
TIMEFRAME = "15m"
DAYS = 15
MIN_BALANCE = 1000
START_BALANCE = 10000

# ================= GRID CONFIG =================

GRID_STEP = 200                          # spacing between levels (price points)
GRID_LEVELS = 3                          # levels above and below the anchor
GRID_TP = GRID_STEP * 1.5                # take profit per trade = 300
GRID_SL = GRID_STEP * 3                  # stop loss per trade   = 600 (wide backstop)
GRID_BOUNDARY = GRID_STEP * GRID_LEVELS  # outer risk boundary   = 600

# ================= ADX ENTRY FILTER =================

ADX_TIMEFRAME = "15m"
ADX_PERIOD = 14
ADX_THRESHOLD = 30.0       # the calm line; below = calm, above = grid off
ADX_AVG_PERIOD = 5         # how many recent ADX values to average

# ADX at/above this -> flatten EVERY open position (trend confirmed).
TREND_EXIT_THRESHOLD = 32.0

# ================= DAILY TARGET =================

DAILY_TARGET = 1200         # realized -> stop for the day

# ================= SESSION RESET =================

RESET_TZ = ZoneInfo("Asia/Kolkata")
RESET_TIME = dt_time(2, 30)


def current_session_id():
    """Calendar date (IST) of the most recent 02:30 IST that has passed."""
    now = datetime.now(RESET_TZ)
    if now.time() >= RESET_TIME:
        return now.date()
    return (now - timedelta(days=1)).date()


# ================= INIT =================

utils = TradingUtils(
    contract_size=CONTRACT_SIZE,
    taker_fee=TAKER_FEE,
    timeframe=TIMEFRAME,
    days=DAYS,
    telegram_token=os.getenv("testmyaglostrategy_bot"),
    telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID"),
    bot_name=BOT_NAME,
)

SAVE_DIR = os.path.join(os.getcwd(), "data", "price_grid_strategy")
os.makedirs(SAVE_DIR, exist_ok=True)


# ================= PERSISTENCE =================

def save_grid(state, symbol):
    """Write the grid snapshot to CSV, but only when it actually changed."""
    if state["grid"] is None:
        return

    snapshot = tuple(
        (lvl["index"], lvl["price"], lvl["side"], lvl["filled"])
        for lvl in state["grid"]
    )
    if state.get("_last_saved") == snapshot:
        return
    state["_last_saved"] = snapshot

    rows = [
        {
            "level_index": lvl["index"],
            "price": lvl["price"],
            "side": lvl["side"],
            "filled": lvl["filled"],
        }
        for lvl in state["grid"]
    ]
    path = os.path.join(SAVE_DIR, f"{symbol}_grid.csv")
    pd.DataFrame(rows).to_csv(path, index=False)


# ================= ADX =================

_candle_cache = {}
_adx_cache = {}
ADX_FETCH_EVERY_SEC = 60


def _fetch_adx_candles(symbol):
    cached = _candle_cache.get(symbol)
    nowm = time.monotonic()
    if cached is not None and (nowm - cached["ts"]) < ADX_FETCH_EVERY_SEC:
        return cached["df"]

    try:
        df = utils.fetch_candles(symbol, timeframe=ADX_TIMEFRAME)
    except TypeError:
        df = utils.fetch_candles(symbol)

    _candle_cache[symbol] = {"ts": nowm, "df": df}
    return df


def _col(df, *names):
    lookup = {str(c).lower(): c for c in df.columns}
    for name in names:
        if name in lookup:
            return df[lookup[name]]
    return None


def compute_adx(symbol):
    """Return (adx_now, adx_avg) for the latest CLOSED 15m bar, or (None, None)."""
    df = _fetch_adx_candles(symbol)
    if df is None or len(df) < ADX_PERIOD * 2:
        return None, None

    try:
        bar_key = df.index[-1]
    except Exception:
        bar_key = len(df)

    cached = _adx_cache.get(symbol)
    if cached is not None and cached["key"] == bar_key:
        return cached["val"]

    high = _col(df, "high", "h")
    low = _col(df, "low", "l")
    close = _col(df, "close", "c", "close_price", "last")
    if high is None or low is None or close is None:
        return None, None

    adx_df = ta.adx(high=high, low=low, close=close, length=ADX_PERIOD)
    if adx_df is None or adx_df.empty:
        return None, None

    col = f"ADX_{ADX_PERIOD}"
    if col not in adx_df.columns:
        return None, None

    series = adx_df[col].dropna()
    if len(series) < 2:
        return None, None
    closed = series.iloc[:-1]

    adx_now = float(closed.iloc[-1])
    n = min(ADX_AVG_PERIOD, len(closed))
    adx_avg = float(closed.iloc[-n:].mean())

    val = (adx_now, adx_avg)
    _adx_cache[symbol] = {"key": bar_key, "val": val}
    return val


def is_calm(adx_now):
    return adx_now is not None and adx_now < ADX_THRESHOLD


def is_trending(adx_now):
    return adx_now is not None and adx_now >= TREND_EXIT_THRESHOLD


def adx_allows_entry(adx_now, adx_avg):
    if adx_now is None or adx_avg is None:
        return False
    return (adx_now < ADX_THRESHOLD) and (adx_now < adx_avg)


# ================= GRID BUILD =================

def build_grid(anchor):
    grid = []
    for n in range(1, GRID_LEVELS + 1):
        grid.append({"index": -n, "price": anchor - GRID_STEP * n,
                     "side": "long", "filled": False})
    for n in range(1, GRID_LEVELS + 1):
        grid.append({"index": n, "price": anchor + GRID_STEP * n,
                     "side": "short", "filled": False})
    return grid


# ================= POSITION HELPERS =================

def _close_position(state, symbol, posn, exit_price, now, reason):
    if posn["side"] == "long":
        gross = (exit_price - posn["entry"]) * CONTRACT_SIZE[symbol] * posn["qty"]
    else:
        gross = (posn["entry"] - exit_price) * CONTRACT_SIZE[symbol] * posn["qty"]

    fees = (utils.commission(posn["entry"], posn["qty"], symbol)
            + utils.commission(exit_price, posn["qty"], symbol))
    net = gross - fees

    state["balance"] += net
    state["daily_pnl"] += net

    for lvl in state["grid"]:
        if lvl["index"] == posn["level_index"]:
            lvl["filled"] = False

    if posn in state["positions"]:
        state["positions"].remove(posn)

    utils.save_trade({
        "symbol": symbol,
        "side": posn["side"],
        "entry_price": posn["entry"],
        "exit_price": exit_price,
        "qty": posn["qty"],
        "net_pnl": round(net, 6),
        "entry_time": posn["entry_time"],
        "exit_time": now,
    })

    emoji = "🟢" if net > 0 else "🔴"
    utils.log(
        f"{emoji} {symbol} EXIT ({reason}) L{posn['level_index']} "
        f"@ {exit_price} | PNL: {round(net, 6)}",
        tg=True,
    )
    return net


# ================= ANCHOR / RESET =================

def build_fresh_grid(state, symbol, price, now, session_id, adx_now, reason):
    if state["grid"] is not None and state["positions"]:
        for posn in list(state["positions"]):
            _close_position(state, symbol, posn, price, now, reason)

    state["anchor"] = price
    state["grid"] = build_grid(price)
    state["grid_active"] = True
    state["trading_enabled"] = True
    state["last_session_id"] = session_id

    if reason == "SESSION-ANCHOR":
        state["daily_pnl"] = 0

    adx_txt = f"{adx_now:.1f}" if adx_now is not None else "n/a"
    utils.log(
        f"🌙 {symbol} GRID BUILT [{reason}] (ADX15m {adx_txt}) @ {price} | "
        f"levels ±{GRID_STEP}..±{GRID_BOUNDARY} | session={session_id}",
        tg=True,
    )


def maybe_reanchor(state, symbol, price, now, adx_now):
    """
    Rebuild the grid when:
      1. new day / first run (SESSION-ANCHOR), or
      2. ADX dropped back below 30 after having been above it (CALM-RETURN).
    Only while calm right now.
    """
    session_id = current_session_id()
    calm = is_calm(adx_now)

    new_day = state["grid"] is None or state.get("last_session_id") != session_id
    if new_day:
        if calm:
            build_fresh_grid(state, symbol, price, now, session_id,
                             adx_now, "SESSION-ANCHOR")
        return

    if calm and state.get("was_above_threshold", False):
        build_fresh_grid(state, symbol, price, now, session_id,
                         adx_now, "CALM-RETURN")


def check_regime(state, symbol, adx_now):
    """Grid OFF (no new entries) when ADX leaves calm. Open trades keep running."""
    if state["grid"] is None:
        return False

    if is_calm(adx_now):
        return state.get("grid_active", True)

    if state.get("grid_active", True):
        adx_txt = f"{adx_now:.1f}" if adx_now is not None else "n/a"
        utils.log(
            f"⚠️ {symbol} GRID OFF (ADX15m {adx_txt} left calm) — no new entries; "
            f"open trades run to their own TP/SL",
            tg=True,
        )
        state["grid_active"] = False
    return False


def maybe_trend_exit(state, symbol, price, now, adx_now, adx_avg):
    """
    Flatten EVERY open position when ADX confirms a real trend (>= 32)
    OR when ADX is rising (adx_now > adx_avg).
    """
    if state["grid"] is None or not state["positions"]:
        return False

    if adx_now is None or adx_avg is None:
        return False

    rising = adx_now > adx_avg
    trending = is_trending(adx_now)

    if not (trending or rising):
        return False

    reason_txt = (f">= {TREND_EXIT_THRESHOLD}" if trending
                  else f"rising (avg {adx_avg:.1f})")
    adx_txt = f"{adx_now:.1f}" if adx_now is not None else "n/a"
    utils.log(
        f"🚨 {symbol} TREND-EXIT (ADX15m {adx_txt} {reason_txt}) "
        f"— flattening all {len(state['positions'])} position(s)",
        tg=True,
    )
    for posn in list(state["positions"]):
        _close_position(state, symbol, posn, price, now, "TREND-EXIT")

    state["grid_active"] = False
    utils.log(
        f"💰 Balance: {round(state['balance'], 2)} | "
        f"📊 Daily PNL: {round(state['daily_pnl'], 2)}",
        tg=True,
    )
    return True


# ================= STRATEGY =================

def process_symbol(symbol, price, state):
    now = datetime.now()
    adx_now, adx_avg = compute_adx(symbol)

    maybe_reanchor(state, symbol, price, now, adx_now)
    save_grid(state, symbol)

    # Arm the CALM-RETURN detector for the NEXT tick.
    state["was_above_threshold"] = (adx_now is not None and adx_now >= ADX_THRESHOLD)

    if state["grid"] is None:
        return

    # ---------- HARD TREND-EXIT (before anything else) ----------
    if maybe_trend_exit(state, symbol, price, now, adx_now, adx_avg):
        return

    anchor = state["anchor"]

    # ---------- EXIT OPEN POSITIONS (TP / SL / target-lock) ----------
    for posn in list(state["positions"]):
        if posn["side"] == "long":
            live_pnl = (price - posn["entry"]) * CONTRACT_SIZE[symbol] * posn["qty"]
            hit_tp = price >= posn["tp_price"]
            hit_sl = price <= posn["sl_price"]
        else:
            live_pnl = (posn["entry"] - price) * CONTRACT_SIZE[symbol] * posn["qty"]
            hit_tp = price <= posn["tp_price"]
            hit_sl = price >= posn["sl_price"]

        force_exit = (state["daily_pnl"] >= DAILY_TARGET and live_pnl > 0)

        if not (hit_tp or hit_sl or force_exit):
            continue

        reason = "TP" if hit_tp else "SL" if hit_sl else "TARGET-LOCK"
        _close_position(state, symbol, posn, price, now, reason)
        utils.log(
            f"💰 Balance: {round(state['balance'], 2)} | "
            f"📊 Daily PNL: {round(state['daily_pnl'], 2)}",
            tg=True,
        )

        if state["daily_pnl"] >= DAILY_TARGET:
            state["trading_enabled"] = False
            utils.log(
                f"🎯 DAILY TARGET HIT: {round(state['daily_pnl'], 2)} "
                f"— stopping for the day (resets after 02:30 IST)",
                tg=True,
            )

    # ---------- ENTRY GUARDS ----------
    if not state["trading_enabled"]:
        return
    if state["balance"] < MIN_BALANCE:
        utils.log(f"⚠️ Balance low: {state['balance']}", tg=True)
        return
    if abs(price - anchor) > GRID_BOUNDARY:
        return
    if not check_regime(state, symbol, adx_now):
        return
    if not adx_allows_entry(adx_now, adx_avg):
        return

    # ---------- FILL LEVELS ----------
    for lvl in state["grid"]:
        if lvl["filled"]:
            continue

        if lvl["side"] == "long" and price <= lvl["price"]:
            pass
        elif lvl["side"] == "short" and price >= lvl["price"]:
            pass
        else:
            continue

        lvl["filled"] = True

        if lvl["side"] == "long":
            tp_price = price + GRID_TP
            sl_price = price - GRID_SL
        else:
            tp_price = price - GRID_TP
            sl_price = price + GRID_SL

        state["positions"].append({
            "side": lvl["side"],
            "entry": price,
            "tp_price": tp_price,
            "sl_price": sl_price,
            "qty": DEFAULT_CONTRACTS[symbol],
            "entry_time": now,
            "level_index": lvl["index"],
        })

        emoji = "🟢" if lvl["side"] == "long" else "🔴"
        utils.log(
            f"{emoji} {symbol} {lvl['side'].upper()} L{lvl['index']} @ {price} "
            f"(lvl {lvl['price']}) | TP→ {tp_price} SL→ {sl_price} | "
            f"ADX15m {adx_now:.1f} (avg {adx_avg:.1f})",
            tg=True,
        )


# ================= MAIN =================

def run():
    state = {
        s: {
            "positions": [],
            "grid": None,
            "grid_active": False,
            "anchor": None,
            "last_session_id": None,
            "balance": START_BALANCE,
            "daily_pnl": 0,
            "trading_enabled": True,
            "was_above_threshold": False,
        }
        for s in SYMBOLS
    }

    utils.log("🚀 GRID BOT STARTED (PAPER MODE)", tg=True)
    utils.log(
        f"⚙️ Entry: ADX < {ADX_THRESHOLD} AND ADX < avg({ADX_AVG_PERIOD}) "
        f"| tf={ADX_TIMEFRAME} period={ADX_PERIOD}",
        tg=True,
    )
    utils.log(
        f"⚙️ Grid: levels={GRID_LEVELS} step={GRID_STEP} "
        f"tp={GRID_TP} sl={GRID_SL} boundary={GRID_BOUNDARY} "
        f"| daily_target={DAILY_TARGET} | trend_exit>={TREND_EXIT_THRESHOLD} or rising",
        tg=True,
    )

    while True:
        try:
            for symbol in SYMBOLS:
                price = utils.fetch_price(symbol)
                if price is None:
                    continue
                process_symbol(symbol, price, state[symbol])
            time.sleep(3)
        except Exception as e:
            utils.log(f"🚨 Runtime error: {e}\n{traceback.format_exc()}", tg=True)
            time.sleep(5)


if __name__ == "__main__":
    run()