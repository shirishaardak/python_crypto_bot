import os
import time
import pandas as pd
import numpy as np
from datetime import datetime
import pytz
import pandas_ta as ta
import subprocess
from dotenv import load_dotenv
from utils import TradingUtils

load_dotenv()

# =========================================================
# CONFIG
# =========================================================

BOT_NAME = "supertrend_ha_fast"

SYMBOLS = ["BTCUSD"]

CONTRACT_SIZE = {
    "BTCUSD": 0.001
}

QTY = {
    "BTCUSD": 1000
}

STOPLOSS = {
    "BTCUSD": 300
}

TP = {
    "BTCUSD": 200
}

TRAIL_STEP = 5

TAKER_FEE = 0.0005

SLEEP_TIME = 5

SAVE_DIR = "data/supertrend_ha_fast"

os.makedirs(SAVE_DIR, exist_ok=True)

IST = pytz.timezone("Asia/Kolkata")

last_git_push = time.time()

# =========================================================
# AUTO GIT
# =========================================================

def auto_git_push():

    global last_git_push

    if time.time() - last_git_push < 3600:
        return

    try:

        subprocess.run("git add -A", shell=True)

        subprocess.run(
            'git diff --cached --quiet || git commit -m "auto update"',
            shell=True
        )

        subprocess.run(
            "git push origin main",
            shell=True
        )

        last_git_push = time.time()

    except Exception as e:

        utils.log(f"Git Error: {e}")

# =========================================================
# TIME
# =========================================================

def get_ist_time():
    return datetime.now(IST)

# =========================================================
# NEW CANDLE CHECK
# =========================================================

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

# =========================================================
# SAFE FETCH
# =========================================================

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

# =========================================================
# HEIKIN ASHI
# =========================================================

def add_heikin_ashi(df):

    ha_close = (
        df["Open"] +
        df["High"] +
        df["Low"] +
        df["Close"]
    ) / 4

    ha_open = np.zeros(len(df))

    ha_open[0] = df["Open"].iloc[0]

    for i in range(1, len(df)):

        ha_open[i] = (
            ha_open[i - 1]
            + ha_close.iloc[i - 1]
        ) / 2

    df["HA_open"] = ha_open

    df["HA_high"] = np.maximum.reduce([
        df["High"],
        ha_open,
        ha_close
    ])

    df["HA_low"] = np.minimum.reduce([
        df["Low"],
        ha_open,
        ha_close
    ])

    df["HA_close"] = ha_close

    return df

# =========================================================
# INDICATORS
# =========================================================

def add_indicators(df):

    df = df.tail(200).copy()

    df = add_heikin_ashi(df)

    st = ta.supertrend(
        high=df["HA_high"],
        low=df["HA_low"],
        close=df["HA_close"],
        length=10,
        multiplier=3
    )

    supertrend_col = [
        c for c in st.columns
        if "SUPERT_" in c
        and not c.endswith("d")
    ][0]

    trend_col = [
        c for c in st.columns
        if "SUPERTd_" in c
    ][0]

    df["supertrend"] = st[supertrend_col]
    df["trend"] = st[trend_col]

    return df.dropna()

# =========================================================
# CROSSOVER FIXED
# =========================================================

def get_crossover(df):

    if len(df) < 2:
        return None

    prev_trend = df["trend"].iloc[-2]
    curr_trend = df["trend"].iloc[-1]

    if prev_trend != curr_trend:
        return curr_trend

    return None

# =========================================================
# TRAILING STOP
# =========================================================

