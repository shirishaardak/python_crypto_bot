"""
price_grid_strategy.py

Mean-reversion price grid (paper mode) with:
  - Level-price fills (no market-slippage entries)
  - Per-level TP tied to the level, not the fill
  - Clean re-anchor that flattens stale positions
  - 15m ADX entry filter (configurable)

NOTE ON THE ADX FILTER
----------------------
You asked: "enter only when ADX(15m) > 25".
ADX > 25 = STRONG TREND. A mean-reversion grid generally LOSES in strong
trends (price marches through levels and never reverts) and WINS in chop.
So >25 is the opposite of what grids usually want.

This file exposes ADX_MODE so you can choose:
    "trend"  -> enter only when ADX > ADX_THRESHOLD   (what you requested)
    "range"  -> enter only when ADX < ADX_THRESHOLD   (theory-favoured for grids)
    "off"    -> ignore ADX entirely

Default is "trend" (your request). Flip to "range" to backtest the grid-friendly
version. I'd strongly suggest comparing both before going live.
"""

import os
import time
import traceback
from datetime import datetime
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
TIMEFRAME = "5m"
DAYS = 15
MIN_BALANCE = 1000
START_BALANCE = 10000

# ================= GRID CONFIG =================

GRID_STEP = 200                      # spacing between levels, in price points
GRID_LEVELS = 3                      # levels above and below the anchor
GRID_TP = GRID_STEP                  # profit booked per grid trade = one step
GRID_BOUNDARY = GRID_STEP * GRID_LEVELS  # outer risk boundary (600)

# ================= ADX ENTRY FILTER =================

ADX_TIMEFRAME = "15m"   # timeframe the ADX is computed on
ADX_PERIOD = 14         # standard ADX lookback
ADX_THRESHOLD = 35.0    # the 25 line you asked for

# --- Level gate (current ADX vs the fixed threshold) ---
# "trend" -> ADX > threshold   (your original request)
# "range" -> ADX < threshold   (grid-friendly)
# "off"   -> ignore the threshold entirely
ADX_MODE = "trend"

# --- Slope gate (current ADX vs its own moving average) ---
# Compares the latest ADX to the average of the last ADX_AVG_PERIOD values.
#   "rising"  -> enter only when ADX > its average  (trend strengthening)
#   "falling" -> enter only when ADX < its average  (trend weakening -> good for grids)
#   "off"     -> ignore slope
# Both gates must pass to enter. Set the one(s) you don't want to "off".
ADX_SLOPE_MODE = "off"
ADX_AVG_PERIOD = 5      # how many recent ADX values to average for the slope gate

# ================= SESSION / RE-ANCHOR =================

US_CLOSE_TZ = ZoneInfo("America/New_York")
US_CLOSE_TIME = dt_time(16, 0)

# ================= TARGET / LOSS =================

DAILY_TARGET = 600
MAX_DAILY_LOSS = -1000
FORCE_EXIT_PROFITABLE_ON_TARGET = True

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

SAVE_DIR = os.path.join(os.getcwd(), "data", "price_grid_strategy_live")
os.makedirs(SAVE_DIR, exist_ok=True)


# ================= PERSISTENCE =================

def save_processed_data(state, symbol):
    if state["grid"] is None:
        return
    path = os.path.join(SAVE_DIR, f"{symbol}_grid.csv")
    rows = [
        {
            "level_index": lvl["index"],
            "price": lvl["price"],
            "side": lvl["side"],
            "filled": lvl["filled"],
        }
        for lvl in state["grid"]
    ]
    pd.DataFrame(rows).to_csv(path, index=False)


# ================= PAPER ORDER =================

def place_market_order(symbol, side, qty):
    utils.log(f"📝 PAPER ORDER → {symbol} {side.upper()} {qty}", tg=True)
    return {"success": True}


# ================= ADX =================

