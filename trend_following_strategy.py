import os
import time
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import pytz

from dotenv import load_dotenv
from utils import TradingUtils

load_dotenv()

# ================= CONFIG =================

BOT_NAME = "supertrend_trend_bot"

SYMBOLS = ["BTCUSD", "ETHUSD"]

CONTRACT_SIZE = {"BTCUSD": 0.001, "ETHUSD": 0.01}

QTY = {"BTCUSD": 100, "ETHUSD": 100}

TAKER_FEE = 0.0005
SLEEP_TIME = 2

BUFFER_MULT = 0.2   # ATR buffer

IST = pytz.timezone("Asia/Kolkata")


# ================= TIME =================

def get_ist_time():
    return datetime.now(IST)


# ================= HEIKIN ASHI =================

def add_heikin_ashi(df):
    df = df.copy()

    ha_close = (df["Open"] + df["High"] + df["Low"] + df["Close"]) / 4

    ha_open = [df["Open"].iloc[0]]
    for i in range(1, len(df)):
        ha_open.append((ha_open[i - 1] + ha_close.iloc[i - 1]) / 2)

    ha_open = pd.Series(ha_open, index=df.index)

    ha_high = pd.concat([df["High"], ha_open, ha_close], axis=1).max(axis=1)
    ha_low = pd.concat([df["Low"], ha_open, ha_close], axis=1).min(axis=1)

    df["HA_Open"] = ha_open
    df["HA_High"] = ha_high
    df["HA_Low"] = ha_low
    df["HA_Close"] = ha_close

    return df


# ================= EMA FILTER =================

def add_ema_filter(df, period=50):
    df = df.copy()
    df["ema"] = df["HA_Close"].ewm(span=period).mean()
    return df


# ================= SUPER TREND (ON HA) =================

def add_supertrend(df, period=10, multiplier=3):
    df = df.copy()

    high = df["HA_High"]
    low = df["HA_Low"]
    close = df["HA_Close"]

    df["tr"] = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs()
    ], axis=1).max(axis=1)

    df["atr"] = df["tr"].rolling(period).mean()

    df["hl2"] = (high + low) / 2

    df["upperband"] = df["hl2"] + (multiplier + BUFFER_MULT) * df["atr"]
    df["lowerband"] = df["hl2"] - (multiplier + BUFFER_MULT) * df["atr"]

    df["trend"] = 1
    df["supertrend"] = 0.0

    for i in range(1, len(df)):
        prev = i - 1

        if close.iloc[i] > df["upperband"].iloc[prev]:
            df.at[df.index[i], "trend"] = 1

        elif close.iloc[i] < df["lowerband"].iloc[prev]:
            df.at[df.index[i], "trend"] = -1

        else:
            df.at[df.index[i], "trend"] = df["trend"].iloc[prev]

            if df["trend"].iloc[i] == 1:
                df.at[df.index[i], "lowerband"] = max(
                    df["lowerband"].iloc[i],
                    df["lowerband"].iloc[prev]
                )
            else:
                df.at[df.index[i], "upperband"] = min(
                    df["upperband"].iloc[i],
                    df["upperband"].iloc[prev]
                )

        df.at[df.index[i], "supertrend"] = (
            df["lowerband"].iloc[i]
            if df["trend"].iloc[i] == 1
            else df["upperband"].iloc[i]
        )

    return df


# ================= INIT =================

utils = TradingUtils(
    contract_size=CONTRACT_SIZE,
    taker_fee=TAKER_FEE,
    timeframe="5m",
    days=2,
    telegram_token=os.getenv("trend_following_strategy_bot"),
    telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID"),
    bot_name=BOT_NAME
)


# ================= STRATEGY =================

