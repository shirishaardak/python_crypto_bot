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

TIMEFRAME_1H = "1h"
TIMEFRAME_15M = "15m"
DAYS = 5

TAKE_PROFIT = {"BTCUSD": 300, "ETHUSD": 30}
STOP_LOSS   = {"BTCUSD": 100, "ETHUSD": 10}

ADX_LENGTH = 9
ADX_THRESHOLD = 25

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

# ================= OPTIONAL SAVE =================

def save_processed_data(df, ha, symbol):
    path = os.path.join(utils.SAVE_DIR, f"{symbol}_processed.csv")

    out = pd.DataFrame({
        "time": df.index,
        "HA_open": ha["HA_open"],
        "HA_high": ha["HA_high"],
        "HA_low": ha["HA_low"],
        "HA_close": ha["HA_close"],
        "trendline": ha["trendline"],
    })

    out.to_csv(path, index=False)

# ================= TRENDLINE =================

def calculate_trendline(df):

    ha = ta.ha(df["Open"], df["High"], df["Low"], df["Close"]).reset_index(drop=True)

    adx = ta.adx(ha["HA_high"], ha["HA_low"], ha["HA_close"], length=ADX_LENGTH)
    ha["ADX"] = adx[f"ADX_{ADX_LENGTH}"]

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

# ================= MULTI-TIMEFRAME DATA FETCH =================

def fetch_multi_timeframe_data(symbol):
    """Fetch and calculate trendlines for both 1h and 15m timeframes"""
    
    ha_15m = None
    ha_1h = None
    
    try:
        # Fetch 15m data
        df_15m = utils.fetch_candles(symbol)
        if df_15m is not None and len(df_15m) >= 100:
            ha_15m = calculate_trendline(df_15m)
        
        # Fetch 1h data (using utils with 1h timeframe temporarily)
        # Store original timeframe
        original_tf = utils.timeframe
        utils.timeframe = TIMEFRAME_1H
        
        df_1h = utils.fetch_candles(symbol)
        if df_1h is not None and len(df_1h) >= 100:
            ha_1h = calculate_trendline(df_1h)
        
        # Restore original timeframe
        utils.timeframe = original_tf
        
    except Exception as e:
        utils.log(f"Error fetching multi-timeframe data for {symbol}: {e}")
        return None, None
    
    return ha_15m, ha_1h

# ================= STRATEGY =================

def process_symbol(symbol, ha_15m, ha_1h, price, state):

    last_15m = ha_15m.iloc[-2]
    prev_15m = ha_15m.iloc[-3]
    
    last_1h = ha_1h.iloc[-2]

    pos = state["position"]
    now = datetime.now()

    adx_ok_15m = last_15m.ADX > ADX_THRESHOLD
    adx_ok_1h = last_1h.ADX > ADX_THRESHOLD

    # ENTRY - Requires alignment between 1h and 15m trendlines
    if pos is None:

        # LONG: Both 1h and 15m showing uptrend
        if (last_15m.HA_close > last_15m.trendline and 
            last_15m.HA_close > prev_15m.HA_close and 
            last_15m.HA_close > prev_15m.HA_open and
            last_1h.HA_close > last_1h.trendline):

            state["position"] = {
                "side": "long",
                "entry": price,
                "stop": last_15m.trendline - STOP_LOSS[symbol],
                "tp": price + TAKE_PROFIT[symbol],
                "qty": DEFAULT_CONTRACTS[symbol],
                "entry_time": now
            }

            utils.log(f"🟢 {symbol} LONG ENTRY @ {price} (1h & 15m confirmed)", tg=True)
            return

        # SHORT: Both 1h and 15m showing downtrend
        if (last_15m.HA_close < last_15m.trendline and 
            last_15m.HA_close < prev_15m.HA_close and 
            last_15m.HA_close < prev_15m.HA_open and
            last_1h.HA_close < last_1h.trendline ):

            state["position"] = {
                "side": "short",
                "entry": price,
                "stop": last_15m.trendline + STOP_LOSS[symbol],
                "tp": price - TAKE_PROFIT[symbol],
                "qty": DEFAULT_CONTRACTS[symbol],
                "entry_time": now
            }

            utils.log(f"🔴 {symbol} SHORT ENTRY @ {price} (1h & 15m confirmed)", tg=True)
            return

    # EXIT - Only based on 15m trendline
    if pos:

        exit_trade = False

        if pos["side"] == "long":
            if last_15m.HA_close <= last_15m.trendline:
                pnl = (price - pos["entry"]) * CONTRACT_SIZE[symbol] * pos["qty"]
                exit_trade = True

        else:
            if last_15m.HA_close >= last_15m.trendline:
                pnl = (pos["entry"] - price) * CONTRACT_SIZE[symbol] * pos["qty"]
                exit_trade = True

        if exit_trade:

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
                "exit_time": now
            })

            emoji = "🟢" if net > 0 else "🔴"
            utils.log(f"{emoji} {symbol} EXIT @ {price} | PNL {round(net,6)}", tg=True)

            state["position"] = None

# ================= MAIN =================

def run():

    state = {s: {"position": None, "last_candle_time": None} for s in SYMBOLS}

    utils.log("🚀 Trend Following Strategy LIVE (Multi-Timeframe: 1h + 15m)", tg=True)

    while True:

        try:
            for symbol in SYMBOLS:

                ha_15m, ha_1h = fetch_multi_timeframe_data(symbol)

                if ha_15m is None or ha_1h is None:
                    continue

                latest_candle_time = ha_15m.index[-1]

                if state[symbol]["last_candle_time"] == latest_candle_time:
                    continue

                state[symbol]["last_candle_time"] = latest_candle_time

                price = utils.fetch_price(symbol)

                if price is None:
                    continue

                process_symbol(symbol, ha_15m, ha_1h, price, state[symbol])

            time.sleep(60)

        except Exception as e:
            utils.log(f"Runtime error: {e}", tg=True)
            time.sleep(5)


if __name__ == "__main__":
    run()