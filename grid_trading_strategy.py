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

SESSION ANCHOR (the reset)
--------------------------
The daily reset is pinned to a FIXED wall-clock time in IST: RESET_TIME in the
RESET_TZ zone (default 02:30 Asia/Kolkata). It does NOT follow US daylight
saving — it fires at exactly 02:30 IST every day, year round.

It works by computing a canonical "session id" = the calendar date (in RESET_TZ)
of the most recent RESET_TIME that has already passed. That id changes EXACTLY
once per day, the moment 02:30 IST passes, and is independent of the server's
own local timezone (datetime.now(tz=RESET_TZ) always converts the absolute
instant correctly). The daily anchor fires once per new session id.
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
ADX_THRESHOLD = 35.0    # shared calm/trend line for BOTH anchoring and entry

# --- Level gate (current ADX vs the fixed threshold) ---
# "trend" -> ADX > threshold   (only trade strong trends)
# "range" -> ADX < threshold   (grid-friendly: only trade calm/chop)
# "off"   -> ignore the threshold entirely
ADX_MODE = "range"

# --- Slope gate (current ADX vs its own moving average) ---
# Compares the latest ADX to the average of the last ADX_AVG_PERIOD values.
#   "rising"  -> enter only when ADX > its average  (trend strengthening)
#   "falling" -> enter only when ADX < its average  (trend weakening -> good for grids)
#   "off"     -> ignore slope
# Both gates must pass to enter. Set the one(s) you don't want to "off".
ADX_SLOPE_MODE = "off"
ADX_AVG_PERIOD = 5      # how many recent ADX values to average for the slope gate

# --- Anchor gate ---
# The grid is only ANCHORED/built when the market is calm by the SAME rule as
# entries (ADX < ADX_THRESHOLD when ADX_MODE="range"). This keeps the levels and
# the entry condition in one consistent regime: levels are never laid down during
# a trend only to have entries blocked, and vice-versa. If True and the market is
# not calm at re-anchor time, anchoring waits until a later tick when it is calm.
REQUIRE_CALM_TO_ANCHOR = True

# --- Intraday regime handling ---
# A grid is only valid while ADX stays in the calm regime it was built in.
#
# REBUILD_ON_CALM_RETURN:
#   True  -> rebuild a fresh grid at the current price ANY time calm returns,
#            not just after the daily reset. The grid only ever lives inside one
#            continuous calm regime, so levels and entries never drift apart.
#   False -> only (re)anchor at the daily reset (still calm-gated).
REBUILD_ON_CALM_RETURN = True

# When ADX LEAVES calm (e.g. rises above the threshold), the grid's regime has
# expired. We always stop opening new positions and mark the grid inactive.
# FLATTEN_ON_REGIME_EXIT decides what happens to ALREADY-OPEN positions:
#   False -> let them run to their own live-price TP (daily loss limit still
#            protects you). Gentler; default.
#   True  -> flatten everything immediately at market when calm is lost.
FLATTEN_ON_REGIME_EXIT = False

# ================= SESSION / RE-ANCHOR =================

# ================= SESSION / RE-ANCHOR =================

# Daily reset is a FIXED wall-clock time, year round. Default: 02:30 IST.
# Change RESET_TIME / RESET_TZ here if you ever want a different instant.
RESET_TZ = ZoneInfo("Asia/Kolkata")
RESET_TIME = dt_time(2, 30)


def current_session_id(now_utc=None):
    """
    Canonical session identifier = the calendar date (in RESET_TZ) of the most
    recent RESET_TIME that has ALREADY occurred. With the defaults this is "the
    date of the last 02:30 IST that passed".

    Properties:
      * Fixed wall-clock — fires at exactly 02:30 IST every day, NOT following
        US daylight saving. No seasonal drift.
      * Deterministic — depends only on the absolute instant, never on the
        server's own local timezone. datetime.now(tz=RESET_TZ) converts the
        instant correctly whether the box is set to IST, UTC, JST, anything.
      * Fires exactly once per day — the id changes the moment 02:30 IST passes
        and stays constant until the next 02:30 IST.

    Returns a `datetime.date` in RESET_TZ.

    Example (RESET_TIME = 02:30 IST):
      Tue 02:29 IST -> session id = Monday's date   (today's 02:30 not yet here)
      Tue 02:30 IST -> session id = Tuesday's date  (new session begins)
      Tue 14:00 IST -> session id = Tuesday's date
      Wed 01:00 IST -> session id = Tuesday's date  (Wed 02:30 not yet here)
    """
    now_local = (now_utc.astimezone(RESET_TZ)
                 if now_utc is not None else datetime.now(RESET_TZ))
    if now_local.time() >= RESET_TIME:
        # Today's reset already passed -> this session is keyed to today.
        return now_local.date()
    # Today's reset hasn't happened yet -> still in yesterday's session.
    return (now_local - timedelta(days=1)).date()


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
    # Only write when the grid snapshot actually changed (fills/rebuilds),
    # not every 3s tick. Avoids a disk write + DataFrame build per loop.
    snapshot = tuple(
        (lvl["index"], lvl["price"], lvl["side"], lvl["filled"])
        for lvl in state["grid"]
    )
    if state.get("_last_saved_snapshot") == snapshot:
        return
    state["_last_saved_snapshot"] = snapshot

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

