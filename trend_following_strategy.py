import os
import time
import pandas as pd
import numpy as np
from datetime import datetime
import pytz
import pandas_ta as ta

from dotenv import load_dotenv
from utils import TradingUtils

load_dotenv()

# ================= CONFIG =================

BOT_NAME = "supertrend_ha_trailing"

SYMBOLS = ["BTCUSD", "ETHUSD"]

CONTRACT_SIZE = {"BTCUSD": 0.001, "ETHUSD": 0.01}
QTY = {"BTCUSD": 100, "ETHUSD": 100}

TAKER_FEE = 0.0005
SLEEP_TIME = 10

SAVE_DIR = "data/supertrend_ha_trailing"
os.makedirs(SAVE_DIR, exist_ok=True)

IST = pytz.timezone("Asia/Kolkata")

# ================= TIME =================

def get_ist_time():
    return datetime.now(IST)

# ================= SAFE FETCH =================

def safe_fetch(fetch_func, *args, retries=3, delay=1):
    for _ in range(retries):
        try:
            result = fetch_func(*args)
            if result is not None:
                return result
        except Exception as e:
            print("Fetch error:", e)
        time.sleep(delay)
    return None

# ================= SAVE =================

def save_processed_data(data, symbol):
    path = os.path.join(SAVE_DIR, f"{symbol}_processed.csv")

    out = pd.DataFrame({
        "time": data.index,
        "ha_open": data["HA_open"],
        "ha_high": data["HA_high"],
        "ha_low": data["HA_low"],
        "ha_close": data["HA_close"],
        "trendline": data["supertrend"],
        "atr": data["atr"]
    })

    out.to_csv(path, index=False)

# ================= HEIKIN ASHI =================

def add_heikin_ashi(df):

    ha_close = (df["Open"] + df["High"] + df["Low"] + df["Close"]) / 4

    ha_open = np.zeros(len(df))
    ha_open[0] = df["Open"].iloc[0]

    for i in range(1, len(df)):
        ha_open[i] = (ha_open[i-1] + ha_close.iloc[i-1]) / 2

    df["HA_open"] = ha_open
    df["HA_high"] = np.maximum.reduce([df["High"], ha_open, ha_close])
    df["HA_low"] = np.minimum.reduce([df["Low"], ha_open, ha_close])
    df["HA_close"] = ha_close

    return df

# ================= INDICATORS =================

def add_indicators(df):

    df = add_heikin_ashi(df)

    ha_high = df["HA_high"]
    ha_low = df["HA_low"]
    ha_close = df["HA_close"]

    st = ta.supertrend(
        high=ha_high,
        low=ha_low,
        close=ha_close,
        length=10,
        multiplier=3
    )

    df["supertrend"] = st["SUPERT_10_3.0"]
    df["trend"] = st["SUPERTd_10_3.0"]

    df["atr"] = ta.atr(
        high=ha_high,
        low=ha_low,
        close=ha_close,
        length=10
    )

    df["atr_ma"] = df["atr"].rolling(20).mean()

    # Distance from trend (sideways filter)
    df["trend_strength"] = abs(ha_close - df["supertrend"]) / df["atr"]

    df = df.dropna()

    return df

# ================= STRATEGY =================

def process_symbol(symbol, df, price, state):

    sym = state["symbols"][symbol]
    positions = sym["positions"]
    qty = QTY[symbol]

    df = add_indicators(df)

    if len(df) < 30:
        return

    save_processed_data(df, symbol)

    curr = df.iloc[-1]
    prev = df.iloc[-2]

    close = curr["HA_close"]
    prev_close = prev["HA_close"]

    st = curr["supertrend"]
    prev_st = prev["supertrend"]

    atr = curr["atr"]
    atr_ma = curr["atr_ma"]

    # ===== SIDEWAYS FILTER =====
    momentum_ok = atr > atr_ma
    trend_strong = curr["trend_strength"] > 0.8

    trade_allowed = momentum_ok and trend_strong

    # ===== STRUCTURE =====

    if "level" not in sym:
        sym["level"] = {"high": None, "low": None}

    level = sym["level"]

    if prev_close <= prev_st and close > st:
        level["high"] = curr["HA_high"]
        level["low"] = None
        utils.log(f"🚀 {symbol} Trend UP → HA High: {round(level['high'],2)}", tg=True)

    elif prev_close >= prev_st and close < st:
        level["low"] = curr["HA_low"]
        level["high"] = None
        utils.log(f"🔻 {symbol} Trend DOWN → HA Low: {round(level['low'],2)}", tg=True)

    # ===== SIGNALS =====

    buy_signal = (
        level["high"] is not None and
        close > level["high"] and
        trade_allowed
    )

    sell_signal = (
        level["low"] is not None and
        close < level["low"] and
        trade_allowed
    )

    # ===== ENTRY =====

    if buy_signal and not any(p["side"] == "long" for p in positions):
        positions.append({
            "side": "long",
            "entry": price,
            "qty": qty,
            "trail_sl": price - atr * 1.5,
        })
        utils.log(f"🚀 {symbol} BUY @ {price}", tg=True)
        level["high"] = None

    elif sell_signal and not any(p["side"] == "short" for p in positions):
        positions.append({
            "side": "short",
            "entry": price,
            "qty": qty,
            "trail_sl": price + atr * 1.5,
        })
        utils.log(f"🔻 {symbol} SELL @ {price}", tg=True)
        level["low"] = None

    # ===== TRAILING EXIT =====

    for p in positions[:]:

        trail_step = atr * 0.5

        if p["side"] == "long":

            # move trailing SL
            if price - p["entry"] > trail_step:
                p["trail_sl"] = max(p["trail_sl"], price - atr * 1.5)

            if price <= p["trail_sl"]:
                pnl = (price - p["entry"]) * CONTRACT_SIZE[symbol] * p["qty"]
            else:
                continue

        else:

            if p["entry"] - price > trail_step:
                p["trail_sl"] = min(p["trail_sl"], price + atr * 1.5)

            if price >= p["trail_sl"]:
                pnl = (p["entry"] - price) * CONTRACT_SIZE[symbol] * p["qty"]
            else:
                continue

        fee = utils.commission(p["entry"], p["qty"], symbol) + \
              utils.commission(price, p["qty"], symbol)

        net = pnl - fee
        state["balance"] += net

        emoji = "🟢" if net > 0 else "🔴"

        utils.log(f"{emoji} {symbol} EXIT @ {price} | PNL: {round(net,6)}", tg=True)
        utils.log(f"💰 Balance: {round(state['balance'],2)}", tg=True)

        positions.remove(p)

# ================= MAIN =================

utils = TradingUtils(
    contract_size=CONTRACT_SIZE,
    taker_fee=TAKER_FEE,
    timeframe="5m",
    days=2,
    telegram_token=os.getenv("trend_following_strategy_bot"),
    telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID"),
    bot_name=BOT_NAME
)

def run():

    state = {
        "balance": 10000,
        "symbols": {s: {"positions": []} for s in SYMBOLS}
    }

    print("BOT STARTED")

    while True:
        try:
            for symbol in SYMBOLS:

                df = safe_fetch(utils.fetch_candles, symbol, "5m")
                if df is None or df.empty:
                    continue

                price = safe_fetch(utils.fetch_price, symbol)
                if price is None:
                    continue

                process_symbol(symbol, df, price, state)

            time.sleep(SLEEP_TIME)

        except Exception as e:
            print("ERROR:", e)
            time.sleep(5)

if __name__ == "__main__":
    run()