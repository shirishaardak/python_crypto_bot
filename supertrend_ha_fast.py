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

BOT_NAME = "supertrend_ha_fast"

SYMBOLS = ["BTCUSD", "ETHUSD"]

CONTRACT_SIZE = {"BTCUSD": 0.001, "ETHUSD": 0.01}
QTY = {"BTCUSD": 100, "ETHUSD": 100}

TAKER_FEE = 0.0005
SLEEP_TIME = 5

SAVE_DIR = "data/supertrend_ha_fast"
os.makedirs(SAVE_DIR, exist_ok=True)

IST = pytz.timezone("Asia/Kolkata")

# ================= TIME =================

def get_ist_time():
    return datetime.now(IST)

# ================= NEW CANDLE =================

last_candle_time = {}

def is_new_candle(symbol, df):
    t = df.index[-1]

    if symbol not in last_candle_time:
        last_candle_time[symbol] = t
        return True

    if t != last_candle_time[symbol]:
        last_candle_time[symbol] = t
        return True

    return False

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

def save_processed_data(df, symbol):
    path = os.path.join(SAVE_DIR, f"{symbol}.csv")

    out = pd.DataFrame({
        "time": df.index,
        "ha_open": df["HA_open"],
        "ha_high": df["HA_high"],
        "ha_low": df["HA_low"],
        "ha_close": df["HA_close"],
        "supertrend": df["supertrend"]
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

    df = df.tail(200)
    df = add_heikin_ashi(df)

    st = ta.supertrend(
        high=df["HA_high"],
        low=df["HA_low"],
        close=df["HA_close"],
        length=10,
        multiplier=3
    )

    supertrend_col = [c for c in st.columns if "SUPERT_" in c and not c.endswith("d")][0]
    trend_col = [c for c in st.columns if "SUPERTd_" in c][0]

    df["supertrend"] = st[supertrend_col]
    df["trend"] = st[trend_col]

    df["atr"] = ta.atr(df["HA_high"], df["HA_low"], df["HA_close"], length=10)
    df["atr_ma"] = df["atr"].rolling(20).mean()

    df["trend_strength"] = abs(df["HA_close"] - df["supertrend"]) / df["atr"]

    return df.dropna()

# ================= STRATEGY =================

def process_symbol(symbol, df, price, state):

    sym = state["symbols"][symbol]
    positions = sym["positions"]
    qty = QTY[symbol]

    df = add_indicators(df)

    if len(df) < 30:
        return

    curr = df.iloc[-1]
    prev = df.iloc[-2]

    close = curr["HA_close"]
    prev_close = prev["HA_close"]

    st = curr["supertrend"]
    prev_st = prev["supertrend"]

    atr = curr["atr"]
    atr_ma = curr["atr_ma"]

    momentum_ok = atr > atr_ma
    trend_strong = curr["trend_strength"] > 0.8
    trade_allowed = momentum_ok

    # ================= LEVEL INIT =================

    if "level" not in sym:
        sym["level"] = {
            "high": None,
            "low": None,
            "locked": False,
            "side": None,
            "attempted": False
        }

    level = sym["level"]

    # ================= TREND FLIP =================

    if prev_close <= prev_st and close > st:
        level.update({
            "high": curr["HA_high"],
            "low": None,
            "locked": True,
            "side": "long",
            "attempted": False
        })

        utils.log(f"📈 {symbol} LONG LEVEL SET @ {round(level['high'], 2)}", tg=True)

    elif prev_close >= prev_st and close < st:
        level.update({
            "low": curr["HA_low"],
            "high": None,
            "locked": True,
            "side": "short",
            "attempted": False
        })

        utils.log(f"📉 {symbol} SHORT LEVEL SET @ {round(level['low'], 2)}", tg=True)

    # ================= ENTRY =================

    if level["locked"] and not level["attempted"]:

        if level["side"] == "long" and close > level["high"] and trade_allowed:
            if not any(p["side"] == "long" for p in positions):

                positions.append({
                    "side": "long",
                    "entry": price,
                    "qty": qty,
                    "trail_sl": price - atr * 3,
                    "entry_time": get_ist_time()
                })

                level["attempted"] = True
                level["locked"] = False

                utils.log(f"🚀 {symbol} LONG ENTRY @ {price}", tg=True)

        elif level["side"] == "short" and close < level["low"] and trade_allowed:
            if not any(p["side"] == "short" for p in positions):

                positions.append({
                    "side": "short",
                    "entry": price,
                    "qty": qty,
                    "trail_sl": price + atr * 3,
                    "entry_time": get_ist_time()
                })

                level["attempted"] = True
                level["locked"] = False

                utils.log(f"🔻 {symbol} SHORT ENTRY @ {price}", tg=True)

# ================= EXIT =================

    for p in positions[:]:

        trail_step = atr

        if p["side"] == "long":

            # if price - p["entry"] > trail_step:
            #     p["trail_sl"] = max(p["trail_sl"], price - atr * 2)

            if price <= p["trail_sl"] or close < st:
                pnl = (price - p["entry"]) * CONTRACT_SIZE[symbol] * p["qty"]

            else:
                continue

        else:

            # if p["entry"] - price > trail_step:
            #     p["trail_sl"] = min(p["trail_sl"], price + atr * 2)

            if price >= p["trail_sl"] or close > st:
                pnl = (p["entry"] - price) * CONTRACT_SIZE[symbol] * p["qty"]

            else:
                continue

        fee = utils.commission(p["entry"], p["qty"], symbol) + \
              utils.commission(price, p["qty"], symbol)

        net = pnl - fee
        state["balance"] += net

        now = get_ist_time()

        emoji = "🟢" if net > 0 else "🔴"
        utils.log(f"{emoji} {symbol} EXIT @ {price} | PNL: {round(net,6)}", tg=True)
        utils.log(f"💰 Balance: {round(state['balance'],2)}", tg=True)

        utils.save_trade({
            "symbol": symbol,
            "side": p["side"],
            "entry_price": p["entry"],
            "exit_price": price,
            "qty": p["qty"],
            "net_pnl": round(net, 6),
            "entry_time": p.get("entry_time"),
            "exit_time": now
        })

        positions.remove(p)

# ================= MAIN =================

utils = TradingUtils(
    contract_size=CONTRACT_SIZE,
    taker_fee=TAKER_FEE,
    timeframe="15m",
    days=5,
    telegram_token=os.getenv("supertrend_ha_fast_bot"),
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

                df = safe_fetch(utils.fetch_candles, symbol, "15m")
                if df is None or df.empty:
                    continue

                if not is_new_candle(symbol, df):
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