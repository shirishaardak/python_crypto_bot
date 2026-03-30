import os
import time
import pandas as pd
import numpy as np
from datetime import datetime
import pandas_ta as ta
from dotenv import load_dotenv
import traceback

from utils import TradingUtils

load_dotenv()

# ================= CONFIG =================

BOT_NAME = "hybrid_fast_bot"

SYMBOLS = ["BTCUSD","ETHUSD"]

DEFAULT_CONTRACTS = {"BTCUSD":100,"ETHUSD":100}

TGT = {"BTCUSD":200,"ETHUSD":20}
STOPLOSS = {"BTCUSD":70,"ETHUSD":7}

CONTRACT_SIZE = {"BTCUSD":0.001,"ETHUSD":0.01}
TRAIL_STEP = {"BTCUSD":70,"ETHUSD":7}

TAKER_FEE = 0.0005

TIMEFRAME = "1m"
DAYS = 3

# ================= INIT UTILS =================

utils = TradingUtils(
    contract_size=CONTRACT_SIZE,
    taker_fee=TAKER_FEE,
    timeframe=TIMEFRAME,
    days=DAYS,
    telegram_token=os.getenv("testing_strategy_my_aglo_bot"),
    telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID"),
    bot_name=BOT_NAME
)

# ================= TRAILING SL =================

def update_trailing_sl(symbol, price, pos):

    step_size = TRAIL_STEP[symbol]

    if pos["side"] == "long":
        move = price - pos["entry"]
    else:
        move = pos["entry"] - price

    steps_crossed = int(move // step_size)

    if steps_crossed > pos["trail_step"]:

        diff_steps = steps_crossed - pos["trail_step"]

        if pos["side"] == "long":
            pos["stop"] += diff_steps * step_size
        else:
            pos["stop"] -= diff_steps * step_size

        pos["trail_step"] = steps_crossed

        utils.log(f"{symbol} SL Trailed → {pos['stop']}", tg=True)



# ================= INDICATORS =================
def build_indicators(df):

    ha = ta.ha(df["Open"], df["High"], df["Low"], df["Close"]).reset_index(drop=True)


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

    ha["Trendline"] = trendline

    return ha


# ================= EXIT =================

def exit_trade(symbol, price, pos, state, candle_time):

    pnl = (
        (price-pos["entry"]) if pos["side"]=="long"
        else (pos["entry"]-price)
    ) * CONTRACT_SIZE[symbol] * pos["qty"]

    net = pnl - utils.commission(price,pos["qty"],symbol)

    utils.save_trade({
        "entry_time": pos["entry_time"],
        "exit_time": datetime.now(),
        "symbol": symbol,
        "side": pos["side"],
        "entry_price": pos["entry"],
        "exit_price": price,
        "qty": pos["qty"],
        "net_pnl": net
    })

    utils.log(f"{symbol} EXIT {net}", tg=True)

    state["position"] = None
    state["last_candle"] = candle_time

# ================= STRATEGY =================

def process_symbol(symbol, df, state):

    if df.empty:
        return

    ha = build_indicators(df)

    if len(ha) < 50:
        return

    last = ha.iloc[-2]
    prev = ha.iloc[-3]

    price = utils.fetch_price(symbol)
    if price is None:
        return

    candle_time = ha.index[-2]

    if state["position"] is None and state["last_candle"] != candle_time:

        if last.HA_close > last.Trendline and last.HA_close > prev.HA_close:

            state["position"] = {
                "side":"long",
                "entry":price,
                "stop":price - STOPLOSS[symbol],
                "TGT":price + TGT[symbol],
                "qty":DEFAULT_CONTRACTS[symbol],
                "trail_step":0,
                "entry_time":datetime.now()
            }

            utils.log(f"{symbol} LONG {price}", tg=True)

        elif last.HA_close < last.Trendline and last.HA_close < prev.HA_close:

            state["position"] = {
                "side":"short",
                "entry":price,
                "stop":price + STOPLOSS[symbol],
                "TGT":price - TGT[symbol],
                "qty":DEFAULT_CONTRACTS[symbol],
                "trail_step":0,
                "entry_time":datetime.now()
            }

            utils.log(f"{symbol} SHORT {price}", tg=True)

    if state["position"]:

        pos = state["position"]

        # TRAILING
        update_trailing_sl(symbol, price, pos)

        if pos["side"] == "long":
            if price > pos["TGT"] or price < pos["stop"]:
                exit_trade(symbol, price, pos, state, candle_time)

        else:
            if price < pos["TGT"] or price > pos["stop"]:
                exit_trade(symbol, price, pos, state, candle_time)

# ================= MAIN =================

def run():

    state = {
        s:{"position":None,"last_candle":None}
        for s in SYMBOLS
    }

    utils.log("🚀 HYBRID BOT STARTED", tg=True)

    while True:
        try:
            for symbol in SYMBOLS:

                df = utils.fetch_candles(symbol)

                if len(df) < 50:
                    continue

                process_symbol(symbol, df, state[symbol])

            time.sleep(5)

        except Exception:
            utils.log(traceback.format_exc(), tg=True)
            time.sleep(5)

if __name__=="__main__":
    run()