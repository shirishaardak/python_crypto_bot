import os
import time
import pandas as pd
import numpy as np
from datetime import datetime
import pandas_ta as ta
from dotenv import load_dotenv
import traceback
import subprocess

from utils import TradingUtils

load_dotenv()

# ================= CONFIG =================

BOT_NAME = "price_trend_strategy"

SYMBOLS = ["BTCUSD"]

DEFAULT_CONTRACTS = {"BTCUSD": 50}
CONTRACT_SIZE = {"BTCUSD": 0.001}
STOPLOSS = {"BTCUSD": 500}
TAKER_FEE = 0.0005

TIMEFRAME = "1h"
DAYS = 15

MIN_BALANCE = 5000
BALANCE = 800

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

# ================= INIT UTILS =================

utils = TradingUtils(
    contract_size=CONTRACT_SIZE,
    taker_fee=TAKER_FEE,
    timeframe=TIMEFRAME,
    days=DAYS,
    telegram_token=os.getenv("price_trend_strategy_bot"),
    telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID"),
    bot_name=BOT_NAME
)

# ================= PAPER ORDER =================

def place_market_order(symbol, side, qty):
    utils.log(f"📝 PAPER ORDER → {symbol} {side.upper()} {qty}", tg=True)
    return {"success": True}

# ================= TRENDLINE =================

def calculate_trendline(df):

    ha = ta.ha(df["Open"], df["High"], df["Low"], df["Close"]).reset_index(drop=True)

    ha["atr"] = ta.atr(ha["HA_high"], ha["HA_low"], ha["HA_close"], length=14)
    ha["atr_ma"] = ha["atr"].rolling(14).mean()

    order = 21
    ha["UPPER"] = ha["HA_high"].rolling(order).max()
    ha["LOWER"] = ha["HA_low"].rolling(order).min()

    ha["Trendline"] = np.nan
    trend = ha.loc[0, "HA_close"]
    ha.loc[0, "Trendline"] = trend

    for i in range(1, len(ha)):

        if ha.loc[i, "HA_high"] == ha.loc[i, "UPPER"]:
            trend = ha.loc[i, "HA_low"]

        elif ha.loc[i, "HA_low"] == ha.loc[i, "LOWER"]:
            trend = ha.loc[i, "HA_high"]

        ha.loc[i, "Trendline"] = trend

    return ha

# ================= STRATEGY =================

def process_symbol(symbol, df, price, state, is_new_candle):

    ha = calculate_trendline(df)

    last = ha.iloc[-2]
    prev = ha.iloc[-3]

    pos = state["position"]
    now = datetime.now()

    # ================= EXIT =================
    if pos:

        exit_trade = False
        pnl = 0

        if pos["side"] == "long" and price < last.Trendline:

            pnl = (price - pos["entry"]) * CONTRACT_SIZE[symbol] * pos["qty"]
            exit_trade = True

        elif pos["side"] == "short" and price > last.Trendline:

            pnl = (pos["entry"] - price) * CONTRACT_SIZE[symbol] * pos["qty"]
            exit_trade = True

        if exit_trade:

            entry_fee = utils.commission(pos["entry"], pos["qty"], symbol)
            exit_fee  = utils.commission(price, pos["qty"], symbol)

            total_fee = entry_fee + exit_fee
            net = pnl - total_fee

            # ✅ UPDATE PAPER BALANCE
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

            emoji = "🟢" if net > 0 else "🔴"
            utils.log(f"{emoji} {symbol} EXIT @ {price} | PNL: {round(net,6)}", tg=True)
            utils.log(f"💰 Balance: {round(state['balance'],2)}", tg=True)

            state["position"] = None
            state["last_exit_candle"] = df.index[-1]

            return

    # ================= ENTRY =================
    if not pos and is_new_candle:

        if state.get("last_exit_candle") == df.index[-1]:
            return

        balance = state["balance"]

        if balance < BALANCE:
            utils.log(f"⚠️ Balance low: {balance}, required: {MIN_BALANCE}")
            return

        # LONG
        if last.HA_close > last.Trendline and last.HA_close > prev.HA_close and last.HA_close > prev.HA_open:

            # place_market_order(symbol, "buy", DEFAULT_CONTRACTS[symbol])

            state["position"] = {
                "side": "long",
                "entry": price,
                "qty": DEFAULT_CONTRACTS[symbol],
                "entry_time": now,
                "sl": price - STOPLOSS[symbol]
            }

            utils.log(f"🟢 {symbol} LONG @ {price} | SL: {price - STOPLOSS[symbol]}", tg=True)

        # SHORT
        elif last.HA_close < last.Trendline and last.HA_close < prev.HA_close and last.HA_close < prev.HA_open:

            # place_market_order(symbol, "sell", DEFAULT_CONTRACTS[symbol])

            state["position"] = {
                "side": "short",
                "entry": price,
                "qty": DEFAULT_CONTRACTS[symbol],
                "entry_time": now,
                "sl": price + STOPLOSS[symbol]
            }

            utils.log(f"🔴 {symbol} SHORT @ {price} | SL: {price + STOPLOSS[symbol]}", tg=True)

# ================= MAIN =================

def run():

    state = {
        s: {
            "position": None,
            "last_candle_time": None,
            "last_exit_candle": None,
            "balance": 5000   # 💰 PAPER START BALANCE
        } for s in SYMBOLS
    }

    utils.log("🚀 LIVE BOT STARTED (PAPER MODE)", tg=True)

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

                auto_git_push()

            time.sleep(3)

        except Exception as e:
            utils.log(f"🚨 Runtime error: {e}", tg=True)
            time.sleep(5)

# ================= START =================

if __name__ == "__main__":
    run()