# Cache so we only refetch + recompute ADX when a NEW 15m bar appears, instead
# of every 3s tick. Keyed per symbol on the latest candle timestamp.
_adx_cache = {}  # symbol -> {"key": <last bar ts>, "val": (adx_now, adx_avg)}

# Throttle the candle network fetch: don't hit the API more than once every
# ADX_FETCH_EVERY_SEC. 15m data can't change faster than that anyway.
_candle_cache = {}  # symbol -> {"ts": <monotonic>, "df": <DataFrame>}
ADX_FETCH_EVERY_SEC = 60


def _fetch_adx_candles(symbol):
    """Fetch ADX_TIMEFRAME candles, throttled by ADX_FETCH_EVERY_SEC."""
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


def _col(df, *candidates):
    """Resolve a column by case-insensitive name, trying each candidate."""
    lookup = {str(c).lower(): c for c in df.columns}
    for cand in candidates:
        if cand in lookup:
            return df[lookup[cand]]
    return None


def compute_adx(symbol):
    """
    Return (adx_now, adx_avg) for the latest CLOSED ADX_TIMEFRAME bar.
    Cached two ways: candle fetch is throttled, and the TA recompute only
    runs when a new bar arrives. Most ticks are effectively free.
    Returns (None, None) if data is unavailable.
    """
    df = _fetch_adx_candles(symbol)

    if df is None or len(df) < ADX_PERIOD * 2:
        return None, None

    # Cache key = timestamp of the most recent bar. If unchanged, reuse.
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
        utils.log(
            f"⚠️ ADX: OHLC columns not found in candles "
            f"(got {list(df.columns)}) — skipping ADX this tick",
            tg=False,
        )
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
    closed = series.iloc[:-1]  # last CLOSED bar and earlier

    adx_now = float(closed.iloc[-1])
    n = min(ADX_AVG_PERIOD, len(closed))
    adx_avg = float(closed.iloc[-n:].mean()) if n > 0 else None

    val = (adx_now, adx_avg)
    _adx_cache[symbol] = {"key": bar_key, "val": val}
    return val


def adx_threshold_ok(adx_now):
    """
    The level/threshold component, shared by BOTH the entry gate and the
    anchor gate so levels and entries stay in one regime.
        "trend" -> adx_now > ADX_THRESHOLD
        "range" -> adx_now < ADX_THRESHOLD
        "off"   -> always passes
    Missing ADX blocks (conservative).
    """
    if ADX_MODE == "off":
        return True
    if adx_now is None:
        return False
    if ADX_MODE == "trend":
        return adx_now > ADX_THRESHOLD
    if ADX_MODE == "range":
        return adx_now < ADX_THRESHOLD
    return True


def adx_allows_entry(adx_now, adx_avg):
    """
    Two independent gates; BOTH must pass.

      Level gate (ADX_MODE): see adx_threshold_ok.

      Slope gate (ADX_SLOPE_MODE):
        "rising"  -> adx_now > adx_avg   (trend strengthening)
        "falling" -> adx_now < adx_avg   (trend weakening; grid-friendly)
        "off"     -> always passes

    Conservative: if a needed value is missing, that gate blocks entry.
    """
    # ----- level gate (shared with anchoring) -----
    level_ok = adx_threshold_ok(adx_now)

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

def _build_fresh_grid(state, symbol, price, now, session_id, adx_now, reason):
    """Flatten stragglers, lay a fresh grid at price, reset daily counters."""
    if state["grid"] is not None and state["positions"]:
        for posn in list(state["positions"]):
            _close_position(state, symbol, posn, price, now, reason)

    state["anchor"] = price
    state["grid"] = build_grid(price)
    state["grid_active"] = True
    state["trading_enabled"] = True
    state["last_session_id"] = session_id
    # Only reset the daily PnL on the US-close session anchor, NOT on every
    # intraday calm-return rebuild (otherwise the daily target/loss would keep
    # resetting through the day).
    if reason == "SESSION-ANCHOR":
        state["daily_pnl"] = 0

    adx_txt = f"{adx_now:.1f}" if adx_now is not None else "n/a"
    utils.log(
        f"🌙 {symbol} GRID BUILT [{reason}] (calm, ADX15m {adx_txt}) @ {price} | "
        f"levels ±{GRID_STEP}..±{GRID_BOUNDARY} | session={session_id}",
        tg=True,
    )


