"""
breakout_strategy.py  (paper mode)

A price-breakout bot with a trailing stop, a take-profit, a daily profit cap,
and a 5m ADX trend filter.

THE WHOLE LOGIC
---------------
ADX FILTER (5m, gates everything):
    Compute ADX (Wilder, period 14) on 5-minute candles.
    A trade is allowed ONLY when:
        latest ADX >= 30  AND  latest ADX >= average of last 5 ADX values.
    This filter gates BOTH:
        - setting the anchor (no levels are placed until ADX qualifies), and
        - the breakout entry itself (re-checked at the moment of breakout).

ANCHOR:
    Set only when the ADX filter passes.
    UPPER level  = anchor + STEP   (e.g. 1000 -> 1100)
    LOWER level  = anchor - STEP   (e.g. 1000 -> 900)

ENTRY (only one position at a time):
    Price breaks ABOVE upper  -> BUY  (long)   [if ADX still OK]
    Price breaks BELOW lower   -> SELL (short)  [if ADX still OK]

EXIT (take-profit OR trailing stop):
    TP   : LONG  exits when price >= entry + TP.
           SHORT exits when price <= entry - TP.
    LONG : SL starts at the lower level. As price makes new highs,
           SL trails 200 points below the highest price reached.
           Exit when price falls back to SL.
    SHORT: SL starts at the upper level. As price makes new lows,
           SL trails 200 points above the lowest price reached.
           Exit when price rises back to SL.

DAILY CAP:
    Once realized PnL for the day reaches DAILY_TARGET, stop trading for the
    rest of the day. Resumes automatically the next calendar day.

RE-ARM:
    After an exit, the anchor is cleared. It is re-set (re-armed) at the
    current price only once the ADX filter passes again.
"""

import os
import time
import traceback
from datetime import datetime

from dotenv import load_dotenv

from utils import TradingUtils

load_dotenv()

# ================= CONFIG =================

BOT_NAME = "breakout_strategy"
SYMBOLS = ["BTCUSD"]

DEFAULT_CONTRACTS = {"BTCUSD": 1000}
CONTRACT_SIZE = {"BTCUSD": 0.001}

TAKER_FEE = 0.0005
MIN_BALANCE = 1000
START_BALANCE = 10000
TIMEFRAME = "15m"
DAYS = 15

STEP = 200          # distance from anchor to each level (upper/lower)
TRAIL = 300         # trailing-stop gap (points)
TP = 400            # take-profit distance from entry (points)
DAILY_TARGET = 600  # stop trading for the day once realized PnL hits this

# ADX trend filter (5-minute)
ADX_TF = "15m"
ADX_PERIOD = 14
ADX_AVG_LEN = 5
ADX_MIN = 30

# ================= INIT =================

utils = TradingUtils(
    contract_size=CONTRACT_SIZE,
    taker_fee=TAKER_FEE,
    timeframe=TIMEFRAME,
    days=DAYS,
    telegram_token=os.getenv("price_trend_following_strategy"),
    telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID"),
    bot_name=BOT_NAME,
)


# ================= ADX (Wilder, 5m) =================

def compute_adx(candles, period=ADX_PERIOD):
    """
    Wilder's ADX from candles.
    Accepts a pandas DataFrame with High/Low/Close columns (capitalized or
    lowercase) or a list of dicts with those keys.
    Returns a list of ADX values (one per smoothed bar), or [] if not enough
    data.
    """
    def _col(df, *names):
        """Return the first matching column name present in df, else None."""
        for nm in names:
            if nm in df.columns:
                return nm
        return None

    # Extract high/low/close into plain lists, DataFrame or list-of-dicts.
    if hasattr(candles, "columns"):           # pandas DataFrame
        if candles.empty:
            return []
        h_col = _col(candles, "High", "high")
        l_col = _col(candles, "Low", "low")
        c_col = _col(candles, "Close", "close")
        if not (h_col and l_col and c_col):
            utils.log(
                f"🚫 ADX: candle columns not found "
                f"(have {list(candles.columns)})",
                tg=True,
            )
            return []
        highs  = candles[h_col].tolist()
        lows   = candles[l_col].tolist()
        closes = candles[c_col].tolist()
    else:                                     # list of dicts (or None)
        if not candles:
            return []
        def _get(d, *names):
            for nm in names:
                if nm in d:
                    return d[nm]
            raise KeyError(names)
        highs  = [_get(c, "High", "high")  for c in candles]
        lows   = [_get(c, "Low", "low")    for c in candles]
        closes = [_get(c, "Close", "close") for c in candles]

    n_bars = len(closes)
    if n_bars < 2 * period + 1:
        return []

    plus_dm, minus_dm, tr = [], [], []
    for i in range(1, n_bars):
        up_move   = highs[i] - highs[i - 1]
        down_move = lows[i - 1] - lows[i]
        plus_dm.append(up_move   if (up_move > down_move and up_move > 0) else 0.0)
        minus_dm.append(down_move if (down_move > up_move and down_move > 0) else 0.0)
        tr.append(max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i]  - closes[i - 1]),
        ))

    # Wilder smoothing (RMA)
    def rma(values, n):
        if len(values) < n:
            return []
        out = [sum(values[:n]) / n]
        for v in values[n:]:
            out.append((out[-1] * (n - 1) + v) / n)
        return out

    atr   = rma(tr, period)
    pdm_s = rma(plus_dm, period)
    mdm_s = rma(minus_dm, period)
    if not atr or not pdm_s or not mdm_s:
        return []

    dx = []
    for a, p, m in zip(atr, pdm_s, mdm_s):
        if a == 0:
            dx.append(0.0)
            continue
        pdi = 100 * (p / a)
        mdi = 100 * (m / a)
        denom = pdi + mdi
        dx.append(0.0 if denom == 0 else 100 * abs(pdi - mdi) / denom)

    adx = rma(dx, period)   # ADX = Wilder-smoothed DX
    return adx


