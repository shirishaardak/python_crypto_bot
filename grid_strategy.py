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

BOT_NAME = "test_trading_strategy"

SYMBOLS = ["BTCUSD", "ETHUSD"]

CONTRACT_SIZE = {"BTCUSD": 0.001, "ETHUSD": 0.01}
DEFAULT_CONTRACTS = {"BTCUSD": 100, "ETHUSD": 500}

STOPLOSS = {"BTCUSD": 200, "ETHUSD": 10}
TAKER_FEE = 0.0005

TIMEFRAME = "5m"
DAYS = 5

MIN_BALANCE = 5000

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

        res = subprocess.run("git push origin main", shell=True)

        if res.returncode == 0:
            utils.log("✅ Git Push Done", tg=True)

        last_git_push = time.time()

    except Exception as e:
        utils.log(f"Git Error: {e}")

# ================= SAVE =================

BASE_DIR = os.getcwd()
SAVE_DIR = os.path.join(BASE_DIR, "data", BOT_NAME)
os.makedirs(SAVE_DIR, exist_ok=True)

def save_processed_data(data, symbol):
    path = os.path.join(SAVE_DIR, f"{symbol}_processed.csv")

    out = pd.DataFrame({
        "time": data.index,
        "HA_open": data["HA_open"],
        "HA_high": data["HA_high"],
        "HA_low": data["HA_low"],
        "HA_close": data["HA_close"],
        "trendline": data["Trendline"], 
    })

    out.to_csv(path, index=False)

# ================= INIT =================

utils = TradingUtils(
    contract_size=CONTRACT_SIZE,
    taker_fee=TAKER_FEE,
    timeframe=TIMEFRAME,
    days=DAYS,
    telegram_token=os.getenv("TELEGRAM_BOT_TOKEN"),
    telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID"),
    bot_name=BOT_NAME
)

# ================= TRENDLINE =================

def calculate_trendline(df):

    ha = ta.ha(df["Open"], df["High"], df["Low"], df["Close"]).reset_index(drop=True)

    ha["atr"] = ta.atr(ha["HA_high"], ha["HA_low"], ha["HA_close"], length=10)
    ha["atr_ma"] = ha["atr"].rolling(20).mean()

    order = 7

    ha["UPPER"] = ha["HA_high"].rolling(order).max()
    ha["LOWER"] = ha["HA_low"].rolling(order).min()

    ha["Trendline"] = np.nan

    trend = ha.loc[0, "HA_close"]
    ha.loc[0, "Trendline"] = trend

    for i in range(1, len(ha)):

        prev_trend = ha.loc[i - 1, "Trendline"]
        current_close = ha.loc[i, "HA_close"]

        upper = ha.loc[i, "UPPER"]
        lower = ha.loc[i, "LOWER"]

        prev_upper = ha.loc[i-1, "UPPER"]
        prev_lower = ha.loc[i-1, "LOWER"]

        if np.isnan(upper) or np.isnan(lower):
            ha.loc[i, "Trendline"] = prev_trend
            continue

        if ha.loc[i, "HA_high"] > prev_upper and current_close > prev_trend:
            trend = lower

        elif ha.loc[i, "HA_low"] < prev_lower and current_close < prev_trend:
            trend = upper

        else:
            trend = prev_trend

        ha.loc[i, "Trendline"] = trend

    return ha

# ================= FILTERS =================

def is_volatile(ha):
    return ha["atr"].iloc[-1] > ha["atr_ma"].iloc[-5]

def strong_trend(ha):
    last = ha.iloc[-2]
    strength = abs(last.HA_close - last.Trendline)
    return strength > ha["atr"].iloc[-2] * 0.5

# ================= STRATEGY =================

def process_symbol(symbol, df, price, state, is_new_candle):

    ha = calculate_trendline(df)
    # save_processed_data(ha, symbol)

    last = ha.iloc[-2]
    prev = ha.iloc[-3]

    pos = state["position"]
    now = datetime.now()

    # ================= EXIT =================
    if pos:

        exit_trade = False

        if pos["side"] == "long":
            if price >= pos["TP"] or last.HA_close < last.Trendline:
                pnl = (price - pos["entry"]) * CONTRACT_SIZE[symbol] * pos["qty"]
                exit_trade = True

        else:
            if price <= pos["TP"] or last.HA_close > last.Trendline:
                pnl = (pos["entry"] - price) * CONTRACT_SIZE[symbol] * pos["qty"]
                exit_trade = True

        if exit_trade:

            entry_fee = utils.commission(pos["entry"], pos["qty"], symbol)
            exit_fee  = utils.commission(price, pos["qty"], symbol)

            net = pnl - (entry_fee + exit_fee)

            state["balance"] += net

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

            utils.log(f"{'🟢' if net > 0 else '🔴'} {symbol} EXIT @ {price} | PNL: {round(net,6)}", tg=True)
            utils.log(f"💰 Balance: {round(state['balance'],2)}", tg=True)

            state["position"] = None
            state["last_exit_candle"] = df.index[-1]
            return

    # ================= ENTRY =================
    if not pos and is_new_candle:
       
        if state.get("last_exit_candle") == df.index[-1]:
            return

        if state["balance"] < MIN_BALANCE:
            utils.log(f"⚠️ Low balance: {state['balance']}", tg=True)
            return

        if not is_volatile(ha) or not strong_trend(ha):
            return

        # LONG
        if last.HA_close > last.Trendline and last.HA_close > prev.HA_open:

            state["position"] = {
                "side": "long",
                "entry": price,
                "qty": DEFAULT_CONTRACTS[symbol],
                "entry_time": now,
                "SL": price - STOPLOSS[symbol],
                "TP": price + STOPLOSS[symbol]
            }

            utils.log(f"🟢 {symbol} LONG @ {price}", tg=True)

        # SHORT
        elif last.HA_close < last.Trendline and last.HA_close < prev.HA_open:

            state["position"] = {
                "side": "short",
                "entry": price,
                "qty": DEFAULT_CONTRACTS[symbol],
                "entry_time": now,
                "SL": price + STOPLOSS[symbol],
                "TP": price - STOPLOSS[symbol]
            }

            utils.log(f"🔴 {symbol} SHORT @ {price}", tg=True)

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

    utils.log("🚀 BOT STARTED (SMART MODE)", tg=True)

    while True:

        try:

            for symbol in SYMBOLS:

                df = utils.fetch_candles(symbol)

                if df is None or len(df) < 100:
                    continue

                latest_candle_time = df.index[-2]
                is_new_candle = state[symbol]["last_candle_time"] != latest_candle_time

                if is_new_candle:
                    state[symbol]["last_candle_time"] = latest_candle_time

                price = utils.fetch_price(symbol)

                if price is None:
                    continue

                process_symbol(symbol, df, price, state[symbol], is_new_candle)

            time.sleep(3)

        except Exception as e:
            utils.log(f"🚨 Error: {e}", tg=True)
            time.sleep(5)

# ================= START =================

if __name__ == "__main__":
    run()