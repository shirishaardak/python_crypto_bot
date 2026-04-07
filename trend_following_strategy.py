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

BOT_NAME = "trend_following_strategy"

SYMBOLS = ["BTCUSD", "ETHUSD"]

DEFAULT_CONTRACTS = {"BTCUSD": 100, "ETHUSD": 100}
CONTRACT_SIZE = {"BTCUSD": 0.001, "ETHUSD": 0.01}

TAKER_FEE = 0.0005

TIMEFRAME_15M = "15m"
DAYS = 5

TAKE_PROFIT = {"BTCUSD": 300, "ETHUSD": 30}

# 🔥 TSL CONFIG
TSL_CONFIG = {
    "BTCUSD": {"step": 200, "sl": 400},
    "ETHUSD": {"step": 10,  "sl": 20}
}

ATR_LENGTH = 14
ATR_MA_LENGTH = 20

# ================= INIT UTILS =================

utils = TradingUtils(
    contract_size=CONTRACT_SIZE,
    taker_fee=TAKER_FEE,
    timeframe=TIMEFRAME_15M,
    days=DAYS,
    telegram_token=os.getenv("trend_following_strategy_bot"),
    telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID"),
    bot_name=BOT_NAME
)

# ================= INDICATORS =================

def calculate_indicators(df):

    ha = ta.ha(df["Open"], df["High"], df["Low"], df["Close"]).reset_index(drop=True)

    # ATR
    atr = ta.atr(df["High"], df["Low"], df["Close"], length=ATR_LENGTH)
    ha["ATR"] = atr
    ha["ATR_MA"] = ha["ATR"].rolling(ATR_MA_LENGTH).mean()

    # Trendline
    ha["UPPER"] = ha["HA_high"].rolling(21).max()
    ha["LOWER"] = ha["HA_low"].rolling(21).min()

    trendline = np.zeros(len(ha))
    trend = ha["HA_close"].iloc[0]
    trendline[0] = trend

    for i in range(1, len(ha)):

        if ha["HA_high"].iloc[i] == ha["UPPER"].iloc[i]:
            trend = ha["HA_low"].iloc[i]

        elif ha["HA_low"].iloc[i] == ha["LOWER"].iloc[i]:
            trend = ha["HA_high"].iloc[i]

        trendline[i] = trend

    ha["trendline"] = trendline

    return ha

# ================= STRATEGY =================

def process_symbol(symbol, ha, price, state):

    last = ha.iloc[-2]
    prev = ha.iloc[-3]

    pos = state["position"]

    atr_ok = last.ATR > last.ATR_MA

    # ================= ENTRY =================
    if pos is None:

        # LONG
        if (
            last.HA_close > last.trendline and
            last.HA_close > prev.HA_close and
            last.HA_close > prev.HA_open and
            atr_ok
        ):

            state["position"] = {
                "side": "long",
                "entry": price,
                "stop": price - TSL_CONFIG[symbol]["sl"],
                "tp": price + TAKE_PROFIT[symbol],
                "qty": DEFAULT_CONTRACTS[symbol],
                "entry_time": datetime.now(),
                "trail_price": price
            }

            utils.log(f"🟢 {symbol} LONG ENTRY @ {price}", tg=True)
            return

        # SHORT
        if (
            last.HA_close < last.trendline and
            last.HA_close < prev.HA_close and
            last.HA_close < prev.HA_open and
            atr_ok
        ):

            state["position"] = {
                "side": "short",
                "entry": price,
                "stop": price + TSL_CONFIG[symbol]["sl"],
                "tp": price - TAKE_PROFIT[symbol],
                "qty": DEFAULT_CONTRACTS[symbol],
                "entry_time": datetime.now(),
                "trail_price": price
            }

            utils.log(f"🔴 {symbol} SHORT ENTRY @ {price}", tg=True)
            return

    # ================= POSITION MANAGEMENT =================
    if pos:

        step = TSL_CONFIG[symbol]["step"]

        # ===== TSL UPDATE =====
        if pos["side"] == "long":

            if price > pos["trail_price"]:
                pos["trail_price"] = price

            move = pos["trail_price"] - pos["entry"]

            if move >= step:
                steps_moved = int(move // step)
                new_sl = pos["entry"] + (steps_moved - 1) * step

                if new_sl > pos["stop"]:
                    pos["stop"] = new_sl
                    utils.log(f"🔄 {symbol} TSL moved to {pos['stop']}", tg=True)

        elif pos["side"] == "short":

            if price < pos["trail_price"]:
                pos["trail_price"] = price

            move = pos["entry"] - pos["trail_price"]

            if move >= step:
                steps_moved = int(move // step)
                new_sl = pos["entry"] - (steps_moved - 1) * step

                if new_sl < pos["stop"]:
                    pos["stop"] = new_sl
                    utils.log(f"🔄 {symbol} TSL moved to {pos['stop']}", tg=True)

        # ===== EXIT =====
        exit_now = False
        pnl = 0

        # SL HIT
        if pos["side"] == "long" and price <= pos["stop"]:
            pnl = (price - pos["entry"]) * CONTRACT_SIZE[symbol] * pos["qty"]
            exit_now = True

        elif pos["side"] == "short" and price >= pos["stop"]:
            pnl = (pos["entry"] - price) * CONTRACT_SIZE[symbol] * pos["qty"]
            exit_now = True

        # TP HIT (optional but kept)
        elif pos["side"] == "long" and price >= pos["tp"]:
            pnl = (price - pos["entry"]) * CONTRACT_SIZE[symbol] * pos["qty"]
            exit_now = True

        elif pos["side"] == "short" and price <= pos["tp"]:
            pnl = (pos["entry"] - price) * CONTRACT_SIZE[symbol] * pos["qty"]
            exit_now = True

        if exit_now:

            fee = utils.commission(price, pos["qty"], symbol)
            net = pnl - fee

            utils.save_trade({
                "symbol": symbol,
                "side": pos["side"],
                "entry_price": pos["entry"],
                "exit_price": price,
                "qty": pos["qty"],
                "net_pnl": round(net, 6),
                "entry_time": pos["entry_time"],
                "exit_time": datetime.now()
            })

            emoji = "🟢" if net > 0 else "🔴"
            utils.log(f"{emoji} {symbol} EXIT @ {price} | PNL {round(net,6)}", tg=True)

            state["position"] = None

# ================= MAIN =================

def run():

    state = {s: {"position": None, "last_candle_time": None} for s in SYMBOLS}

    utils.log("🚀 Strategy LIVE (15m + ATR + TSL)", tg=True)

    while True:

        try:
            for symbol in SYMBOLS:

                df = utils.fetch_candles(symbol)

                if df is None or len(df) < 100:
                    continue

                ha = calculate_indicators(df)

                latest_candle_time = df.index[-1]

                if state[symbol]["last_candle_time"] == latest_candle_time:
                    continue

                state[symbol]["last_candle_time"] = latest_candle_time

                price = utils.fetch_price(symbol)

                if price is None:
                    continue

                process_symbol(symbol, ha, price, state[symbol])

            time.sleep(60)

        except Exception as e:
            utils.log(f"Runtime error: {e}", tg=True)
            time.sleep(5)

if __name__ == "__main__":
    run()