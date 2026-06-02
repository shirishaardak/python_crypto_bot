import os
import time
import json
import threading
import pandas as pd
import numpy as np

from datetime import datetime
from collections import deque

import websocket  # pip install websocket-client

import pandas_ta as ta

from dotenv import load_dotenv

import traceback
import subprocess

from utils import TradingUtils
from order_manager import OrderManager

load_dotenv()

# ================= CONFIG =================

BOT_NAME = "price_trend_following_strategy"

SYMBOLS = ["BTCUSD"]

DEFAULT_CONTRACTS = {
    "BTCUSD": 1000
}

live_DEFAULT_CONTRACTS = {
    "BTCUSD": 1
}

CONTRACT_SIZE = {
    "BTCUSD": 0.001
}

STOPLOSS = {
    "BTCUSD": 500
}

TP = {
    "BTCUSD": 300
}

TAKER_FEE = 0.0005

TIMEFRAME = "5m"

DAYS = 15

MIN_BALANCE = 1000

# ================= TARGET / LOSS =================

DAILY_TARGET = 500

MAX_DAILY_LOSS = None

# ================= WEBSOCKET CONFIG =================

# Production Delta India socket. For testnet use:
#   wss://socket-ind.testnet.deltaex.org
WS_URL = os.getenv("DELTA_WS_URL", "wss://socket.india.delta.exchange")

# Delta candlestick channel uses resolution strings like "1m", "5m", "1h"
WS_RESOLUTION = "5m"

# How many candles to keep in memory (must cover your strategy lookback)
MAX_CANDLES = 5000

# ================= INIT UTILS =================

utils = TradingUtils(
    contract_size=CONTRACT_SIZE,
    taker_fee=TAKER_FEE,
    timeframe=TIMEFRAME,
    days=DAYS,
    telegram_token=os.getenv("price_trend_following_strategy_live_bot"),  # FIXED: was reading bot name
    telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID"),
    bot_name=BOT_NAME
)

order_manager = OrderManager()

BASE_DIR = os.getcwd()

SAVE_DIR = os.path.join(
    BASE_DIR,
    "data",
    "price_trend_following_strategy"
)

os.makedirs(SAVE_DIR, exist_ok=True)

# ================= LIVE MARKET STATE (shared across threads) =================

# Updated by the WS thread, read by the strategy thread. Guarded by a lock.
market = {
    s: {
        "price": None,
        "candles": deque(maxlen=MAX_CANDLES),   # list of dicts
        "candle_index": {},                     # candle_start_time -> deque pos
        "last_seen_candle_start": None,
    } for s in SYMBOLS
}

market_lock = threading.Lock()
ws_ready = threading.Event()


# ================= SAVE DATA =================

def save_processed_data(data, symbol):

    path = os.path.join(
        SAVE_DIR,
        f"{symbol}_processed.csv"
    )

    out = pd.DataFrame({
        "time": data.index,
        "HA_open": data["HA_open"],
        "HA_high": data["HA_high"],
        "HA_low": data["HA_low"],
        "HA_close": data["HA_close"],
        "trendline": data["Trendline"],
    })

    out.to_csv(path, index=False)


# ================= PAPER ORDER =================

def place_market_order(symbol, side, qty):

    utils.log(
        f"📝 PAPER ORDER → {symbol} {side.upper()} {qty}",
        tg=True
    )

    return {"success": True}


# ================= DAILY RESET =================

def reset_daily_state(state):

    today = datetime.now().date()

    if state["last_reset_day"] != today:

        state["daily_pnl"] = 0

        state["trading_enabled"] = True

        state["last_reset_day"] = today

        utils.log(
            "🌞 New trading day started",
            tg=True
        )