def process_symbol(symbol, df_5m, df_15m, price, state):

    sym = state["symbols"][symbol]
    positions = sym["positions"]
    now = get_ist_time()
    qty = QTY[symbol]

    if df_5m is None or df_15m is None:
        return

    if len(df_5m) < 60 or len(df_15m) < 60:
        return

    # ================= FEATURES =================
    df_5m = add_heikin_ashi(df_5m)
    df_15m = add_heikin_ashi(df_15m)

    df_5m = add_ema_filter(df_5m)
    df_15m = add_ema_filter(df_15m)

    df_5m = add_supertrend(df_5m)
    df_15m = add_supertrend(df_15m)

    # ================= SIGNALS =================

    prev_5m = df_5m.iloc[-2]["trend"]
    curr_5m = df_5m.iloc[-1]["trend"]
    trend_15m = df_15m.iloc[-1]["trend"]

    ema_5m = df_5m.iloc[-1]["ema"]
    close_5m = df_5m.iloc[-1]["HA_Close"]
    prev_close_5m = df_5m.iloc[-2]["HA_Close"]

    atr = df_5m.iloc[-1]["atr"]

    buy_signal = (
       close_5m > prev_close_5m and  curr_5m == 1 and trend_15m == 1
    )

    sell_signal = (
         close_5m < prev_close_5m and curr_5m == -1 and trend_15m == -1
    )

    # ================= ENTRY =================

    if buy_signal and not any(p["side"] == "long" for p in positions):
        positions.append({
            "side": "long",
            "entry": price,
            "qty": qty,
            "sl": price - atr * 2,
            "trail_sl": price - atr * 2,
            "time": now
        })
        utils.log(f"🚀 BUY {symbol} @ {price}", tg=True)

    elif sell_signal and not any(p["side"] == "short" for p in positions):
        positions.append({
            "side": "short",
            "entry": price,
            "qty": qty,
            "sl": price + atr * 2,
            "trail_sl": price + atr * 2,
            "time": now
        })
        utils.log(f"⚡ SHORT {symbol} @ {price}", tg=True)

    # ================= EXIT =================

    for p in positions[:]:

        exit_trade = False

        if p["side"] == "long":
            p["trail_sl"] = max(p["trail_sl"], price - atr * 2)

            if curr_5m == -1:
                pnl = (price - p["entry"]) * CONTRACT_SIZE[symbol] * p["qty"]
                exit_trade = True

        else:
            p["trail_sl"] = min(p["trail_sl"], price + atr * 2)

            if curr_5m == 1:
                pnl = (p["entry"] - price) * CONTRACT_SIZE[symbol] * p["qty"]
                exit_trade = True

        if not exit_trade:
            continue

        fee = utils.commission(p["entry"], p["qty"], symbol) + \
              utils.commission(price, p["qty"], symbol)

        net = pnl - fee
        state["balance"] += net

        utils.save_trade({
            "symbol": symbol,
            "side": p["side"],
            "entry_price": p["entry"],
            "exit_price": price,
            "qty": p["qty"],
            "net_pnl": round(net, 6),
            "entry_time": p["time"],
            "exit_time": now
        })

        utils.log(f"💰 EXIT {symbol} {p['side']} PNL: {round(net,6)}", tg=True)
        utils.log(f"💰 BALANCE: {round(state['balance'],2)}", tg=True)

        positions.remove(p)


# ================= MAIN =================

def run():

    state = {
        "balance": 10000,
        "symbols": {s: {"positions": []} for s in SYMBOLS}
    }

    utils.log("🚀 HA + SUPER TREND BOT STARTED", tg=True)

    while True:
        try:
            for symbol in SYMBOLS:

                price = utils.fetch_price(symbol)

                df_5m = utils.fetch_candles(symbol, timeframe="5m")
                df_15m = utils.fetch_candles(symbol, timeframe="15m")

                if price is None or df_5m.empty or df_15m.empty:
                    continue

                process_symbol(symbol, df_5m, df_15m, price, state)

            time.sleep(SLEEP_TIME)

        except Exception as e:
            utils.log(f"🚨 ERROR: {e}", tg=True)
            time.sleep(5)


# ================= START =================

if __name__ == "__main__":
    run()