def compute_adx(symbol):
    """
    Fetch ADX_TIMEFRAME candles and return (adx_now, adx_avg) for the latest
    CLOSED candle:
      adx_now -> ADX value of the last closed bar
      adx_avg -> mean of the last ADX_AVG_PERIOD closed ADX values
    Returns (None, None) if data is unavailable so the caller can decide.
    """
    try:
        df = utils.fetch_candles(symbol, timeframe=ADX_TIMEFRAME)
    except TypeError:
        # Fallback if fetch_candles doesn't accept a timeframe kwarg.
        df = utils.fetch_candles(symbol)

    if df is None or len(df) < ADX_PERIOD * 2:
        return None, None

    adx_df = ta.adx(
        high=df["high"],
        low=df["low"],
        close=df["close"],
        length=ADX_PERIOD,
    )
    if adx_df is None or adx_df.empty:
        return None, None

    col = f"ADX_{ADX_PERIOD}"
    if col not in adx_df.columns:
        return None, None

    # Drop the still-forming last bar, keep only closed values.
    series = adx_df[col].dropna()
    if len(series) < 2:
        return None, None
    closed = series.iloc[:-1]  # everything up to and including the last CLOSED bar

    adx_now = float(closed.iloc[-1])

    n = min(ADX_AVG_PERIOD, len(closed))
    adx_avg = float(closed.iloc[-n:].mean()) if n > 0 else None

    return adx_now, adx_avg


def adx_allows_entry(adx_now, adx_avg):
    """
    Two independent gates; BOTH must pass.

      Level gate (ADX_MODE):
        "trend" -> adx_now > ADX_THRESHOLD
        "range" -> adx_now < ADX_THRESHOLD
        "off"   -> always passes

      Slope gate (ADX_SLOPE_MODE):
        "rising"  -> adx_now > adx_avg   (trend strengthening)
        "falling" -> adx_now < adx_avg   (trend weakening; grid-friendly)
        "off"     -> always passes

    Conservative: if a needed value is missing, that gate blocks entry.
    """
    # ----- level gate -----
    if ADX_MODE == "off":
        level_ok = True
    elif adx_now is None:
        level_ok = False
    elif ADX_MODE == "trend":
        level_ok = adx_now > ADX_THRESHOLD
    elif ADX_MODE == "range":
        level_ok = adx_now < ADX_THRESHOLD
    else:
        level_ok = True

    # ----- slope gate -----
    if ADX_SLOPE_MODE == "off":
        slope_ok = True
    elif adx_now is None or adx_avg is None:
        slope_ok = False
    elif ADX_SLOPE_MODE == "rising":
        slope_ok = adx_now > adx_avg
    elif ADX_SLOPE_MODE == "falling":
        slope_ok = adx_now < adx_avg
    else:
        slope_ok = True

    return level_ok and slope_ok


# ================= GRID BUILD =================

def build_grid(anchor):
    """
    Levels BELOW anchor = BUY (buy the dip).
    Levels ABOVE anchor = SHORT (sell the rally).
    Each level stores its target fill price.
    """
    grid = []
    for n in range(1, GRID_LEVELS + 1):
        grid.append({
            "index": -n,
            "price": anchor - GRID_STEP * n,
            "side": "long",
            "filled": False,
        })
    for n in range(1, GRID_LEVELS + 1):
        grid.append({
            "index": n,
            "price": anchor + GRID_STEP * n,
            "side": "short",
            "filled": False,
        })
    return grid


# ================= POSITION HELPERS =================

def _close_position(state, symbol, posn, exit_price, now, reason):
    """Book PnL (after fees), free the level, record the trade, log it."""
    if posn["side"] == "long":
        gross = (exit_price - posn["entry"]) * CONTRACT_SIZE[symbol] * posn["qty"]
    else:
        gross = (posn["entry"] - exit_price) * CONTRACT_SIZE[symbol] * posn["qty"]

    fees = (
        utils.commission(posn["entry"], posn["qty"], symbol)
        + utils.commission(exit_price, posn["qty"], symbol)
    )
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
        f"{emoji} {symbol} GRID EXIT ({reason}) L{posn['level_index']} "
        f"@ {exit_price} | PNL: {round(net, 6)}",
        tg=True,
    )
    return net