def _fetch_candles(symbol, tf):
    """
    Pull candles from utils, trying the call signatures your utils may expose.
    Returns whatever utils returns (DataFrame expected), or None.
    """
    for attempt in (
        lambda: utils.fetch_candles(symbol, timeframe=tf),
        lambda: utils.fetch_candles(symbol, tf),
        lambda: utils.fetch_candles(symbol, resolution=tf),
        lambda: utils.fetch_candles(symbol),
    ):
        try:
            return attempt()
        except TypeError:
            continue
        except Exception:
            raise
    return None


# Simple per-symbol cache so we don't refetch 5m candles every tick.
_adx_cache = {}   # symbol -> {"ts": epoch_seconds, "adx": [...]}
ADX_REFRESH_SEC = 60   # recompute ADX at most once a minute


def _get_adx(symbol):
    """Return the cached ADX list, refreshing at most once per ADX_REFRESH_SEC."""
    nowt = time.time()
    cached = _adx_cache.get(symbol)
    if cached is not None and (nowt - cached["ts"]) < ADX_REFRESH_SEC:
        return cached["adx"]

    candles = _fetch_candles(symbol, ADX_TF)
    adx = compute_adx(candles)
    _adx_cache[symbol] = {"ts": nowt, "adx": adx}
    return adx


def adx_filter_ok(symbol):
    """
    True only if the latest 5m ADX is >= ADX_MIN AND >= the average of the
    last ADX_AVG_LEN ADX values. Both conditions must hold.
    """
    adx = _get_adx(symbol)

    if len(adx) < ADX_AVG_LEN:
        # utils.log("⏳ ADX: not enough data yet — no trade", tg=True)
        return False

    latest = adx[-1]
    avg = sum(adx[-ADX_AVG_LEN:]) / ADX_AVG_LEN

    if latest < ADX_MIN:
        # utils.log(f"🚫 ADX {latest:.1f} < {ADX_MIN} — no trade", tg=True)
        return False
    if latest < avg:
        # utils.log(f"🚫 ADX {latest:.1f} < avg {avg:.1f} — no trade", tg=True)
        return False

    # utils.log(f"✅ ADX {latest:.1f} >= {ADX_MIN} and >= avg {avg:.1f}", tg=True)
    return True


# ================= ANCHOR =================

def set_anchor(state, price, reason):
    """Re-center the levels at the current price."""
    state["anchor"] = price
    state["upper"] = price + STEP
    state["lower"] = price - STEP

    utils.log(
        f"🎯 ANCHOR [{reason}] @ {price} | upper {state['upper']} "
        f"lower {state['lower']}",
        tg=True,
    )


# ================= POSITION HELPERS =================

def _close_position(state, symbol, exit_price, now, reason):
    """Book PnL after fees, log it, and clear the position."""
    posn = state["position"]
    if posn["side"] == "long":
        gross = (exit_price - posn["entry"]) * CONTRACT_SIZE[symbol] * posn["qty"]
    else:
        gross = (posn["entry"] - exit_price) * CONTRACT_SIZE[symbol] * posn["qty"]

    fees = (utils.commission(posn["entry"], posn["qty"], symbol)
            + utils.commission(exit_price, posn["qty"], symbol))
    net = gross - fees

    state["balance"] += net
    state["daily_pnl"] += net
    state["position"] = None

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
        f"{emoji} {symbol} EXIT ({reason}) @ {exit_price} | PNL: {round(net, 6)}",
        tg=True,
    )
    utils.log(f"💰 Balance: {round(state['balance'], 2)}", tg=True)


