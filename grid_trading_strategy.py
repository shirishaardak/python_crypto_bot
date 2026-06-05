"""
price_grid_strategy.py  (paper mode)

A mean-reversion price grid. Plain version — no mode switches.

THE WHOLE LOGIC IN ONE PLACE
----------------------------
ENTRY (both must be true):
    1.  ADX(15m) < 35            -> market is calm / ranging, not trending
    2.  ADX(15m) < its average   -> ADX is falling (trend weakening)
    That's it. A grid wants calm, falling-ADX conditions, nothing more.

GRID:
    Levels BELOW the anchor  -> BUY  (buy the dip)
    Levels ABOVE the anchor  -> SHORT (sell the rally)
    Each filled level gets its own TP and SL, measured from the live entry.

RE-ENTRY:
    When a level's position exits (TP or SL) the level is freed again.
    If price later returns to that same level, it FILLS AGAIN. Levels are
    reusable for as long as the grid is alive and the market is calm.

DAILY TARGET:
    +$600 realized -> stop trading for the rest of the day.
    Resets at the next 02:30 IST (fixed wall-clock, no daylight saving).

ANCHOR / RESET:
    The grid is (re)built at the current price when:
      * the bot first starts, or
      * a new day begins (02:30 IST), or
      * ADX drops back below the threshold after having risen above it
        (any calm-after-above round-trip re-centers the grid).
    The grid is only ever built while ADX < threshold, so levels and entries
    always live in the same calm regime.
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

GRID_STEP = 300                          # spacing between levels (price points)
GRID_LEVELS =3                          # levels above and below the anchor
GRID_TP = GRID_STEP * 1.5                # take profit per trade = 300
GRID_SL = GRID_STEP                      # stop loss per trade   = 200
GRID_BOUNDARY = GRID_STEP * GRID_LEVELS  # outer risk boundary   = 1200

# ================= ADX ENTRY FILTER =================
# The only two conditions, both required:
#     ADX < ADX_THRESHOLD          (calm, not trending)
#     ADX < average of last N ADX  (ADX falling)

ADX_TIMEFRAME = "15m"
ADX_PERIOD = 14
ADX_THRESHOLD = 20.0     # the calm line; below this = calm, above = trending
ADX_AVG_PERIOD = 5       # how many recent ADX values to average

# ================= DAILY TARGET =================

DAILY_TARGET = 600       # +$600 realized -> stop for the day

# ================= SESSION RESET =================
# Fixed wall-clock reset at 02:30 IST every day (no daylight saving).

RESET_TZ = ZoneInfo("Asia/Kolkata")
RESET_TIME = dt_time(2, 30)


def current_session_id():
    """
    Session id = the calendar date (IST) of the most recent 02:30 IST that has
    already passed. It changes exactly once per day, the moment 02:30 IST hits,
    and does not depend on the server's own timezone.
    """
    now = datetime.now(RESET_TZ)
    if now.time() >= RESET_TIME:
        return now.date()                      # today's 02:30 already passed
    return (now - timedelta(days=1)).date()    # still in yesterday's session


# ================= INIT =================

utils = TradingUtils(
    contract_size=CONTRACT_SIZE,
    taker_fee=TAKER_FEE,
    timeframe=TIMEFRAME,
    days=DAYS,
    telegram_token=os.getenv("supertrend_ha_fast_bot"),
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
# Cached: candle fetch is throttled, and ADX is recomputed only on a new bar.

_candle_cache = {}            # symbol -> {"ts": monotonic, "df": DataFrame}
_adx_cache = {}               # symbol -> {"key": bar_ts, "val": (now, avg)}
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
    """Find a column by case-insensitive name."""
    lookup = {str(c).lower(): c for c in df.columns}
    for name in names:
        if name in lookup:
            return df[lookup[name]]
    return None


def compute_adx(symbol):
    """
    Return (adx_now, adx_avg) for the latest CLOSED 15m bar.
    Returns (None, None) if data is missing.
    """
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
    closed = series.iloc[:-1]              # drop the still-forming bar

    adx_now = float(closed.iloc[-1])
    n = min(ADX_AVG_PERIOD, len(closed))
    adx_avg = float(closed.iloc[-n:].mean())

    val = (adx_now, adx_avg)
    _adx_cache[symbol] = {"key": bar_key, "val": val}
    return val


def is_calm(adx_now):
    """Calm = ADX below the threshold. Missing ADX -> not calm (safe)."""
    return adx_now is not None and adx_now < ADX_THRESHOLD


def adx_allows_entry(adx_now, adx_avg):
    """
    The two-condition entry gate:
        ADX < threshold   (calm)
        ADX < its average (falling)
    Both required. Missing data blocks entry.
    """
    if adx_now is None or adx_avg is None:
        return False
    return (adx_now < ADX_THRESHOLD) and (adx_now < adx_avg)


# ================= GRID BUILD =================

def build_grid(anchor):
    """Below anchor = BUY levels, above anchor = SHORT levels."""
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
    """Book PnL after fees, FREE THE LEVEL (so it can fill again), log it."""
    if posn["side"] == "long":
        gross = (exit_price - posn["entry"]) * CONTRACT_SIZE[symbol] * posn["qty"]
    else:
        gross = (posn["entry"] - exit_price) * CONTRACT_SIZE[symbol] * posn["qty"]

    fees = (utils.commission(posn["entry"], posn["qty"], symbol)
            + utils.commission(exit_price, posn["qty"], symbol))
    net = gross - fees

    state["balance"] += net
    state["daily_pnl"] += net

    # Free the level -> price returning here later will RE-ENTER.
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
    """Flatten any stragglers, lay a new grid at the current price."""
    if state["grid"] is not None and state["positions"]:
        for posn in list(state["positions"]):
            _close_position(state, symbol, posn, price, now, reason)

    state["anchor"] = price
    state["grid"] = build_grid(price)
    state["grid_active"] = True
    state["trading_enabled"] = True
    state["last_session_id"] = session_id

    # Only the daily reset zeroes the daily PnL — not intraday rebuilds.
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
    Build the grid when:
      1. it's a new day (or first run), or
      2. ADX has dropped back below the threshold after having been above it
         (any calm-after-above round-trip re-centers the grid).
    Either way, only while the market is calm right now.
    """
    session_id = current_session_id()
    calm = is_calm(adx_now)

    new_day = state["grid"] is None or state.get("last_session_id") != session_id
    if new_day:
        if calm:
            build_fresh_grid(state, symbol, price, now, session_id,
                             adx_now, "SESSION-ANCHOR")
        # If not calm we simply wait; we'll retry next tick.
        return

    # ADX came back below the threshold after having been above it.
    # was_above_threshold is the prior tick's "ADX >= threshold" reading,
    # so calm + was_above means a fresh round-trip back into calm.
    if calm and state.get("was_above_threshold", False):
        build_fresh_grid(state, symbol, price, now, session_id,
                         adx_now, "CALM-RETURN")


def check_regime(state, symbol, adx_now):
    """
    If ADX has left calm, switch the grid OFF (no new entries). Open positions
    keep running to their own TP/SL. Returns True if the grid is tradeable.
    """
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


# ================= STRATEGY =================

def process_symbol(symbol, price, state):
    now = datetime.now()
    adx_now, adx_avg = compute_adx(symbol)

    maybe_reanchor(state, symbol, price, now, adx_now)
    save_grid(state, symbol)

    # Track whether ADX is currently above the threshold, so the NEXT tick can
    # detect a back-below-threshold round-trip in maybe_reanchor. Missing ADX
    # is treated as "not above" so it can't spuriously trigger a re-anchor.
    state["was_above_threshold"] = (adx_now is not None and adx_now >= ADX_THRESHOLD)

    if state["grid"] is None:
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

        # Once the daily target is hit, close any position that's in profit.
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
        return                              # price escaped the grid
    if not check_regime(state, symbol, adx_now):
        return                              # grid switched off by a trend
    if not adx_allows_entry(adx_now, adx_avg):
        return                              # ADX gate: calm AND falling

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

        # Enter at the live price; TP/SL are measured from that live entry.
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
        f"| daily_target={DAILY_TARGET}",
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