def update_trailing_stop(symbol, price, position):

    if position["side"] == "long":

        profit_move = price - position["entry"]

        if profit_move <= 0:
            return

        steps = int(profit_move // TRAIL_STEP)

        new_sl = (
            position["entry"]
            - STOPLOSS[symbol]
            + steps * TRAIL_STEP
        )

        position["trail_sl"] = max(
            position["trail_sl"],
            new_sl
        )

    else:

        profit_move = position["entry"] - price

        if profit_move <= 0:
            return

        steps = int(profit_move // TRAIL_STEP)

        new_sl = (
            position["entry"]
            + STOPLOSS[symbol]
            - steps * TRAIL_STEP
        )

        position["trail_sl"] = min(
            position["trail_sl"],
            new_sl
        )

# =========================================================
# ENTRY
# =========================================================

def process_symbol(symbol, df, price, state):

    if not state["trading_enabled"]:
        return

    sym = state["symbols"][symbol]
    positions = sym["positions"]

    if len(positions) > 0:
        return

    trend_dir = get_crossover(df)

    if trend_dir is None:
        return

    qty = QTY[symbol]

    if trend_dir == 1:

        positions.append({
            "side": "long",
            "entry": price,
            "qty": qty,
            "trail_sl": price - STOPLOSS[symbol],
            "entry_time": get_ist_time()
        })

        utils.log(
            f"🚀 {symbol} LONG ENTRY @ {price}",
            tg=True
        )

    elif trend_dir == -1:

        positions.append({
            "side": "short",
            "entry": price,
            "qty": qty,
            "trail_sl": price + STOPLOSS[symbol],
            "entry_time": get_ist_time()
        })

        utils.log(
            f"🔻 {symbol} SHORT ENTRY @ {price}",
            tg=True
        )

# =========================================================
# UTILS
# =========================================================

utils = TradingUtils(
    contract_size=CONTRACT_SIZE,
    taker_fee=TAKER_FEE,
    timeframe="5m",
    days=5,
    telegram_token=os.getenv("supertrend_ha_fast_bot"),
    telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID"),
    bot_name=BOT_NAME
)

# =========================================================
# MAIN
# =========================================================

def run():

    state = {
        "balance": 10000,
        "daily_pnl": 0,
        "last_day": get_ist_time().date(),
        "trading_enabled": True,
        "symbols": {
            s: {"positions": []}
            for s in SYMBOLS
        }
    }

    utils.log("🚀 BOT STARTED", tg=True)

    while True:

        try:

            today = get_ist_time().date()

            if today != state["last_day"]:

                state["daily_pnl"] = 0
                state["last_day"] = today
                state["trading_enabled"] = True

                utils.log(
                    "✅ New Day Started - Trading Enabled",
                    tg=True
                )

            for symbol in SYMBOLS:

                price = safe_fetch(
                    utils.fetch_price,
                    symbol
                )

                if price is None:
                    continue

                sym = state["symbols"][symbol]
                positions = sym["positions"]

                for p in positions[:]:

                    update_trailing_stop(
                        symbol,
                        price,
                        p
                    )
                    supertrend_price = processed_df["supertrend"].iloc[-1]

                    exit_trade = False
                    pnl = 0

                    if p["side"] == "long":

                        if (
                            price >= p["entry"] + TP[symbol]
                            or price <= p["trail_sl"]
                            or price <= supertrend_price
                        ):

                            pnl = (
                                (price - p["entry"])
                                * CONTRACT_SIZE[symbol]
                                * p["qty"]
                            )

                            exit_trade = True

                    else:

                        if (
                            price <= p["entry"] - TP[symbol]
                            or price >= p["trail_sl"]
                            or price >= supertrend_price
                        ):

                            pnl = (
                                (p["entry"] - price)
                                * CONTRACT_SIZE[symbol]
                                * p["qty"]
                            )

                            exit_trade = True

                    if exit_trade:

                        fee = (
                            utils.commission(
                                p["entry"],
                                p["qty"],
                                symbol
                            )
                            +
                            utils.commission(
                                price,
                                p["qty"],
                                symbol
                            )
                        )

                        net = pnl - fee

                        state["balance"] += net
                        state["daily_pnl"] += net

                        utils.log(
                            f"{'🟢' if net > 0 else '🔴'} "
                            f"{symbol} EXIT @ {price} | "
                            f"PNL: {round(net, 2)} | "
                            f"DAILY: {round(state['daily_pnl'], 2)} | "
                            f"SL: {round(p['trail_sl'], 2)}",
                            tg=True
                        )

                        utils.save_trade({
                            "symbol": symbol,
                            "side": p["side"],
                            "entry_price": p["entry"],
                            "exit_price": price,
                            "qty": p["qty"],
                            "net_pnl": round(net, 6),
                            "entry_time": p["entry_time"],
                            "exit_time": get_ist_time()
                        })

                        positions.remove(p)

                df = safe_fetch(
                    utils.fetch_candles,
                    symbol,
                    "5m"
                )

                if df is None or df.empty:
                    continue

                # remove open candle if exchange returns it
                df = df.iloc[:-1]

                if not is_new_candle(symbol, df):
                    continue

                processed_df = add_indicators(df)

                process_symbol(
                    symbol,
                    processed_df,
                    price,
                    state
                )

            auto_git_push()

            time.sleep(SLEEP_TIME)

        except Exception as e:

            print("ERROR:", e)

            time.sleep(5)

# =========================================================
# START
# =========================================================

if __name__ == "__main__":
    run()