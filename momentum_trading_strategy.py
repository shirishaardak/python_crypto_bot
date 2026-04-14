import os
import time
import pandas as pd
import numpy as np
from datetime import datetime
import pandas_ta as ta
from dotenv import load_dotenv

from utils import TradingUtils

load_dotenv()

# ================= CONFIG =================

BOT_NAME = "scalp_5m_strategy"

SYMBOLS = ["BTCUSD"]

DEFAULT_CONTRACTS = {"BTCUSD": 100}
CONTRACT_SIZE = {"BTCUSD": 0.001}

STOPLOSS = {"BTCUSD": 80}
TAKER_FEE = 0.0005

TIMEFRAME = "5m"
DAYS = 3

MIN_BALANCE = 5000

# ================= INIT =================

utils = TradingUtils(
    contract_size=CONTRACT_SIZE,
    taker_fee=TAKER_FEE,
    timeframe=TIMEFRAME,
    days=DAYS,
    telegram_token=os.getenv("testing_strategy_my_aglo_bot"),
    telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID"),
    bot_name=BOT_NAME
)

# ================= PAPER ORDER =================

def place_market_order(symbol, side, qty):
    utils.log(f"📝 PAPER ORDER → {symbol} {side.upper()} {qty}", tg=True)
    return {"success": True}

# ================= STRATEGY =================

def process_symbol(symbol, df, price, state, is_new_candle):

    # ===== INDICATORS =====
    df["ema9"] = ta.ema(df["Close"], length=9)
    df["ema21"] = ta.ema(df["Close"], length=21)
    df["rsi"] = ta.rsi(df["Close"], length=7)
    df["vwap"] = ta.vwap(df["High"], df["Low"], df["Close"], df["Volume"])
    df["atr"] = ta.atr(df["High"], df["Low"], df["Close"], length=14)

    last = df.iloc[-2]
    prev = df.iloc[-3]

    pos = state["position"]
    now = datetime.now()

    # ================= EXIT =================
    if pos:

        exit_trade = False

        # ===== PROFIT CALC =====
        if pos["side"] == "long":
            profit = price - pos["entry"]
        else:
            profit = pos["entry"] - price

        # ===== SMART TRAILING SL =====
        if pos["side"] == "long":

            if profit > 50:
                pos["sl"] = max(pos["sl"], pos["entry"] + 10)

            if profit > 100:
                pos["sl"] = max(pos["sl"], pos["entry"] + 50)

            if profit > 150:
                pos["sl"] = max(pos["sl"], pos["entry"] + 100)

            if profit > 200:
                pos["sl"] = max(pos["sl"], price - 40)

        elif pos["side"] == "short":

            if profit > 50:
                pos["sl"] = min(pos["sl"], pos["entry"] - 10)

            if profit > 100:
                pos["sl"] = min(pos["sl"], pos["entry"] - 50)

            if profit > 150:
                pos["sl"] = min(pos["sl"], pos["entry"] - 100)

            if profit > 200:
                pos["sl"] = min(pos["sl"], price + 40)

        # ===== SL HIT =====
        if pos["side"] == "long" and price <= pos["sl"]:
            exit_trade = True

        elif pos["side"] == "short" and price >= pos["sl"]:
            exit_trade = True

        # ===== OPTIONAL HARD TP =====
        elif pos["side"] == "long" and price >= pos["entry"] + 300:
            exit_trade = True

        elif pos["side"] == "short" and price <= pos["entry"] - 300:
            exit_trade = True

        if exit_trade:

            if pos["side"] == "long":
                pnl = (price - pos["entry"]) * CONTRACT_SIZE[symbol] * pos["qty"]
            else:
                pnl = (pos["entry"] - price) * CONTRACT_SIZE[symbol] * pos["qty"]

            entry_fee = utils.commission(pos["entry"], pos["qty"], symbol)
            exit_fee  = utils.commission(price, pos["qty"], symbol)

            net = pnl - (entry_fee + exit_fee)

            state["balance"] += net

            utils.save_trade({
                "symbol": symbol,
                "side": pos["side"],
                "entry_price": pos["entry"],
                "exit_price": price,
                "qty": pos["qty"],
                "net_pnl": round(net, 6),
                "entry_time": pos["entry_time"],
                "exit_time": now
            })

            emoji = "🟢" if net > 0 else "🔴"
            utils.log(f"{emoji} EXIT {symbol} @ {price} | PNL: {round(net,6)}", tg=True)
            utils.log(f"💰 Balance: {round(state['balance'],2)}", tg=True)

            state["position"] = None
            state["last_exit_candle"] = df.index[-1]

            return

    # ================= ENTRY =================
    if not pos and is_new_candle:

        if state.get("last_exit_candle") == df.index[-1]:
            return

        balance = state["balance"]

        if balance < MIN_BALANCE:
            utils.log(f"⚠️ Balance low: {balance}")
            return

        # ===== LONG =====
        if (
            last["ema9"] > last["ema21"] and
            last["Close"] > last["vwap"] and
            prev["Close"] < prev["ema9"] and
            last["Close"] > last["ema9"] and
            last["rsi"] > 50
        ):

            state["position"] = {
                "side": "long",
                "entry": price,
                "qty": DEFAULT_CONTRACTS[symbol],
                "entry_time": now,
                "sl": price - STOPLOSS[symbol]
            }

            utils.log(f"⚡ LONG {symbol} @ {price}", tg=True)

        # ===== SHORT =====
        elif (
            last["ema9"] < last["ema21"] and
            last["Close"] < last["vwap"] and
            prev["Close"] > prev["ema9"] and
            last["Close"] < last["ema9"] and
            last["rsi"] < 50
        ):

            state["position"] = {
                "side": "short",
                "entry": price,
                "qty": DEFAULT_CONTRACTS[symbol],
                "entry_time": now,
                "sl": price + STOPLOSS[symbol]
            }

            utils.log(f"⚡ SHORT {symbol} @ {price}", tg=True)

# ================= MAIN =================

def run():

    state = {
        s: {
            "position": None,
            "last_candle_time": None,
            "last_exit_candle": None,
            "balance": 5000
        } for s in SYMBOLS
    }

    utils.log("🚀 SCALPING BOT STARTED (SMART TSL)", tg=True)

    while True:

        try:
            for symbol in SYMBOLS:

                df = utils.fetch_candles(symbol)

                if df is None or len(df) < 50:
                    continue

                latest_candle_time = df.index[-2]

                is_new_candle = state[symbol]["last_candle_time"] != latest_candle_time

                if is_new_candle:
                    state[symbol]["last_candle_time"] = latest_candle_time

                price = utils.fetch_price(symbol)

                if price is None:
                    continue

                process_symbol(symbol, df, price, state[symbol], is_new_candle)

            time.sleep(2)

        except Exception as e:
            utils.log(f"🚨 Runtime error: {e}", tg=True)
            time.sleep(5)

# ================= START =================

if __name__ == "__main__":
    run()