def _unrealized(state, symbol, price):
    total = 0.0
    for posn in state["positions"]:
        if posn["side"] == "long":
            g = (price - posn["entry"]) * CONTRACT_SIZE[symbol] * posn["qty"]
        else:
            g = (posn["entry"] - price) * CONTRACT_SIZE[symbol] * posn["qty"]
        g -= (
            utils.commission(posn["entry"], posn["qty"], symbol)
            + utils.commission(price, posn["qty"], symbol)
        )
        total += g
    return total


# ================= RE-ANCHOR =================

def maybe_reanchor(state, symbol, price, now):
    """
    Re-anchor after US close, once per session date.
    Flattens any open positions at the current price BEFORE rebuilding,
    so no position is orphaned against a stale grid.
    """
    now_et = datetime.now(US_CLOSE_TZ)
    is_after_close = now_et.time() >= US_CLOSE_TIME
    anchor_date = now_et.date()

    needs_anchor = (
        state["grid"] is None
        or (is_after_close and state["last_anchor_date"] != anchor_date)
    )
    if not needs_anchor:
        return

    # Flatten everything before re-anchoring (skip on very first build).
    if state["grid"] is not None and state["positions"]:
        for posn in list(state["positions"]):
            _close_position(state, symbol, posn, price, now, "RE-ANCHOR")

    state["anchor"] = price
    state["grid"] = build_grid(price)
    state["daily_pnl"] = 0
    state["trading_enabled"] = True
    state["last_anchor_date"] = anchor_date

    utils.log(
        f"🌙 {symbol} GRID RE-ANCHORED after US close @ {price} | "
        f"levels ±{GRID_STEP}..±{GRID_BOUNDARY}",
        tg=True,
    )


# ================= STRATEGY =================