def calculate_trendline(df):

    # ================= HEIKIN ASHI =================

    ha = ta.ha(
        df["Open"],
        df["High"],
        df["Low"],
        df["Close"]
    ).reset_index(drop=True)

    # ================= FRACTALS =================

    ha["high_fractal"] = np.nan
    ha["low_fractal"] = np.nan

    # NON-REPAINTING FRACTALS

    for i in range(2, len(ha) - 2):

        is_high = (
            ha.loc[i, "HA_high"] > ha.loc[i - 1, "HA_high"]
            and ha.loc[i, "HA_high"] > ha.loc[i - 2, "HA_high"]
            and ha.loc[i, "HA_high"] > ha.loc[i + 1, "HA_high"]
            and ha.loc[i, "HA_high"] > ha.loc[i + 2, "HA_high"]
        )

        is_low = (
            ha.loc[i, "HA_low"] < ha.loc[i - 1, "HA_low"]
            and ha.loc[i, "HA_low"] < ha.loc[i - 2, "HA_low"]
            and ha.loc[i, "HA_low"] < ha.loc[i + 1, "HA_low"]
            and ha.loc[i, "HA_low"] < ha.loc[i + 2, "HA_low"]
        )

        # CONFIRM AFTER 2 CANDLES

        if is_high:
            ha.loc[i + 2, "high_fractal"] = ha.loc[i, "HA_high"]

        if is_low:
            ha.loc[i + 2, "low_fractal"] = ha.loc[i, "HA_low"]

    # ================= TRENDLINE =================

    ha["Trendline"] = np.nan

    last_high_fractal = np.nan
    last_low_fractal = np.nan

    trendline = ha.loc[0, "HA_close"]

    for i in range(1, len(ha)):

        # UPDATE FRACTALS

        if not np.isnan(ha.loc[i, "high_fractal"]):
            last_high_fractal = ha.loc[i, "high_fractal"]

        if not np.isnan(ha.loc[i, "low_fractal"]):
            last_low_fractal = ha.loc[i, "low_fractal"]

        current_close = ha.loc[i, "HA_close"]

        prev_close = ha.loc[i - 1, "HA_close"]

        # ================= BULLISH BREAK =================

        if (
            not np.isnan(last_high_fractal)
            and prev_close <= last_high_fractal
            and current_close > last_high_fractal
            and current_close > trendline
            and not np.isnan(last_low_fractal)
        ):

            trendline = last_low_fractal

        # ================= BEARISH BREAK =================

        elif (
            not np.isnan(last_low_fractal)
            and prev_close >= last_low_fractal
            and current_close < last_low_fractal
            and current_close < trendline
            and not np.isnan(last_high_fractal)
        ):

            trendline = last_high_fractal

        ha.loc[i, "Trendline"] = trendline
        ha.loc[i, "up_Trendline"] = trendline + 50
        ha.loc[i, "down_Trendline"] = trendline - 50

    return ha


# ================= STRATEGY =================
# Core entry/exit logic is unchanged. The ONLY additions are:
#   - capturing the order_manager.place_order(...) return value
#   - skipping the position update if the order did not succeed
#   - using the real average fill price when available
# These are the minimum changes needed so a failed/rejected order does not
# create a phantom position in state.

