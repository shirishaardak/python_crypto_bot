import os
import time
import json
import threading
import pandas as pd
import numpy as np

from datetime import datetime
from collections import deque

import websocket  # pip install websocket-client

from dotenv import load_dotenv

import traceback

from utils import TradingUtils

load_dotenv()

# ================= CONFIG =================

BOT_NAME = "trend_following_strategy"

SYMBOLS = ["BTCUSD", "ETHUSD", "SOLUSD"]

# --- accounting ---
START_BALANCE = 10000.0
TAKER_FEE = 0.0005
MIN_BALANCE = 1000
CONTRACT_SIZE = {"BTCUSD": 0.001, "ETHUSD": 0.01, "SOLUSD": 1.0}
DEFAULT_CONTRACTS = {
    "BTCUSD": 1,
    "ETHUSD": 1,
    "SOLUSD": 1,
}

# ----- SINGLE TIMEFRAME: 1 day -----
CORE_TIMEFRAME = "1d"
EXEC_TF = CORE_TIMEFRAME
ALL_TFS = [EXEC_TF]
TIMEFRAME = EXEC_TF

DAYS = 365

# Need at least 2 closed HA candles to compare current vs previous.
MIN_BARS = 5


# ================= HEIKIN-ASHI =================

def heikin_ashi(df):
    """
    Convert OHLC dataframe (cols: Open, High, Low, Close) to Heikin-Ashi.
    Returns a dataframe with HA_Open, HA_High, HA_Low, HA_Close.
    """
    o = df["Open"].to_numpy(float)
    h = df["High"].to_numpy(float)
    l = df["Low"].to_numpy(float)
    c = df["Close"].to_numpy(float)
    n = len(df)

    ha_close = (o + h + l + c) / 4.0
    ha_open = np.empty(n, float)
    ha_open[0] = (o[0] + c[0]) / 2.0
    for i in range(1, n):
        ha_open[i] = (ha_open[i - 1] + ha_close[i - 1]) / 2.0

    ha_high = np.maximum.reduce([h, ha_open, ha_close])
    ha_low = np.minimum.reduce([l, ha_open, ha_close])

    return pd.DataFrame(
        {
            "HA_Open": ha_open,
            "HA_High": ha_high,
            "HA_Low": ha_low,
            "HA_Close": ha_close,
        },
        index=df.index,
    )


def core_commission(price, qty, symbol):
    return abs(price) * qty * CONTRACT_SIZE[symbol] * TAKER_FEE


# ================= WEBSOCKET CONFIG =================

WS_URL = os.getenv("DELTA_WS_URL", "wss://socket.india.delta.exchange")
WS_RESOLUTIONS = ALL_TFS
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
    "trend_following_strategy"
)

os.makedirs(SAVE_DIR, exist_ok=True)

# ================= LIVE MARKET STATE (shared across threads) =================