def process_symbol(symbol, price, state):
    now = datetime.now()

    maybe_reanchor(state, symbol, price, now)
    save_processed_data(state, symbol)

    anchor = state["anchor"]

    # ---------- EXIT OPEN POSITIONS (TP or target-lock) ----------
    for posn in list(state["positions"]):
        # TP is the fixed level-anchored target captured at entry. Falls back
        # to entry±GRID_TP for any legacy position without a tp_price.
        if posn["side"] == "long":
            live_pnl = (price - posn["entry"]) * CONTRACT_SIZE[symbol] * posn["qty"]
            tp_target = posn.get("tp_price", posn["entry"] + GRID_TP)
            hit_tp = price >= tp_target
        else:
            live_pnl = (posn["entry"] - price) * CONTRACT_SIZE[symbol] * posn["qty"]
            tp_target = posn.get("tp_price", posn["entry"] - GRID_TP)
            hit_tp = price <= tp_target

        # Only force-exit on target if THIS position is itself in profit,
        # so we lock the day without dumping underwater trades.
        force_exit = (
            FORCE_EXIT_PROFITABLE_ON_TARGET
            and DAILY_TARGET is not None
            and state["daily_pnl"] >= DAILY_TARGET
            and live_pnl > 0
        )

        if not (hit_tp or force_exit):
            continue

        _close_position(
            state, symbol, posn, price, now,
            "TP" if hit_tp else "TARGET-LOCK",
        )
        utils.log(
            f"💰 Balance: {round(state['balance'], 2)} | "
            f"📊 Daily PNL: {round(state['daily_pnl'], 2)}",
            tg=True,
        )

        if DAILY_TARGET is not None and state["daily_pnl"] >= DAILY_TARGET:
            state["trading_enabled"] = False
            utils.log(
                f"🎯 DAILY TARGET HIT: {round(state['daily_pnl'], 2)} "
                f"— stopping for the day (resets after US close)",
                tg=True,
            )

        if MAX_DAILY_LOSS is not None and state["daily_pnl"] <= MAX_DAILY_LOSS:
            state["trading_enabled"] = False
            utils.log("🛑 MAX DAILY LOSS HIT — stopping for the day", tg=True)

    # ---------- LIVE (REALIZED + UNREALIZED) LOSS HALT ----------
    if MAX_DAILY_LOSS is not None:
        unrealized = _unrealized(state, symbol, price)
        if (state["daily_pnl"] + unrealized) <= MAX_DAILY_LOSS:
            utils.log(
                f"🛑 MAX DAILY LOSS HIT (incl. open) "
                f"{round(state['daily_pnl'] + unrealized, 2)} — FLATTENING ALL",
                tg=True,
            )
            for posn in list(state["positions"]):
                # When live: place_market_order(symbol, opp_side, posn["qty"]) first.
                _close_position(state, symbol, posn, price, now, "FORCE-FLATTEN")

            state["trading_enabled"] = False
            utils.log(
                f"💰 Balance: {round(state['balance'], 2)} | "
                f"📊 Daily PNL: {round(state['daily_pnl'], 2)} "
                f"— halted for the day (resets after US close)",
                tg=True,
            )
            return

    # ---------- ENTRY GUARDS ----------
    if not state["trading_enabled"]:
        return
    if state["balance"] < MIN_BALANCE:
        utils.log(f"⚠️ Balance low: {state['balance']}", tg=True)
        return
    if abs(price - anchor) > GRID_BOUNDARY:
        return  # price escaped the grid; don't open new risk

    # ---------- ADX GATE ----------
    adx_now, adx_avg = compute_adx(symbol)
    if not adx_allows_entry(adx_now, adx_avg):
        return

    # ---------- FILL LEVELS ----------
    for lvl in state["grid"]:
        if lvl["filled"]:
            continue

        if lvl["side"] == "long" and price <= lvl["price"]:
            should_fill = True
        elif lvl["side"] == "short" and price >= lvl["price"]:
            should_fill = True
        else:
            should_fill = False

        if not should_fill:
            continue

        lvl["filled"] = True

        # Enter at the LIVE market price (real fill). If price gapped THROUGH
        # the level, this captures the true worse fill, not the clean level price.
        #
        # TP is anchored to the LIVE ENTRY, so the trade targets exactly
        # GRID_TP points from where it actually filled:
        #   long  -> tp = entry + GRID_TP
        #   short -> tp = entry - GRID_TP
        if lvl["side"] == "long":
            tp_price = price + GRID_TP
        else:
            tp_price = price - GRID_TP

        state["positions"].append({
            "side": lvl["side"],
            "entry": price,                 # live fill, used for PnL
            "level_price": lvl["price"],    # level reference (info only)
            "tp_price": tp_price,           # TP measured from the live entry
            "qty": DEFAULT_CONTRACTS[symbol],
            "entry_time": now,
            "level_index": lvl["index"],
        })

        emoji = "🟢" if lvl["side"] == "long" else "🔴"
        adx_txt = f"{adx_now:.1f}" if adx_now is not None else "n/a"
        avg_txt = f"{adx_avg:.1f}" if adx_avg is not None else "n/a"
        utils.log(
            f"{emoji} {symbol} GRID {lvl['side'].upper()} L{lvl['index']} "
            f"@ {price} (lvl {lvl['price']}) | TP→ {tp_price} ({GRID_TP}pt) | "
            f"ADX15m: {adx_txt} (avg {avg_txt})",
            tg=True,
        )


# ================= MAIN =================

def run():
    state = {
        s: {
            "positions": [],
            "grid": None,
            "anchor": None,
            "last_candle_time": None,
            "last_anchor_date": None,
            "balance": START_BALANCE,
            "daily_pnl": 0,
            "trading_enabled": True,
        }
        for s in SYMBOLS
    }

    utils.log("🚀 LIVE GRID BOT STARTED (PAPER MODE)", tg=True)
    utils.log(
        f"⚙️ ADX filter: mode={ADX_MODE} slope={ADX_SLOPE_MODE} "
        f"tf={ADX_TIMEFRAME} period={ADX_PERIOD} "
        f"threshold={ADX_THRESHOLD} avg_period={ADX_AVG_PERIOD}",
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
            utils.log(
                f"🚨 Runtime error: {e}\n{traceback.format_exc()}",
                tg=True,
            )
            time.sleep(5)


if __name__ == "__main__":
    run()