def process_symbol(symbol, df, price, state, is_new_candle):

    reset_daily_state(state)

    ha = calculate_trendline(df)

    save_processed_data(ha, symbol)

    last = ha.iloc[-2]

    prev = ha.iloc[-3]

    pos = state["position"]

    now = datetime.now()

    # ================= EXIT =================

    if pos:

        exit_trade = False

        pnl = 0

        # ================= LIVE PNL =================

        if pos["side"] == "long":

            live_pnl = (
                (price - pos["entry"])
                * CONTRACT_SIZE[symbol]
                * pos["qty"]
            )

        else:

            live_pnl = (
                (pos["entry"] - price)
                * CONTRACT_SIZE[symbol]
                * pos["qty"]
            )

        # ================= DAILY TARGET FORCE EXIT =================

        if (
            DAILY_TARGET is not None
            and state["daily_pnl"] + live_pnl >= DAILY_TARGET
        ):

            pnl = live_pnl

            exit_trade = True

            utils.log(
                "🎯 DAILY TARGET REACHED DURING LIVE TRADE",
                tg=True
            )

        # ================= LONG EXIT =================

        elif (
            pos["side"] == "long"
            and (
                price < last.down_Trendline
                or price >= pos["entry"] + TP[symbol]
            )
        ):

            pnl = live_pnl

            exit_trade = True

        # ================= SHORT EXIT =================

        elif (
            pos["side"] == "short"
            and (
                price > last.up_Trendline
                or price <= pos["entry"] - TP[symbol]
            )
        ):

            pnl = live_pnl

            exit_trade = True

        # ================= FINAL EXIT =================

        if exit_trade:

            # ================= LIVE EXIT ORDER =================

            exit_side = "sell" if pos["side"] == "long" else "buy"

            exit_res = order_manager.place_order(
                size=live_DEFAULT_CONTRACTS[symbol],
                side=exit_side,
                symbol=symbol,
                reduce_only=True
            )

            # If the exit order didn't go through, keep the position open and
            # retry on the next tick rather than booking a fake closed trade.
            if not exit_res.get("success"):
                utils.log(
                    f"🚨 EXIT ORDER FAILED {symbol} "
                    f"({exit_res.get('error')}) — holding position",
                    tg=True
                )
                return

            # Use the real fill price for PnL when the exchange reports it.
            fill_price = exit_res.get("avg_price") or price

            # Recompute realised pnl on the actual fill price.
            if pos["side"] == "long":
                pnl = (
                    (fill_price - pos["entry"])
                    * CONTRACT_SIZE[symbol]
                    * pos["qty"]
                )
            else:
                pnl = (
                    (pos["entry"] - fill_price)
                    * CONTRACT_SIZE[symbol]
                    * pos["qty"]
                )

            entry_fee = utils.commission(
                pos["entry"],
                pos["qty"],
                symbol
            )

            exit_fee = utils.commission(
                fill_price,
                pos["qty"],
                symbol
            )

            total_fee = entry_fee + exit_fee

            net = pnl - total_fee

            # UPDATE BALANCE

            state["balance"] += net

            # UPDATE DAILY PNL

            state["daily_pnl"] += net

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

            utils.log(
                f"{emoji} {symbol} EXIT @ {fill_price} | "
                f"PNL: {round(net, 6)}",
                tg=True
            )

            utils.log(
                f"💰 Balance: {round(state['balance'], 2)}",
                tg=True
            )

            utils.log(
                f"📊 Daily PNL: {round(state['daily_pnl'], 2)}",
                tg=True
            )

            # ================= DAILY TARGET =================

            if (
                DAILY_TARGET is not None
                and state["daily_pnl"] >= DAILY_TARGET
            ):

                state["trading_enabled"] = False

                utils.log(
                    f"🎯 DAILY TARGET HIT: "
                    f"{round(state['daily_pnl'], 2)}",
                    tg=True
                )

            # ================= DAILY LOSS =================

            if (
                MAX_DAILY_LOSS is not None
                and state["daily_pnl"] <= MAX_DAILY_LOSS
            ):

                state["trading_enabled"] = False

                utils.log(
                    "🛑 MAX DAILY LOSS HIT",
                    tg=True
                )

            state["position"] = None

            state["last_exit_candle"] = df.index[-1]

            return

    # ================= ENTRY =================

    if not pos and is_new_candle:

        if not state["trading_enabled"]:
            return

        # ================= AVOID SAME CANDLE REENTRY =================

        if state.get("last_exit_candle") == df.index[-1]:
            return

        balance = state["balance"]

        if balance < MIN_BALANCE:

            utils.log(
                f"⚠️ Balance low: {balance}",
                tg=True
            )

            return

        # ================= LONG ENTRY =================

        if (
            prev.HA_close <= prev.up_Trendline
            and last.HA_close > last.up_Trendline
        ):
            # ================= LIVE LONG ORDER =================

            entry_res = order_manager.place_order(
                size=live_DEFAULT_CONTRACTS[symbol],
                side="buy",
                symbol=symbol
            )

            # Only record the position if the order actually filled.
            if not entry_res.get("success"):
                utils.log(
                    f"🚨 LONG ENTRY FAILED {symbol} "
                    f"({entry_res.get('error')}) — no position opened",
                    tg=True
                )
                return

            entry_price = entry_res.get("avg_price") or price

            state["position"] = {
                "side": "long",
                "entry": entry_price,
                "qty": DEFAULT_CONTRACTS[symbol],
                "entry_time": now,
                "sl": last.Trendline
            }

            utils.log(
                f"🟢 {symbol} LONG @ {entry_price} | "
                f"SL: {last.Trendline}",
                tg=True
            )

        # ================= SHORT ENTRY =================

        elif (
            prev.HA_close >= prev.down_Trendline
            and last.HA_close < last.down_Trendline
        ):
            # ================= LIVE SHORT ORDER =================

            entry_res = order_manager.place_order(
                size=live_DEFAULT_CONTRACTS[symbol],
                side="sell",
                symbol=symbol
            )

            # Only record the position if the order actually filled.
            if not entry_res.get("success"):
                utils.log(
                    f"🚨 SHORT ENTRY FAILED {symbol} "
                    f"({entry_res.get('error')}) — no position opened",
                    tg=True
                )
                return

            entry_price = entry_res.get("avg_price") or price

            state["position"] = {
                "side": "short",
                "entry": entry_price,
                "qty": DEFAULT_CONTRACTS[symbol],
                "entry_time": now,
                "sl": last.Trendline
            }

            utils.log(
                f"🔴 {symbol} SHORT @ {entry_price} | "
                f"SL: {last.Trendline}",
                tg=True
            )