def _open_position(state, symbol, side, price, sl_price, now):
    # Take-profit target set relative to entry.
    if side == "long":
        tp_price = price + TP
    else:
        tp_price = price - TP

    state["position"] = {
        "side": side,
        "entry": price,
        "sl_price": sl_price,
        "tp_price": tp_price,      # take-profit target
        "extreme": price,          # highest (long) / lowest (short) seen so far
        "qty": DEFAULT_CONTRACTS[symbol],
        "entry_time": now,
    }
    emoji = "🟢" if side == "long" else "🔴"
    utils.log(
        f"{emoji} {symbol} {side.upper()} @ {price} | SL→ {sl_price} "
        f"(trail {TRAIL}) | TP→ {tp_price}",
        tg=True,
    )


# ================= STRATEGY =================

def process_symbol(symbol, price, state):
    now = datetime.now()

    # Reset daily PnL when the date changes.
    if now.date() != state["current_day"]:
        state["current_day"] = now.date()
        state["daily_pnl"] = 0.0
        state["capped"] = False
        utils.log("📅 New day — daily PnL reset", tg=True)

    posn = state["position"]

    # ---------- MANAGE OPEN POSITION (take-profit + trailing stop) ----------
    if posn is not None:
        if posn["side"] == "long":
            # Take-profit first.
            if price >= posn["tp_price"]:
                _close_position(state, symbol, price, now, "TP")
                state["anchor"] = None   # re-arm waits for ADX next tick
            else:
                # Trail the stop up as new highs print.
                if price > posn["extreme"]:
                    posn["extreme"] = price
                    posn["sl_price"] = max(posn["sl_price"], price - TRAIL)
                if price <= posn["sl_price"]:
                    _close_position(state, symbol, price, now, "TSL")
                    state["anchor"] = None   # re-arm waits for ADX next tick
        else:
            # Take-profit first.
            if price <= posn["tp_price"]:
                _close_position(state, symbol, price, now, "TP")
                state["anchor"] = None   # re-arm waits for ADX next tick
            else:
                # Trail the stop down as new lows print.
                if price < posn["extreme"]:
                    posn["extreme"] = price
                    posn["sl_price"] = min(posn["sl_price"], price + TRAIL)
                if price >= posn["sl_price"]:
                    _close_position(state, symbol, price, now, "TSL")
                    state["anchor"] = None   # re-arm waits for ADX next tick

        # After managing, flag if the day's target is now reached.
        if state["daily_pnl"] >= DAILY_TARGET and not state["capped"]:
            state["capped"] = True
            utils.log(
                f"🎯 Daily target hit: {round(state['daily_pnl'], 2)} "
                f"— done for today",
                tg=True,
            )
        return

    # ---------- DAILY CAP ----------
    if state["daily_pnl"] >= DAILY_TARGET:
        if not state["capped"]:
            state["capped"] = True
            utils.log(
                f"🎯 Daily target hit: {round(state['daily_pnl'], 2)} "
                f"— done for today",
                tg=True,
            )
        return

    # ---------- BALANCE GUARD ----------
    if state["balance"] < MIN_BALANCE:
        utils.log(f"⚠️ Balance low: {state['balance']}", tg=True)
        return

    # ---------- ANCHOR GATE (ADX) ----------
    # No levels exist until ADX qualifies. This covers START and RE-ARM.
    if state["anchor"] is None:
        if adx_filter_ok(symbol):
            set_anchor(state, price, "START/RE-ARM")
        return   # nothing to break out of yet this tick

    # ---------- BREAKOUT ENTRY (ADX re-checked) ----------
    if price > state["upper"]:
        if adx_filter_ok(symbol):
            # Buy breakout: initial SL at the lower level.
            _open_position(state, symbol, "long", price, state["lower"], now)
    elif price < state["lower"]:
        if adx_filter_ok(symbol):
            # Sell breakout: initial SL at the upper level.
            _open_position(state, symbol, "short", price, state["upper"], now)


# ================= MAIN =================

def run():
    state = {
        s: {
            "position": None,
            "anchor": None,
            "upper": None,
            "lower": None,
            "balance": START_BALANCE,
            "daily_pnl": 0.0,
            "current_day": datetime.now().date(),
            "capped": False,
        }
        for s in SYMBOLS
    }

    utils.log("🚀 BREAKOUT BOT STARTED (PAPER MODE)", tg=True)
    utils.log(
        f"⚙️ Breakout: buy > anchor+{STEP}, sell < anchor-{STEP} "
        f"| trail {TRAIL} | TP {TP} | daily cap {DAILY_TARGET} "
        f"| ADX{ADX_PERIOD}@{ADX_TF} >= {ADX_MIN} & >= avg{ADX_AVG_LEN}",
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