def _new_tf_buffer():
    return {
        "candles": deque(maxlen=MAX_CANDLES),
        "candle_index": {},
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

def save_processed_data(df, symbol):
    """Persist the latest computed HA view."""
    path = os.path.join(SAVE_DIR, f"{symbol}_processed.csv")
    df.to_csv(path, index=True)


# ================= PAPER ORDER =================

def place_market_order(symbol, side, qty):

    utils.log(
        f"📝 PAPER ORDER → {symbol} {side.upper()} {qty}",
        tg=True
    )

    return {"success": True}


# ================= TRADE BOOKKEEPING =================

def _close_position(symbol, pos, fill_price, state, now):
    """Close an open long or short, book PnL, log, and clear the position."""

    close_side = "sell" if pos["side"] == "long" else "buy"
    place_market_order(symbol, close_side, pos["qty"])

    # Long profits when price rises; short profits when price falls.
    if pos["side"] == "long":
        gross = (fill_price - pos["entry"]) * CONTRACT_SIZE[symbol] * pos["qty"]
    else:  # short
        gross = (pos["entry"] - fill_price) * CONTRACT_SIZE[symbol] * pos["qty"]

    entry_fee = utils.commission(pos["entry"], pos["qty"], symbol)
    exit_fee = utils.commission(fill_price, pos["qty"], symbol)
    total_fee = entry_fee + exit_fee

    net = gross - total_fee
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
    utils.log(f"{emoji} {symbol} EXIT {pos['side'].upper()} @ {fill_price} | PNL: {round(net, 6)}", tg=True)
    utils.log(f"💰 Balance: {round(state['balance'], 2)}", tg=True)

    state["position"] = None
    return net


def _open_position(symbol, side, fill_price, state, now, candle_id):
    """Open a long or short position."""

    order_side = "buy" if side == "long" else "sell"
    place_market_order(symbol, order_side, DEFAULT_CONTRACTS[symbol])

    state["position"] = {
        "side": side,
        "entry": fill_price,
        "qty": DEFAULT_CONTRACTS[symbol],
        "entry_time": now,
        "entry_candle": candle_id,
    }

    arrow = "🟢" if side == "long" else "🔻"
    utils.log(
        f"{arrow} {symbol} {side.upper()} (HA) @ {round(fill_price, 2)}",
        tg=True
    )


# ================= STRATEGY =================
# 1d Heikin-Ashi, LONG + SHORT, paper-trading.
# Current = today's forming bar, Previous = yesterday's closed bar.
#
#   LONG  entry : today HA close > yesterday HA close
#   LONG  exit  : today HA close < yesterday HA open
#   SHORT entry : today HA close < yesterday HA close
#   SHORT exit  : today HA close > yesterday HA open
#
# Flip behavior: if an opposite ENTRY signal fires while in a position,
# close current and immediately open the opposite side (same loop).

def process_symbol(symbol, exec_df, state):

    df = exec_df

    # Need history + a forming bar.
    if len(df) < MIN_BARS + 2:
        return

    # Build Heikin-Ashi from raw OHLC, INCLUDING today's forming bar.
    ha = heikin_ashi(df)
    if len(ha) < 2:
        return

    # save_processed_data(ha, symbol)

    cur = ha.iloc[-1]    # today (forming) HA candle
    prev = ha.iloc[-2]   # yesterday (closed) HA candle

    ha_close_cur = float(cur.HA_Close)
    ha_close_prev = float(prev.HA_Close)
    ha_open_prev = float(prev.HA_Open)

    # Fills assumed at today's (forming) candle close.
    fill_price = float(df.iloc[-1].Close)

    candle_id = df.index[-1]
    now = datetime.now()

    pos = state["position"]

    long_entry_sig = ha_close_cur > ha_close_prev
    short_entry_sig = ha_close_cur < ha_close_prev
    long_exit_sig = ha_close_cur < ha_open_prev
    short_exit_sig = ha_close_cur > ha_open_prev

    # ================= MANAGE OPEN POSITION =================
    if pos:

        # Don't act on the same candle we entered on.
        if pos.get("entry_candle") == candle_id:
            return

        if pos["side"] == "long":
            # Flip to short if a short entry signal fires.
            if short_entry_sig:
                _close_position(symbol, pos, fill_price, state, now)
                state["last_exit_candle"] = candle_id
                if short_exit_sig:
                    return  # would immediately exit a fresh short; stay flat
                _open_position(symbol, "short", fill_price, state, now, candle_id)
                return
            # Otherwise plain long exit.
            if long_exit_sig:
                _close_position(symbol, pos, fill_price, state, now)
                state["last_exit_candle"] = candle_id
            return

        else:  # short position
            # Flip to long if a long entry signal fires.
            if long_entry_sig:
                _close_position(symbol, pos, fill_price, state, now)
                state["last_exit_candle"] = candle_id
                if long_exit_sig:
                    return  # would immediately exit a fresh long; stay flat
                _open_position(symbol, "long", fill_price, state, now, candle_id)
                return
            # Otherwise plain short exit.
            if short_exit_sig:
                _close_position(symbol, pos, fill_price, state, now)
                state["last_exit_candle"] = candle_id
            return

    # ================= FLAT -> LOOK FOR ENTRY =================
    if not pos:

        # Avoid same-candle reentry after an exit.
        if state.get("last_exit_candle") == candle_id:
            return

        balance = state["balance"]
        if balance < MIN_BALANCE:
            utils.log(f"⚠️ Balance low: {balance}", tg=True)
            return

        if long_entry_sig:
            _open_position(symbol, "long", fill_price, state, now, candle_id)
        elif short_entry_sig:
            _open_position(symbol, "short", fill_price, state, now, candle_id)
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
    """Map a Delta payload type like 'candlestick_1d' to '1d'."""
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


def on_close(ws, close_status_code, close_msg):
    utils.log(f"🌐 WS closed: {close_status_code} {close_msg}", tg=True)


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


# ================= MAIN =================

def run():

    state = {
        s: {
            "position": None,
            "last_exit_candle": None,
            "balance": START_BALANCE,
        } for s in SYMBOLS
    }

    utils.log("🚀 LIVE BOT STARTED (1d Heikin-Ashi, LONG+SHORT)", tg=True)

    _seed_history()

    threading.Thread(target=start_ws, daemon=True).start()

    ws_ready.wait(timeout=15)

    while True:

        try:

            for symbol in SYMBOLS:

                with market_lock:
                    price = market[symbol]["price"]
                    exec_candles = list(market[symbol]["tf"][EXEC_TF]["candles"])

                if price is None or len(exec_candles) < MIN_BARS + 2:
                    continue

                exec_df = pd.DataFrame(exec_candles).set_index("time")

                process_symbol(symbol, exec_df, state[symbol])

            time.sleep(0.2)

        except Exception as e:
            utils.log(f"🚨 Runtime error: {e}\n{traceback.format_exc()}", tg=True)
            time.sleep(2)


# ================= START =================

if __name__ == "__main__":
    run()