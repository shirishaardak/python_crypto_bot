import os
import time
import json
import threading
import pandas as pd
import numpy as np

from dataclasses import dataclass, field
from datetime import datetime
from collections import deque

import websocket  # pip install websocket-client

from dotenv import load_dotenv

import traceback
import subprocess

from utils import TradingUtils

load_dotenv()

# ============================================================
# STRATEGY CORE (inlined — was sr_core.py)
# ============================================================
# Single source of truth for the S/R bounce + breakout strategy.
# Pure logic — no data source, no broker, no Telegram.

# ----- CONFIG (the single place to tune the strategy) -----
SR_SYMBOLS = ["BTCUSD", "ETHUSD", "SOLUSD"]

CORE_TIMEFRAME = "15m"
BAR_MINUTES = 15

# --- structure ---
TREND_BARS = 16            # 16 * 15m = 4h trend window
SR_BARS = 80               # 80 * 15m = 20h S/R window
SR_TOL_PCT = 0.25          # cluster swings within 0.25% of each other
SWING_LEFT = 3
SWING_RIGHT = 3
FLAT_THRESHOLD_PCT = 0.3   # |net move| under this over window -> sideways
TOUCH_ATR_FRAC = 0.5       # "at the level" tolerance, as a fraction of ATR

# --- risk (the locked-in rules) ---
ATR_PERIOD = 14
SL_ATR_MULT = 1.3         # stop gap beyond the level = ATR * 0.5 (tighter)
RR = 3.0                   # take-profit = R * RR  -> 1:3
TSL_ARM_R = 0.5            # arm trailing stop after +0.5R of open profit
TSL_STEP_ATR_MULT = 0.5    # step-trail: stop moves in chunks of ATR * 0.5,
                           # and only after best price advances a full step

# --- accounting ---
START_BALANCE = 10000.0
TAKER_FEE = 0.0005
MIN_BALANCE = 1000
CONTRACT_SIZE = {"BTCUSD": 0.001, "ETHUSD": 0.01, "SOLUSD": 1.0}
DEFAULT_CONTRACTS = {"BTCUSD": 1000, "ETHUSD": 1000, "SOLUSD": 1000}


# ----- INDICATORS -----
def atr_series(high, low, close, period=ATR_PERIOD):
    """Wilder ATR as a pandas Series (no external indicator lib)."""
    h = np.asarray(high, float)
    l = np.asarray(low, float)
    c = np.asarray(close, float)
    prev_c = np.roll(c, 1)
    if len(c):
        prev_c[0] = c[0]
    tr = np.maximum.reduce([h - l, np.abs(h - prev_c), np.abs(l - prev_c)])
    idx = high.index if isinstance(high, pd.Series) else None
    return pd.Series(tr, index=idx).ewm(alpha=1.0 / period, adjust=False).mean()


def atr_last(high, low, close, period=ATR_PERIOD):
    """Scalar latest ATR, or np.nan if not computable."""
    s = atr_series(high, low, close, period).dropna()
    return float(s.iloc[-1]) if len(s) else np.nan