# ================= WEBSOCKET LAYER =================

def _upsert_candle(symbol, candle_start, o, h, l, c):
    """
    Insert or update a candle keyed by its start time.
    Delta sends repeated updates for the same (forming) candle, so we
    overwrite the existing entry instead of appending duplicates.
    """
    m = market[symbol]
    idx = m["candle_index"]

    row = {
        "time": candle_start,
        "Open": o,
        "High": h,
        "Low": l,
        "Close": c,
    }

    if candle_start in idx:
        # Update the forming candle in place
        pos = idx[candle_start]
        m["candles"][pos] = row
    else:
        m["candles"].append(row)
        # Rebuild index (deque positions shift when maxlen evicts from left)
        idx.clear()
        for p, r in enumerate(m["candles"]):
            idx[r["time"]] = p
        m["last_seen_candle_start"] = candle_start


def on_message(ws, message):
    try:
        msg = json.loads(message)
    except Exception:
        return

    msg_type = msg.get("type")

    # ----- CANDLESTICK CHANNEL -----
    # Delta candlestick payload type looks like "candlestick_5m"
    if msg_type and msg_type.startswith("candlestick"):

        symbol = msg.get("symbol")
        if symbol not in market:
            return

        try:
            # Delta candle timestamp is in microseconds (epoch)
            candle_start = int(msg["candle_start_time"])
            o = float(msg["open"])
            h = float(msg["high"])
            l = float(msg["low"])
            c = float(msg["close"])
        except (KeyError, TypeError, ValueError):
            return

        with market_lock:
            _upsert_candle(symbol, candle_start, o, h, l, c)

    # ----- PRICE (mark price) CHANNEL -----
    elif msg_type == "mark_price":
        # symbol on mark price comes prefixed as "MARK:BTCUSD"
        raw = msg.get("symbol", "")
        symbol = raw.replace("MARK:", "")
        if symbol in market:
            try:
                with market_lock:
                    market[symbol]["price"] = float(msg["price"])
            except (KeyError, TypeError, ValueError):
                pass

    # ----- TICKER CHANNEL (fallback for last traded / mark price) -----
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
    sub = {
        "type": "subscribe",
        "payload": {
            "channels": [
                {
                    "name": f"candlestick_{WS_RESOLUTION}",
                    "symbols": SYMBOLS
                },
                {
                    "name": "mark_price",
                    "symbols": [f"MARK:{s}" for s in SYMBOLS]
                },
                {
                    "name": "v2/ticker",
                    "symbols": SYMBOLS
                }
            ]
        }
    }
    ws.send(json.dumps(sub))
    utils.log("🌐 WS subscribed", tg=True)
    ws_ready.set()


