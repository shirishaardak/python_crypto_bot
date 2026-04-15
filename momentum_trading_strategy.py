import os
import time
import pandas as pd
from datetime import datetime
import pandas_ta as ta
import traceback

from utils import TradingUtils

# ================= CONFIG =================

BOT_NAME = "momentum_trading_strategy"

SYMBOLS = ["BTCUSD", "ETHUSD"]

CONTRACT_SIZE = {
    "BTCUSD": 0.001,
    "ETHUSD": 0.01
}

TAKER_FEE = 0.0005

DAYS = 5

START_BALANCE = 10000
MIN_BALANCE = 500

# 🔥 risk per symbol
RISK_PER_TRADE = {
    "BTCUSD": 0.01,
    "ETHUSD": 0.008
}

MAX_DAILY_LOSS = 0.03
MAX_TRADES_PER_DAY = 5
COOLDOWN = 3

# ================= UTILS =================

utils_5m = TradingUtils(
    contract_size=CONTRACT_SIZE,
    taker_fee=TAKER_FEE,
    timeframe="5",
    days=DAYS,
    bot_name=BOT_NAME,
)

utils_15m = TradingUtils(
    contract_size=CONTRACT_SIZE,
    taker_fee=TAKER_FEE,
    timeframe="15",
    days=DAYS,
    bot_name=BOT_NAME
)

# ================= INDICATORS =================

def add_indicators(df):
    df["EMA50"] = ta.ema(df["Close"], length=50)
    df["EMA200"] = ta.ema(df["Close"], length=200)
    df["RSI"] = ta.rsi(df["Close"], length=14)
    df["ATR"] = ta.atr(df["High"], df["Low"], df["Close"], length=14)
    df["VOL_MA"] = df["Volume"].rolling(20).mean()
    return df

# ================= POSITION SIZE =================

def calculate_position_size(balance, risk_per_trade, stop_distance, symbol):
    risk_amount = balance * risk_per_trade
    qty = risk_amount / (stop_distance * CONTRACT_SIZE[symbol])
    return max(round(qty, 2), 1)

# ================= RISK CONTROL =================

def check_daily_limits(state):
    today = datetime.now().date()

    if state.get("day") != today:
        state["day"] = today
        state["start_balance"] = state["balance"]
        state["trades_today"] = 0

    drawdown = (state["balance"] - state["start_balance"]) / state["start_balance"]

    if drawdown <= -MAX_DAILY_LOSS:
        return True

    if state["trades_today"] >= MAX_TRADES_PER_DAY:
        return True

    return False

def in_cooldown(state, idx):
    last = state.get("last_trade_index")
    if last is None:
        return False
    return (idx - last) < COOLDOWN

# ================= FETCH =================

def fetch_multi_tf(symbol):

    df_5m = utils_5m.fetch_candles(symbol)
    df_15m = utils_15m.fetch_candles(symbol)

    if df_5m is None or df_15m is None:
        return None, None

    if df_5m.empty or df_15m.empty:
        return None, None

    return df_5m, df_15m

# ================= STRATEGY =================

def process_symbol(symbol, df_5m, df_15m, price, state, is_new_candle):

    df_5m = add_indicators(df_5m)
    df_15m = add_indicators(df_15m)

    last_5m = df_5m.iloc[-2]
    last_15m = df_15m.iloc[-2]

    idx = len(df_5m)

    if check_daily_limits(state):
        utils_5m.log(f"⛔ {symbol} trading paused (risk limits)")
        return

    if in_cooldown(state, idx):
        return

    pos = state["position"]
    now = datetime.now()

    # ================= EXIT =================
    if pos:

        hit_tp = False
        hit_sl = False

        if pos["side"] == "long":
            if price >= pos["tp"]:
                hit_tp = True
            elif price <= pos["sl"]:
                hit_sl = True
        else:
            if price <= pos["tp"]:
                hit_tp = True
            elif price >= pos["sl"]:
                hit_sl = True

        if hit_tp or hit_sl:

            pnl = (
                (price - pos["entry"]) * CONTRACT_SIZE[symbol] * pos["qty"]
                if pos["side"] == "long"
                else (pos["entry"] - price) * CONTRACT_SIZE[symbol] * pos["qty"]
            )

            fee = utils_5m.commission(pos["entry"], pos["qty"], symbol) + \
                  utils_5m.commission(price, pos["qty"], symbol)

            net = pnl - fee

            state["balance"] += net
            state["position"] = None
            state["last_trade_index"] = idx
            state["trades_today"] += 1

            utils_5m.log(f"{symbol} {'🟢 TP' if hit_tp else '🔴 SL'} | PNL: {round(net,2)} | Bal: {round(state['balance'],2)}")
            return

    # ================= ENTRY =================
    if not pos and is_new_candle:

        balance = state["balance"]

        if balance < MIN_BALANCE:
            return

        atr = last_5m["ATR"]

        rsi_long = 55 if symbol == "ETHUSD" else 50
        rsi_short = 45 if symbol == "ETHUSD" else 50

        # 🔼 LONG
        if (
            last_15m["EMA50"] > last_15m["EMA200"] and
            last_5m["Close"] > last_5m["EMA50"] and
            last_5m["RSI"] > rsi_long and
            last_5m["Volume"] > last_5m["VOL_MA"]
        ):

            sl = price - atr
            tp = price + (atr * 1.5)

            qty = calculate_position_size(balance, RISK_PER_TRADE[symbol], atr, symbol)

            state["position"] = {
                "side": "long",
                "entry": price,
                "qty": qty,
                "sl": sl,
                "tp": tp,
                "entry_time": now
            }

            utils_5m.log(f"🟢 LONG {symbol} @ {price}")

        # 🔽 SHORT
        elif (
            last_15m["EMA50"] < last_15m["EMA200"] and
            last_5m["Close"] < last_5m["EMA50"] and
            last_5m["RSI"] < rsi_short and
            last_5m["Volume"] > last_5m["VOL_MA"]
        ):

            sl = price + atr
            tp = price - (atr * 1.5)

            qty = calculate_position_size(balance, RISK_PER_TRADE[symbol], atr, symbol)

            state["position"] = {
                "side": "short",
                "entry": price,
                "qty": qty,
                "sl": sl,
                "tp": tp,
                "entry_time": now
            }

            utils_5m.log(f"🔴 SHORT {symbol} @ {price}")

# ================= MAIN =================

def run():

    state = {
        s: {
            "position": None,
            "last_candle_time": None,
            "last_trade_index": None,
            "balance": START_BALANCE,
            "day": None,
            "start_balance": START_BALANCE,
            "trades_today": 0
        } for s in SYMBOLS
    }

    print("🚀 MULTI-ASSET PRO BOT STARTED (PAPER MODE)")

    while True:

        try:
            for symbol in SYMBOLS:

                df_5m, df_15m = fetch_multi_tf(symbol)

                if df_5m is None or len(df_5m) < 200:
                    continue

                latest_time = df_5m.index[-2]

                is_new_candle = state[symbol]["last_candle_time"] != latest_time

                if is_new_candle:
                    state[symbol]["last_candle_time"] = latest_time

                price = utils_5m.fetch_price(symbol)

                if price is None:
                    continue

                process_symbol(symbol, df_5m, df_15m, price, state[symbol], is_new_candle)

            time.sleep(2)

        except Exception as e:
            print(f"🚨 ERROR: {e}")
            traceback.print_exc()
            time.sleep(5)

# ================= START =================

if __name__ == "__main__":
    run()