import os
import time
import pandas as pd
import pandas_ta as ta
from datetime import datetime
from dotenv import load_dotenv
import traceback
import subprocess

from utils import TradingUtils

load_dotenv()

# ================= CONFIG =================

BOT_NAME = "supertrend_reverse_strategy"

SYMBOLS = ["BTCUSD", "ETHUSD"]

DEFAULT_CONTRACTS = {"BTCUSD": 100, "ETHUSD": 100}
CONTRACT_SIZE = {"BTCUSD": 0.001, "ETHUSD": 0.01}

TAKER_FEE = 0.0005
TIMEFRAME = "5m"
DAYS = 1
ADX_LENGTH = 14


# ================= INIT UTILS =================

utils = TradingUtils(
    contract_size=CONTRACT_SIZE,
    taker_fee=TAKER_FEE,
    timeframe=TIMEFRAME,
    days=DAYS,
    telegram_token=os.getenv("TEL_BOT_TOKEN"),
    telegram_chat_id=os.getenv("TEL_CHAT_ID"),
    bot_name=BOT_NAME
)

# ================= GIT =================

last_git_push = time.time()

def auto_git_push():
    global last_git_push

    if time.time() - last_git_push < 3600:
        return

    try:
        subprocess.run("git add -A", shell=True)

        res = subprocess.run(
            'git diff --cached --quiet || git commit -m "auto update"',
            shell=True
        )

        if res.returncode != 0:
            utils.log("✅ Changes committed")

        res = subprocess.run("git push origin main", shell=True)

        if res.returncode == 0:
            utils.log("✅ Git Push Done", tg=True)

        last_git_push = time.time()

    except Exception as e:
        utils.log(f"Git Error: {e}")

# ================= INDICATORS =================

def build_indicators(df):

    ha = ta.ha(df["open"], df["high"], df["low"], df["close"])

    df["HA_open"] = ha["HA_open"]
    df["HA_high"] = ha["HA_high"]
    df["HA_low"] = ha["HA_low"]
    df["HA_close"] = ha["HA_close"]

    st = ta.supertrend(
        df["HA_high"], df["HA_low"], df["HA_close"],
        length=21, multiplier=2.5
    )

    df["SUPERTREND"] = st["SUPERT_21_2.5"]

    df["UPPER"] = df["HA_high"].rolling(21).max()
    df["LOWER"] = df["HA_low"].rolling(21).min()

    adx = ta.adx(df["HA_high"], df["HA_low"], df["HA_close"], length=ADX_LENGTH)

    df["ADX"] = adx[f"ADX_{ADX_LENGTH}"]
    df["ADX_MA"] = df["ADX"].rolling(5).mean()

    return df

# ================= EXIT =================

def exit_trade(symbol, price, pos, state):

    pnl = (
        (price - pos["entry"]) if pos["side"] == "long"
        else (pos["entry"] - price)
    ) * CONTRACT_SIZE[symbol] * pos["qty"]

    net = pnl - utils.commission(price, pos["qty"], symbol)

    trade = {
        "entry_time": pos["entry_time"],
        "exit_time": datetime.now(),
        "symbol": symbol,
        "side": pos["side"],
        "entry_price": pos["entry"],
        "exit_price": price,
        "qty": pos["qty"],
        "net_pnl": net
    }

    utils.save_trade(trade)
    utils.log(f"{symbol} EXIT {net}", tg=True)

    state["position"] = None

# ================= STRATEGY =================

def process_symbol(symbol, df, state):

    if df.empty or len(df) < 50:
        return

    df = build_indicators(df)

    last = df.iloc[-2]
    prev = df.iloc[-4]

    price = utils.fetch_price(symbol)
    if price is None:
        return

    pos = state["position"]

    cross_up = (
        last.HA_close > last.SUPERTREND and
        last.HA_close > prev.UPPER and
        last.ADX > last.ADX_MA
    )

    cross_down = (
        last.HA_close < last.SUPERTREND and
        last.HA_close < prev.LOWER and
        last.ADX > last.ADX_MA
    )

    candle_time = df.index[-2]

    if pos is None and state["last_candle"] != candle_time:

        if cross_up:
            state["position"] = {
                "side": "long",
                "entry": price,
                "stop": last.SUPERTREND,
                "best_price": price,
                "qty": DEFAULT_CONTRACTS[symbol],
                "entry_time": datetime.now()
            }
            utils.log(f"{symbol} LONG {price}", tg=True)

        elif cross_down:
            state["position"] = {
                "side": "short",
                "entry": price,
                "stop": last.SUPERTREND,
                "best_price": price,
                "qty": DEFAULT_CONTRACTS[symbol],
                "entry_time": datetime.now()
            }
            utils.log(f"{symbol} SHORT {price}", tg=True)

        state["last_candle"] = candle_time

    if pos:
        distance = abs(last.HA_close - last.SUPERTREND)

        if pos["side"] == "long":
            if price > pos["best_price"]:
                pos["best_price"] = price
                pos["stop"] = max(pos["stop"], price - distance)

            if price <= pos["stop"]:
                exit_trade(symbol, price, pos, state)

        else:
            if price < pos["best_price"]:
                pos["best_price"] = price
                pos["stop"] = min(pos["stop"], price + distance)

            if price >= pos["stop"]:
                exit_trade(symbol, price, pos, state)

# ================= MAIN =================

def run():

    state = {s: {"position": None, "last_candle": None} for s in SYMBOLS}

    utils.log("🚀 BOT STARTED", tg=True)

    while True:
        try:
            for symbol in SYMBOLS:

                df = utils.fetch_candles(symbol)

                if not df.empty:
                    process_symbol(symbol, df, state[symbol])

                time.sleep(1)

            auto_git_push()
            time.sleep(2)

        except Exception:
            utils.log(traceback.format_exc(), tg=True)
            time.sleep(5)

if __name__ == "__main__":
    run()