def _seed_history():
    """
    WebSocket only streams from connection time forward. Your strategy needs
    100+ historical candles before it can compute the trendline, so seed the
    deque once from the REST candle fetch you already have in utils.
    """
    for symbol in SYMBOLS:
        try:
            df = utils.fetch_candles(symbol)
            if df is None or len(df) < 100:
                continue
            with market_lock:
                m = market[symbol]
                m["candles"].clear()
                m["candle_index"].clear()
                for ts, row in df.iterrows():
                    # ts -> int microseconds key; adapt if your index differs
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
            utils.log(f"📥 Seeded {len(df)} candles for {symbol}", tg=False)
        except Exception as e:
            utils.log(f"⚠️ Seed failed {symbol}: {e}", tg=True)


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
            # ping keeps the socket alive; reconnect loop handles drops
            ws.run_forever(ping_interval=20, ping_timeout=10)
        except Exception as e:
            utils.log(f"🌐 WS crashed, reconnecting: {e}", tg=True)
        time.sleep(3)  # backoff before reconnect


# ================= AUTO GIT PUSH =================

def auto_git_push():

    try:

        subprocess.run(
            ["git", "add", "."],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )

        subprocess.run(
            ["git", "commit", "-m", "auto update"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )

        subprocess.run(
            ["git", "push"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )

    except:
        pass


# ================= MAIN =================

def run():

    state = {
        s: {
            "position": None,
            "last_candle_time": None,
            "last_exit_candle": None,
            "balance": 10000,
            "daily_pnl": 0,
            "last_reset_day": datetime.now().date(),
            "trading_enabled": True
        } for s in SYMBOLS
    }

    utils.log(
        "🚀 LIVE BOT STARTED",
        tg=True
    )

    # Seed history first (REST), then start the WS stream
    _seed_history()

    threading.Thread(target=start_ws, daemon=True).start()

    # Wait for the socket to subscribe before entering the loop
    ws_ready.wait(timeout=15)

    # Git push throttle (don't push every tick)
    last_push = 0

    while True:

        try:

            for symbol in SYMBOLS:

                # Snapshot shared state under the lock, then release fast
                with market_lock:
                    price = market[symbol]["price"]
                    candles = list(market[symbol]["candles"])

                if price is None or len(candles) < 100:
                    continue

                df = pd.DataFrame(candles).set_index("time")

                latest_candle_time = df.index[-2]

                is_new_candle = (
                    state[symbol]["last_candle_time"]
                    != latest_candle_time
                )

                if is_new_candle:

                    state[symbol][
                        "last_candle_time"
                    ] = latest_candle_time

                process_symbol(
                    symbol,
                    df,
                    price,
                    state[symbol],
                    is_new_candle
                )

            # Push at most once every 5 minutes, not every tick
            now_ts = time.time()
            if now_ts - last_push > 300:
                auto_git_push()
                last_push = now_ts

            # Small sleep: fast enough for tick-level exits, low CPU
            time.sleep(0.2)

        except Exception as e:

            utils.log(
                f"🚨 Runtime error: {e}\n"
                f"{traceback.format_exc()}",
                tg=True
            )

            time.sleep(2)


# ================= START =================

if __name__ == "__main__":

    run()