# ----- TREND -----
def analyse_trend(closes):
    """
    Trend over the last TREND_BARS closes -> "up" / "down" / "sideways".
    Regression slope sign + EMA-fast vs EMA-slow agreement.
    Accepts a list / ndarray / Series of closes (uses the tail).
    """
    closes = np.asarray(closes, float)[-TREND_BARS:]
    n = len(closes)
    if n < 5:
        return "sideways"
    x = np.arange(n)
    slope, intercept = np.polyfit(x, closes, 1)
    fit = slope * x + intercept
    slope_pct = (fit[-1] - fit[0]) / fit[0] * 100.0
    ser = pd.Series(closes)
    ema_fast = ser.ewm(span=max(3, n // 6), adjust=False).mean().iloc[-1]
    ema_slow = ser.ewm(span=max(6, n // 3), adjust=False).mean().iloc[-1]
    if abs(slope_pct) < FLAT_THRESHOLD_PCT:
        return "sideways"
    if slope_pct > 0 and ema_fast >= ema_slow:
        return "up"
    if slope_pct < 0 and ema_fast <= ema_slow:
        return "down"
    return "sideways"


# ----- SUPPORT / RESISTANCE -----
def _swings(H, L):
    H = np.asarray(H, float)
    L = np.asarray(L, float)
    highs, lows = [], []
    for i in range(SWING_LEFT, len(H) - SWING_RIGHT):
        wh = H[i - SWING_LEFT:i + SWING_RIGHT + 1]
        wl = L[i - SWING_LEFT:i + SWING_RIGHT + 1]
        if H[i] == wh.max() and wh.argmax() == SWING_LEFT:
            highs.append(float(H[i]))
        if L[i] == wl.min() and wl.argmin() == SWING_LEFT:
            lows.append(float(L[i]))
    return highs, lows


def _cluster(levels, tol_pct=SR_TOL_PCT):
    if not levels:
        return []
    levels = sorted(levels)
    groups, g = [], [levels[0]]
    for p in levels[1:]:
        if abs(p - g[-1]) / g[-1] * 100 <= tol_pct:
            g.append(p)
        else:
            groups.append(g)
            g = [p]
    groups.append(g)
    return [float(np.mean(x)) for x in groups]


def support_resistance(H, L, price):
    """
    Nearest support below `price` and nearest resistance above it, from
    clustered swing highs/lows. H, L are arrays/Series for the S/R window.
    Returns (support_or_None, resistance_or_None).
    """
    H = np.asarray(H, float)
    L = np.asarray(L, float)
    highs, lows = _swings(H, L)
    if len(H):
        highs.append(float(H.max()))
        lows.append(float(L.min()))
    res = [p for p in _cluster(highs) if p > price]
    sup = [p for p in _cluster(lows) if p < price]
    return (max(sup) if sup else None), (min(res) if res else None)


# ----- SIGNAL DETECTION -----
def detect_signal(trend, support, resistance, atr, close, low, high):
    """
    Return {kind, side, level} or None. Bounce preferred over breakout.
        bounce long  : dip into support, close back above it  (up/sideways)
        bounce short : poke resistance, close back below it    (down/sideways)
        breakout long: close above resistance                  (up/sideways)
        breakout short: close below support                    (down/sideways)
    """
    if atr is None or np.isnan(atr) or atr <= 0:
        return None
    touch = atr * TOUCH_ATR_FRAC
    long_ok = trend in ("up", "sideways")
    short_ok = trend in ("down", "sideways")

    if long_ok and support is not None and low <= support + touch and close > support:
        return {"kind": "bounce", "side": "long", "level": support}
    if short_ok and resistance is not None and high >= resistance - touch and close < resistance:
        return {"kind": "bounce", "side": "short", "level": resistance}
    if long_ok and resistance is not None and close > resistance:
        return {"kind": "breakout", "side": "long", "level": resistance}
    if short_ok and support is not None and close < support:
        return {"kind": "breakout", "side": "short", "level": support}
    return None


# ----- RISK -----
def build_risk(side, entry, level, atr):
    """
    SL beyond the level by an ATR gap; R = |entry-SL|; TP = entry +/- R*RR.
    Returns (sl_price, tp_price, R).
    """
    gap = atr * SL_ATR_MULT
    if side == "long":
        sl = level - gap
        R = entry - sl
        tp = entry + R * RR
    else:
        sl = level + gap
        R = sl - entry
        tp = entry - R * RR
    return sl, tp, R


def core_commission(price, qty, symbol):
    return abs(price) * qty * CONTRACT_SIZE[symbol] * TAKER_FEE


# ----- POSITION + TRAILING STOP -----
@dataclass
class Position:
    side: str            # "long" | "short"
    kind: str            # "bounce" | "breakout"
    entry: float
    sl: float
    tp: float
    R: float
    level: float
    qty: int
    best: float = 0.0
    tsl_armed: bool = False
    entry_time: object = None
    extra: dict = field(default_factory=dict)

    def __post_init__(self):
        if not self.best:
            self.best = self.entry


def update_trailing(pos, ref_price):
    """
    Step-based (chunky) trailing stop.

    Arm the TSL once open profit reaches +TSL_ARM_R*R. Once armed, the stop
    only moves when the BEST price has advanced by a full `step` beyond where
    the stop was last placed. Each full step, the stop jumps up (long) / down
    (short) to sit one `step` behind the best price. Moves less than a full
    step do NOT move the stop. The stop only ever moves favorably.

    `step` is taken from pos["step"] (set at entry = ATR * TSL_STEP_ATR_MULT).
    If absent, it falls back to R (so it never crashes on an old position).

    `pos` may be a Position or a plain dict with keys
    ("side","entry","R","best","tsl_armed","sl"[,"step"]). Mutates in place.

    For LIVE use, pass the current price as ref_price.
    For BACKTEST use, pass the bar extreme in the trade's favor
    (high for longs, low for shorts) to model the best intrabar excursion.
    """
    is_dict = isinstance(pos, dict)

    def g(k, default=None):
        return (pos.get(k, default) if is_dict else getattr(pos, k, default))

    side = g("side")
    entry = g("entry")
    R = g("R")
    best = g("best")
    armed = g("tsl_armed")
    sl = g("sl")
    step = g("step") or R          # fall back to R if step missing
    if not step or step <= 0:
        step = R

    if side == "long":
        best = max(best, ref_price)
        if not armed and (best - entry) >= TSL_ARM_R * R:
            armed = True
        if armed:
            # how many full steps is the best price above the current stop?
            steps = int((best - sl) // step)
            if steps > 1:                       # keep exactly one step behind
                sl = max(sl, sl + (steps - 1) * step)
    else:
        best = min(best, ref_price)
        if not armed and (entry - best) >= TSL_ARM_R * R:
            armed = True
        if armed:
            steps = int((sl - best) // step)
            if steps > 1:
                sl = min(sl, sl - (steps - 1) * step)

    if is_dict:
        pos["best"], pos["tsl_armed"], pos["sl"] = best, armed, sl
    else:
        pos.best, pos.tsl_armed, pos.sl = best, armed, sl
    return pos


# ================= CONFIG =================

BOT_NAME = "support_resistance_strategy"

SYMBOLS = ["BTCUSD", "ETHUSD", "SOLUSD"]

# Accounting knobs (formerly pulled from sr_core, now defined above).
# Keep the full multi-symbol dicts so all symbols in SYMBOLS are covered.
CORE_DEFAULT_CONTRACTS = DEFAULT_CONTRACTS
CORE_CONTRACT_SIZE = CONTRACT_SIZE

DEFAULT_CONTRACTS = {s: CORE_DEFAULT_CONTRACTS[s] for s in SYMBOLS}

live_DEFAULT_CONTRACTS = {
    "BTCUSD": 1,
    "ETHUSD": 1,
    "SOLUSD": 1,
}

CONTRACT_SIZE = {s: CORE_CONTRACT_SIZE[s] for s in SYMBOLS}

# TAKER_FEE already defined above in the inlined core.

# ----- SINGLE TIMEFRAME (sr_core style) -----
# Everything (levels + entries + exits) is computed on one execution TF.
EXEC_TF = CORE_TIMEFRAME             # "15m"
ALL_TFS = [EXEC_TF]                  # only stream we subscribe to / seed

TIMEFRAME = EXEC_TF

DAYS = 15

# MIN_BALANCE already defined above in the inlined core.

# Bars of history needed before logic is valid (whichever window is longest).
MIN_BARS = max(TREND_BARS, SR_BARS, ATR_PERIOD) + 5

# ================= WEBSOCKET CONFIG =================

# Production Delta India socket. For testnet use:
#   wss://socket-ind.testnet.deltaex.org
WS_URL = os.getenv("DELTA_WS_URL", "wss://socket.india.delta.exchange")

WS_RESOLUTIONS = ALL_TFS

# How many candles to keep in memory (must cover your strategy lookback)
MAX_CANDLES = 5000

# ================= INIT UTILS =================

utils = TradingUtils(
    contract_size=CONTRACT_SIZE,
    taker_fee=TAKER_FEE,
    timeframe=TIMEFRAME,
    days=DAYS,
    telegram_token=os.getenv("price_trend_following_strategy_live_bot"),
    telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID"),
    bot_name=BOT_NAME
)

BASE_DIR = os.getcwd()

SAVE_DIR = os.path.join(
    BASE_DIR,
    "data",
    "support_resistance_strategy"
)

os.makedirs(SAVE_DIR, exist_ok=True)

# ================= LIVE MARKET STATE (shared across threads) =================

def _new_tf_buffer():
    return {
        "candles": deque(maxlen=MAX_CANDLES),   # list of dicts
        "candle_index": {},                     # candle_start_time -> pos
        "last_seen_candle_start": None,
    }


market = {
    s: {
        "price": None,
        "tf": {tf: _new_tf_buffer() for tf in ALL_TFS},
    } for s in SYMBOLS
}

market_lock = threading.Lock()
ws_ready = threading.Event()


# ================= SAVE DATA =================

def save_processed_data(df, symbol, support, resistance, trend):
    """Persist the latest computed view (OHLC + nearest S/R + trend label)."""
    path = os.path.join(SAVE_DIR, f"{symbol}_processed.csv")
    out = df.copy()
    out["support"] = support
    out["resistance"] = resistance
    out["trend"] = trend
    out.to_csv(path, index=True)


# ================= PAPER ORDER =================

def place_market_order(symbol, side, qty):

    utils.log(
        f"📝 PAPER ORDER → {symbol} {side.upper()} {qty}",
        tg=True
    )

    return {"success": True}


# ================= STRATEGY =================
# Single-timeframe, paper-trading. Levels come from support_resistance
# on the exec TF; entries come from detect_signal (trend-filtered
# bounce/breakout); risk from build_risk; trailing from update_trailing.
# Fills assumed at the just-closed candle's close.

def process_symbol(symbol, exec_df, state, is_new_candle):

    df = exec_df

    # Need at least the last CLOSED candle plus history.
    if len(df) < MIN_BARS + 2:
        return

    # Use the last CLOSED bar (index -2); -1 is still forming.
    last = df.iloc[-2]

    # ----- windows for the core logic (everything on the exec TF) -----
    closes = df["Close"].to_numpy()
    H = df["High"].to_numpy()
    L = df["Low"].to_numpy()
    C = df["Close"].to_numpy()

    # ATR up to (and including) the just-closed bar.
    atr = atr_last(
        df["High"].iloc[:-1],
        df["Low"].iloc[:-1],
        df["Close"].iloc[:-1],
        ATR_PERIOD,
    )

    # Trend over the last TREND_BARS closes (excluding the forming bar).
    trend = analyse_trend(closes[:-1])

    # Nearest support below / resistance above, from the S/R window.
    sr_window = slice(max(0, len(df) - 1 - SR_BARS), len(df) - 1)
    support, resistance = support_resistance(
        H[sr_window], L[sr_window], float(last.Close)
    )

    # save_processed_data(df.iloc[:-1], symbol, support, resistance, trend)

    pos = state["position"]

    now = datetime.now()

    # ================= EXIT (evaluated on candle close) =================
    # Exits use the just-closed candle's close, so entries and exits share
    # the same timing. Trailing stop is updated using the bar extreme in the
    # trade's favor (update_trailing), then SL/TP are checked.

    if pos:

        # Don't evaluate an exit on the very same candle we entered on.
        # Wait for the next closed bar so entries/exits can't both fire on
        # one bar (which produced fee-only round-trips at startup).
        if pos.get("entry_candle") == df.index[-2]:
            return

        exit_trade = False

        pnl = 0

        close_px = float(last.Close)

        # ----- update trailing stop using bar extreme in trade's favor -----
        ref = float(last.High) if pos["side"] == "long" else float(last.Low)
        update_trailing(pos, ref)        # mutates pos["sl"], pos["best"], etc.

        # ================= LONG EXIT =================
        # TP (R*RR above entry) or SL (trailing / initial stop) hit on close.
        if (
            pos["side"] == "long"
            and (close_px >= pos["tp"] or close_px <= pos["sl"])
        ):
            exit_trade = True

        # ================= SHORT EXIT =================
        elif (
            pos["side"] == "short"
            and (close_px <= pos["tp"] or close_px >= pos["sl"])
        ):
            exit_trade = True

        # ================= FINAL EXIT =================
        if exit_trade:

            exit_side = "sell" if pos["side"] == "long" else "buy"
            place_market_order(symbol, exit_side, pos["qty"])

            fill_price = close_px

            if pos["side"] == "long":
                pnl = (fill_price - pos["entry"]) * CONTRACT_SIZE[symbol] * pos["qty"]
            else:
                pnl = (pos["entry"] - fill_price) * CONTRACT_SIZE[symbol] * pos["qty"]

            entry_fee = utils.commission(pos["entry"], pos["qty"], symbol)
            exit_fee = utils.commission(fill_price, pos["qty"], symbol)
            total_fee = entry_fee + exit_fee

            net = pnl - total_fee

            state["balance"] += net

            utils.save_trade({
                "symbol": symbol,
                "side": pos["side"],
                "entry_price": pos["entry"],
                "exit_price": fill_price,
                "qty": pos["qty"],
                "net_pnl": round(net, 6),
                "entry_time": pos["entry_time"],
                "exit_time": now
            })

            emoji = "🟢" if net > 0 else "🔴"
            utils.log(f"{emoji} {symbol} EXIT @ {fill_price} | PNL: {round(net, 6)}", tg=True)
            utils.log(f"💰 Balance: {round(state['balance'], 2)}", tg=True)

            state["position"] = None
            state["last_exit_candle"] = df.index[-1]
            return

    # ================= ENTRY =================

    if not pos and is_new_candle:

        # Avoid same-candle reentry after an exit.
        if state.get("last_exit_candle") == df.index[-1]:
            return

        balance = state["balance"]
        if balance < MIN_BALANCE:
            utils.log(f"⚠️ Balance low: {balance}", tg=True)
            return

        # ----- core signal: trend-filtered bounce/breakout -----
        sig = detect_signal(
            trend,
            support,
            resistance,
            atr,
            float(last.Close),
            float(last.Low),
            float(last.High),
        )

        if sig is None:
            return

        entry_price = float(last.Close)
        sl, tp, R = build_risk(sig["side"], entry_price, sig["level"], atr)

        order_side = "buy" if sig["side"] == "long" else "sell"
        place_market_order(symbol, order_side, DEFAULT_CONTRACTS[symbol])

        # Position carries the keys update_trailing expects.
        state["position"] = {
            "side": sig["side"],
            "kind": sig["kind"],
            "entry": entry_price,
            "level": sig["level"],
            "qty": DEFAULT_CONTRACTS[symbol],
            "entry_time": now,
            "sl": sl,
            "tp": tp,
            "R": R,
            "best": entry_price,
            "tsl_armed": False,
            "step": atr * TSL_STEP_ATR_MULT,   # fixed trail step for this trade
            "entry_candle": df.index[-2],
        }

        emoji = "🟢" if sig["side"] == "long" else "🔴"
        utils.log(
            f"{emoji} {symbol} {sig['side'].upper()} ({sig['kind']}, {trend}) @ "
            f"{round(entry_price, 2)} | level {round(sig['level'], 2)} | "
            f"TP {round(tp, 2)} | SL {round(sl, 2)} | R {round(R, 2)} "
            f"(ATR {round(atr, 2)})",
            tg=True
        )
        return


# ================= WEBSOCKET LAYER =================

def _upsert_candle(symbol, tf, candle_start, o, h, l, c):
    """Insert or update a candle keyed by its start time."""
    m = market[symbol]["tf"][tf]
    idx = m["candle_index"]

    row = {"time": candle_start, "Open": o, "High": h, "Low": l, "Close": c}

    if candle_start in idx:
        pos = idx[candle_start]
        m["candles"][pos] = row
    else:
        m["candles"].append(row)
        idx.clear()
        for p, r in enumerate(m["candles"]):
            idx[r["time"]] = p
        m["last_seen_candle_start"] = candle_start


def _tf_from_type(msg_type):
    """Map a Delta payload type like 'candlestick_15m' to '15m'."""
    suffix = msg_type.split("candlestick_", 1)[-1]
    return suffix if suffix in ALL_TFS else None


def on_message(ws, message):
    try:
        msg = json.loads(message)
    except Exception:
        return

    msg_type = msg.get("type")

    if msg_type and msg_type.startswith("candlestick"):

        symbol = msg.get("symbol")
        if symbol not in market:
            return

        tf = _tf_from_type(msg_type)
        if tf is None:
            return

        try:
            candle_start = int(msg["candle_start_time"])
            o = float(msg["open"])
            h = float(msg["high"])
            l = float(msg["low"])
            c = float(msg["close"])
        except (KeyError, TypeError, ValueError):
            return

        with market_lock:
            _upsert_candle(symbol, tf, candle_start, o, h, l, c)

    elif msg_type == "mark_price":
        raw = msg.get("symbol", "")
        symbol = raw.replace("MARK:", "")
        if symbol in market:
            try:
                with market_lock:
                    market[symbol]["price"] = float(msg["price"])
            except (KeyError, TypeError, ValueError):
                pass

    elif msg_type == "v2/ticker":
        symbol = msg.get("symbol")
        if symbol in market and msg.get("mark_price") is not None:
            try:
                with market_lock:
                    market[symbol]["price"] = float(msg["mark_price"])
            except (TypeError, ValueError):
                pass


def on_error(ws, error):
    utils.log(f"🌐 WS error: {error}", tg=True)


def on_close(ws, code, reason):
    utils.log(f"🌐 WS closed: {code} {reason}", tg=True)


def on_open(ws):
    channels = [
        {"name": f"candlestick_{tf}", "symbols": SYMBOLS}
        for tf in WS_RESOLUTIONS
    ]
    channels.append({"name": "mark_price", "symbols": [f"MARK:{s}" for s in SYMBOLS]})
    channels.append({"name": "v2/ticker", "symbols": SYMBOLS})

    sub = {"type": "subscribe", "payload": {"channels": channels}}
    ws.send(json.dumps(sub))
    utils.log(f"🌐 WS subscribed ({', '.join(WS_RESOLUTIONS)})", tg=True)
    ws_ready.set()


def _fetch_tf(symbol, tf):
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


def _seed_history():
    """Seed the single exec-TF buffer from REST history before streaming."""
    for symbol in SYMBOLS:
        for tf in ALL_TFS:
            try:
                df = _fetch_tf(symbol, tf)
                if df is None or len(df) < MIN_BARS:
                    utils.log(f"⚠️ Thin/no history for {symbol} {tf}", tg=False)
                    continue
                with market_lock:
                    m = market[symbol]["tf"][tf]
                    m["candles"].clear()
                    m["candle_index"].clear()
                    for ts, row in df.iterrows():
                        key = int(pd.Timestamp(ts).value // 1000)
                        m["candles"].append({
                            "time": key,
                            "Open": float(row["Open"]),
                            "High": float(row["High"]),
                            "Low": float(row["Low"]),
                            "Close": float(row["Close"]),
                        })
                    for p, r in enumerate(m["candles"]):
                        m["candle_index"][r["time"]] = p
                    if m["candles"]:
                        m["last_seen_candle_start"] = m["candles"][-1]["time"]
                utils.log(f"📥 Seeded {len(df)} {tf} candles for {symbol}", tg=False)
            except Exception as e:
                utils.log(f"⚠️ Seed failed {symbol} {tf}: {e}", tg=True)


def start_ws():
    while True:
        try:
            ws = websocket.WebSocketApp(
                WS_URL,
                on_open=on_open,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close,
            )
            ws.run_forever(ping_interval=20, ping_timeout=10)
        except Exception as e:
            utils.log(f"🌐 WS crashed, reconnecting: {e}", tg=True)
        time.sleep(3)


# ================= AUTO GIT PUSH =================

def auto_git_push():
    try:
        subprocess.run(["git", "add", "."], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["git", "commit", "-m", "auto update"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["git", "push"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


# ================= MAIN =================

def run():

    state = {
        s: {
            "position": None,
            "last_candle_time": None,
            "last_exit_candle": None,
            "balance": START_BALANCE,
        } for s in SYMBOLS
    }

    utils.log("🚀 LIVE BOT STARTED", tg=True)

    _seed_history()

    # Mark the most recent SEEDED bar as "already seen" so the bot does NOT
    # trade off historical/back data on startup. Only a candle that closes
    # AFTER the bot is running counts as a new candle and can trigger entries.
    with market_lock:
        for symbol in SYMBOLS:
            seeded = list(market[symbol]["tf"][EXEC_TF]["candles"])
            if len(seeded) >= 2:
                # index[-2] is the last CLOSED bar (index[-1] may still form)
                state[symbol]["last_candle_time"] = seeded[-2]["time"]

    threading.Thread(target=start_ws, daemon=True).start()

    ws_ready.wait(timeout=15)

    last_push = 0

    while True:

        try:

            for symbol in SYMBOLS:

                with market_lock:
                    price = market[symbol]["price"]
                    exec_candles = list(market[symbol]["tf"][EXEC_TF]["candles"])

                if price is None or len(exec_candles) < MIN_BARS + 2:
                    continue

                exec_df = pd.DataFrame(exec_candles).set_index("time")

                # New candle? Use the last CLOSED bar (index -2).
                latest_candle_time = exec_df.index[-2]
                is_new_candle = (
                    state[symbol]["last_candle_time"] != latest_candle_time
                )
                if is_new_candle:
                    state[symbol]["last_candle_time"] = latest_candle_time

                process_symbol(symbol, exec_df, state[symbol], is_new_candle)

            now_ts = time.time()
            if now_ts - last_push > 300:
                auto_git_push()
                last_push = now_ts

            time.sleep(0.2)

        except Exception as e:
            utils.log(f"🚨 Runtime error: {e}\n{traceback.format_exc()}", tg=True)
            time.sleep(2)


# ================= START =================

if __name__ == "__main__":
    run()