import os
import time
import pandas as pd
import numpy as np
from datetime import datetime
import pandas_ta as ta
from dotenv import load_dotenv
import subprocess

from utils import TradingUtils

load_dotenv()

# ================= CONFIG =================

BOT_NAME = "grid_strategy"

SYMBOLS = ["BTCUSD", "ETHUSD"]

CONTRACT_SIZE = {"BTCUSD": 0.001, "ETHUSD": 0.01}
DEFAULT_CONTRACTS = {"BTCUSD": 100, "ETHUSD": 100}

STOPLOSS = {"BTCUSD": 500, "ETHUSD": 50}
TAKER_FEE = 0.0005

TIMEFRAME = "1m"
DAYS = 5

BALANCE = 5000
MIN_BALANCE = 500

last_git_push = time.time()

# ================= AUTO GIT =================

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

# ================= PATH =================

BASE_DIR = os.getcwd()
SAVE_DIR = os.path.join(BASE_DIR, "data", "grid_strategy")
os.makedirs(SAVE_DIR, exist_ok=True)

# ================= LOGGER =================

def save_processed_data(data, symbol):
    path = os.path.join(SAVE_DIR, f"{symbol}_processed.csv")

    df = data.copy()

    out = pd.DataFrame({
        "time": df.index,
        "HA_open": df["HA_open"],
        "HA_high": df["HA_high"],
        "HA_low": df["HA_low"],
        "HA_close": df["HA_close"],
        "Trendline": df["Trendline"],
        "Structure": df["Structure"],
        "Trend": df["Trend"],
        "BOS": df["BOS"],
        "CHoCH": df["CHoCH"],
    })

    out.fillna("", inplace=True)
    out.to_csv(path, index=False)

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

# ================= STRUCTURE =================

def calculate_structure_trendline(df, order=7):

    df = ta.ha(df["Open"], df["High"], df["Low"], df["Close"]).reset_index(drop=True)

    df["pivot_high"] = False
    df["pivot_low"] = False

    df["Structure"] = ""
    df["Trend"] = None
    df["Trendline"] = np.nan
    df["BOS"] = ""
    df["CHoCH"] = ""

    last_high = None
    last_low = None
    trend = None
    trendline = df.loc[0, "HA_close"]

    # pivots
    for i in range(order, len(df)):
        if df["HA_high"].iloc[i] == df["HA_high"].iloc[i-order:i+1].max():
            df.loc[i, "pivot_high"] = True

        if df["HA_low"].iloc[i] == df["HA_low"].iloc[i-order:i+1].min():
            df.loc[i, "pivot_low"] = True

    # structure
    for i in range(len(df)):

        structure = ""

        if df.loc[i, "pivot_high"]:
            h = df.loc[i, "HA_high"]

            if last_high is None:
                structure = "H"
            else:
                if h > last_high:
                    structure = "HH"
                    df.loc[i, "BOS"] = "UP" if trend != "DOWN" else ""
                    df.loc[i, "CHoCH"] = "UP" if trend == "DOWN" else ""
                    trend = "UP"
                else:
                    structure = "LH"

            last_high = h

        elif df.loc[i, "pivot_low"]:
            l = df.loc[i, "HA_low"]

            if last_low is None:
                structure = "L"
            else:
                if l < last_low:
                    structure = "LL"
                    df.loc[i, "BOS"] = "DOWN" if trend != "UP" else ""
                    df.loc[i, "CHoCH"] = "DOWN" if trend == "UP" else ""
                    trend = "DOWN"
                else:
                    structure = "HL"

            last_low = l

        df.loc[i, "Structure"] = structure
        df.loc[i, "Trend"] = trend

        if trend == "UP" and last_low is not None:
            trendline = last_low
        elif trend == "DOWN" and last_high is not None:
            trendline = last_high

        df.loc[i, "Trendline"] = trendline

    return df

# ================= STRATEGY =================

def process_symbol(symbol, df, price, state, is_new_candle):

    ha = calculate_structure_trendline(df)

    if is_new_candle:
        save_processed_data(ha, symbol)

    last = ha.iloc[-2]
    prev = ha.iloc[-3]

    pos = state["position"]
    now = datetime.now()

    # ===== EXIT =====
    if pos:

        exit_trade = False
        pnl = 0

        if pos["side"] == "long" and last.HA_close < last.Trendline:
            pnl = (price - pos["entry"]) * CONTRACT_SIZE[symbol] * pos["qty"]
            exit_trade = True

        elif pos["side"] == "short" and last.HA_close > last.Trendline:
            pnl = (pos["entry"] - price) * CONTRACT_SIZE[symbol] * pos["qty"]
            exit_trade = True

        if exit_trade:
            fee = utils.commission(pos["entry"], pos["qty"], symbol) + \
                  utils.commission(price, pos["qty"], symbol)

            net = pnl - fee
            state["balance"] += net

            utils.log(f"EXIT {symbol} | PNL: {round(net, 4)}", tg=True)

            # ✅ SAVE TRADE
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

            state["position"] = None
            state["last_exit_candle"] = df.index[-1]
            return

    # ===== ENTRY =====
    if not pos and is_new_candle:

        if state.get("last_exit_candle") == df.index[-1]:
            return

        if state["balance"] < MIN_BALANCE:
            utils.log("⚠️ Low balance", tg=True)
            return

        if last.BOS == "UP" and last.Trend == "UP":
            state["position"] = {
                "side": "long",
                "entry": price,
                "qty": DEFAULT_CONTRACTS[symbol],
                "entry_time": now
            }
            utils.log(f"🟢 LONG {symbol} @ {price}", tg=True)

        elif last.BOS == "DOWN" and last.Trend == "DOWN":
            state["position"] = {
                "side": "short",
                "entry": price,
                "qty": DEFAULT_CONTRACTS[symbol],
                "entry_time": now
            }
            utils.log(f"🔴 SHORT {symbol} @ {price}", tg=True)

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

    utils.log("🚀 NON-REPAINT BOT STARTED", tg=True)

    while True:

        try:
            for symbol in SYMBOLS:

                df = utils.fetch_candles(symbol)

                if df is None or len(df) < 100:
                    continue

                latest = df.index[-2]
                is_new = state[symbol]["last_candle_time"] != latest

                if is_new:
                    state[symbol]["last_candle_time"] = latest

                price = utils.fetch_price(symbol)
                if price is None:
                    continue

                process_symbol(symbol, df, price, state[symbol], is_new)

            time.sleep(3)

        except Exception as e:
            utils.log(f"ERROR: {e}", tg=True)
            time.sleep(5)

# ================= START =================

if __name__ == "__main__":
    run()