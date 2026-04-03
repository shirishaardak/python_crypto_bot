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
STOPLOSS = {"BTCUSD":100,"ETHUSD":5}

CONTRACT_SIZE = {"BTCUSD":0.001,"ETHUSD":0.01}
TRAIL_STEP = {"BTCUSD":50,"ETHUSD":5}

TAKER_FEE = 0.0005

TIMEFRAME_1M = "1m"
TIMEFRAME_5M = "15m"
DAYS = 3

# ================= INIT UTILS =================

utils = TradingUtils(
    contract_size=CONTRACT_SIZE,
    taker_fee=TAKER_FEE,
    timeframe=TIMEFRAME_1M,
    days=DAYS,
    telegram_token=os.getenv("testing_strategy_my_aglo_bot"),
    telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID"),
    bot_name=BOT_NAME
)

# ================= TRAILING SL =================

def update_trailing_sl(symbol, current_price, pos):
    """Trail stop loss based on actual price movement"""
    
    step_size = TRAIL_STEP[symbol]

    if pos["side"] == "long":
        move = current_price - pos["entry"]
        new_stop = pos["entry"] + (int(move // step_size) * step_size)
        if new_stop > pos["stop"]:
            pos["stop"] = new_stop
            utils.log(f"{symbol} LONG SL Trailed → {pos['stop']}", tg=True)
    else:
        move = pos["entry"] - current_price
        new_stop = pos["entry"] - (int(move // step_size) * step_size)
        if new_stop < pos["stop"]:
            pos["stop"] = new_stop
            utils.log(f"{symbol} SHORT SL Trailed → {pos['stop']}", tg=True)



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


# ================= MULTI-TIMEFRAME DATA FETCH =================

def fetch_multi_timeframe_data(symbol):
    """Fetch and calculate indicators for both 1m and 5m timeframes"""
    
    ha_1m = None
    ha_5m = None
    
    try:
        # Fetch 1m data (already set in utils)
        df_1m = utils.fetch_candles(symbol)
        if df_1m is not None and len(df_1m) >= 50:
            ha_1m = build_indicators(df_1m)
        
        # Fetch 5m data - temporarily override
        original_tf = utils.timeframe
        utils.timeframe = TIMEFRAME_5M
        utils.TIMEFRAME = TIMEFRAME_5M
        
        df_5m = utils.fetch_candles(symbol)
        if df_5m is not None and len(df_5m) >= 50:
            ha_5m = build_indicators(df_5m)
        
        # Restore original timeframe immediately
        utils.timeframe = original_tf
        utils.TIMEFRAME = original_tf
        
    except Exception as e:
        utils.log(f"Error fetching multi-timeframe data for {symbol}: {e}")
        # Always restore timeframe even on error
        utils.timeframe = TIMEFRAME_1M
        utils.TIMEFRAME = TIMEFRAME_1M
        return None, None
    
    return ha_1m, ha_5m

# ================= EXIT =================

def exit_trade(symbol, price, pos, state, candle_time):

    pnl = (
        (price-pos["entry"]) if pos["side"]=="long"
        else (pos["entry"]-price)
    ) * CONTRACT_SIZE[symbol] * pos["qty"]

    net = pnl - utils.commission(price,pos["qty"],symbol)

    utils.save_trade({
        "symbol": symbol,
        "side": pos["side"],
        "entry_time": pos["entry_time"],
        "exit_time": datetime.now(),
        "entry_price": pos["entry"],
        "exit_price": price,
        "qty": pos["qty"],
        "net_pnl": round(net, 6)
    })

    utils.log(f"{symbol} EXIT {net}", tg=True)

    state["position"] = None
    state["last_candle"] = candle_time

# ================= STRATEGY =================

def process_symbol(symbol, ha_1m, ha_5m, state):

    if ha_1m is None or ha_5m is None:
        return

    last_1m = ha_1m.iloc[-2]
    prev_1m = ha_1m.iloc[-3]
    last_5m = ha_5m.iloc[-2]

    price = utils.fetch_price(symbol)
    if price is None:
        return

    candle_time = ha_1m.index[-2]

    # ENTRY - Requires alignment between 1m and 5m trendlines
    if state["position"] is None and state["last_candle"] != candle_time:

        # LONG: 1m crossover + 5m confirmation
        if (last_1m.HA_close > last_1m.Trendline and 
            prev_1m.HA_close < prev_1m.Trendline and
            last_5m.HA_close > last_5m.Trendline):

            state["position"] = {
                "side":"long",
                "entry":price,
                "stop":last_1m.Trendline,
                "TGT":price + TGT[symbol],
                "qty":DEFAULT_CONTRACTS[symbol],
                "trail_step":0,
                "entry_time":datetime.now(),
                "entry_candle":candle_time
            }
            state["last_candle"] = candle_time

            utils.log(f"{symbol} LONG {price} (1m & 5m confirmed)", tg=True)

        # SHORT: 1m crossover + 5m confirmation
        elif (last_1m.HA_close < last_1m.Trendline and 
              prev_1m.HA_close > prev_1m.Trendline and
              last_5m.HA_close < last_5m.Trendline):

            state["position"] = {
                "side":"short",
                "entry":price,
                "stop":last_1m.Trendline,
                "TGT":price - TGT[symbol],
                "qty":DEFAULT_CONTRACTS[symbol],
                "trail_step":0,
                "entry_time":datetime.now(),
                "entry_candle":candle_time
            }
            state["last_candle"] = candle_time

            utils.log(f"{symbol} SHORT {price} (1m & 5m confirmed)", tg=True)

    # EXIT - Check against actual price (fastest response)
    if state["position"]:

        pos = state["position"]

        # Trail stop loss on current price
        update_trailing_sl(symbol, price, pos)

        # Exit on SL breach (but not on entry candle)
        if candle_time != pos["entry_candle"]:
            if pos["side"] == "long" and last_1m.HA_close < last_1m.Trendline:
                exit_trade(symbol, price, pos, state, candle_time)
            elif pos["side"] == "short" and last_1m.HA_close > last_1m.Trendline:
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

                ha_1m, ha_5m = fetch_multi_timeframe_data(symbol)

                if ha_1m is None or ha_5m is None:
                    continue

                process_symbol(symbol, ha_1m, ha_5m, state[symbol])

            time.sleep(5)

        except Exception:
            utils.log(traceback.format_exc(), tg=True)
            time.sleep(5)

if __name__=="__main__":
    run()