def maybe_reanchor(state, symbol, price, now, adx_now, adx_avg):
    """
    Decide whether to (re)build the grid this tick. Two triggers, both
    calm-gated so levels and entries always share one regime:

      1. SESSION-ANCHOR: first run, or once per daily reset (02:30 IST). Driven
         by the canonical session id, which changes deterministically at each
         02:30 IST regardless of the server's own timezone.
      2. CALM-RETURN: the grid was invalidated by a trend and ADX has now
         dropped back into the calm regime (only if REBUILD_ON_CALM_RETURN).

    Anchoring never happens unless the market is calm right now.
    """
    session_id = current_session_id()

    calm = adx_threshold_ok(adx_now) if REQUIRE_CALM_TO_ANCHOR else True

    # Trigger 1: daily session anchor.
    # Fires on first run (no grid yet) OR when the session id has advanced past
    # the one we last anchored in — i.e. a new 02:30 IST reset has occurred.
    # Because session_id only changes once per 02:30 IST, this fires exactly
    # once per session, with no time-of-day window to get wrong.
    session_due = (
        state["grid"] is None
        or state.get("last_session_id") != session_id
    )
    if session_due:
        if not calm:
            # Wait for calm; DON'T stamp last_session_id, so we retry next tick.
            return
        _build_fresh_grid(
            state, symbol, price, now, session_id, adx_now, "SESSION-ANCHOR"
        )
        return

    # Trigger 2: intraday rebuild once calm returns to a previously
    # invalidated (trend-expired) grid.
    if (
        REBUILD_ON_CALM_RETURN
        and not state.get("grid_active", True)
        and calm
    ):
        _build_fresh_grid(
            state, symbol, price, now, session_id, adx_now, "CALM-RETURN"
        )


def check_regime(state, symbol, price, now, adx_now):
    """
    Invalidate the grid if ADX has LEFT the calm regime. Always stops new
    entries and marks the grid inactive. Optionally flattens open positions.
    Returns True if the grid is currently active/tradeable, else False.
    """
    if state["grid"] is None:
        return False

    calm = adx_threshold_ok(adx_now) if REQUIRE_CALM_TO_ANCHOR else True

    if calm:
        return state.get("grid_active", True)

    # Not calm -> regime expired. Invalidate once (avoid repeat logging).
    if state.get("grid_active", True):
        adx_txt = f"{adx_now:.1f}" if adx_now is not None else "n/a"
        utils.log(
            f"⚠️ {symbol} GRID REGIME EXPIRED (ADX15m {adx_txt} left calm) "
            f"— no new entries"
            + (" — flattening open positions" if FLATTEN_ON_REGIME_EXIT else
               " — open trades run to their own TP"),
            tg=True,
        )
        state["grid_active"] = False

        if FLATTEN_ON_REGIME_EXIT:
            for posn in list(state["positions"]):
                _close_position(state, symbol, posn, price, now, "REGIME-EXIT")

    return False


# ================= STRATEGY =================

def process_symbol(symbol, price, state):
    now = datetime.now()

    # Compute ADX once per tick; reused for anchoring, regime check, and entry.
    adx_now, adx_avg = compute_adx(symbol)

    maybe_reanchor(state, symbol, price, now, adx_now, adx_avg)
    save_processed_data(state, symbol)

    # No grid yet (e.g. waiting for first calm window) -> nothing to do.
    if state["grid"] is None:
        return

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
                f"— stopping for the day (resets after 02:30 IST)",
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
                f"— halted for the day (resets after 02:30 IST)",
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

    # ---------- REGIME CHECK ----------
    # Invalidate the grid if ADX has left the calm regime it was built in.
    # (Open positions were already handled by the exit loop above and, if
    #  FLATTEN_ON_REGIME_EXIT, are closed inside check_regime.)
    if not check_regime(state, symbol, price, now, adx_now):
        return  # grid not active in this regime -> no new entries

    # ---------- ADX ENTRY GATE (threshold + slope) ----------
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
            "grid_active": False,
            "anchor": None,
            "last_candle_time": None,
            "last_session_id": None,   # was last_anchor_date; now the canonical id
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
    utils.log(
        f"⚙️ Regime: require_calm_anchor={REQUIRE_CALM_TO_ANCHOR} "
        f"rebuild_on_calm={REBUILD_ON_CALM_RETURN} "
        f"flatten_on_exit={FLATTEN_ON_REGIME_EXIT}",
        tg=True,
    )
    # Surface the session math at startup so you can verify the reset instant
    # on the EC2 box no matter what its local clock is set to.
    _now_reset = datetime.now(RESET_TZ)
    utils.log(
        f"🕐 server_local={datetime.now()} | "
        f"reset_tz={_now_reset.strftime('%Y-%m-%d %H:%M %Z')} "
        f"| session_id={current_session_id()} "
        f"| next reset at next {RESET_TIME.strftime('%H:%M')} IST",
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