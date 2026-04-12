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
STOPLOSS = {"BTCUSD": 200}
TAKER_FEE = 0.0005

TIMEFRAME = "1h"
DAYS = 15

MIN_BALANCE = 500
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

# ================= STRATEGY =================

def process_symbol(symbol, df, price, state, is_new_candle):

    pos = state["position"]
    now = datetime.now()

    # ================= LEVEL SETUP =================
    if symbol == "BTCUSD":
        step = 200
    else:
        step = 20

    # FIRST TIME SET
    if state.get("base_price") is None:
        state["base_price"] = round(price / step) * step
        state["levels_logged"] = False

    base_price = state["base_price"]

    # ✅ FIXED ZONE CALCULATION
    zone = int(np.floor((price - base_price) / step))

    up_level = base_price + (zone + 1) * step
    down_level = base_price + zone * step

    # ✅ LOG ONLY ON SET / RESET
    if not state.get("levels_logged"):
        utils.log(f"📊 {symbol} PRICE: {round(price)} | UP: {up_level} | DOWN: {down_level}", tg=True)
        state["levels_logged"] = True

    # ================= EXIT =================
    if pos:

        pnl = 0
        exit_trade = False

        # STOP LOSS
        if pos["side"] == "long" and price <= pos["sl"]:
            pnl = (price - pos["entry"]) * CONTRACT_SIZE[symbol] * pos["qty"]
            exit_trade = True

        elif pos["side"] == "short" and price >= pos["sl"]:
            pnl = (pos["entry"] - price) * CONTRACT_SIZE[symbol] * pos["qty"]
            exit_trade = True

        # TRAILING SL
        if pos["side"] == "long":
            move = price - pos["entry"]
            if move >= step:
                pos["sl"] = max(pos["sl"], price - step)

        elif pos["side"] == "short":
            move = pos["entry"] - price
            if move >= step:
                pos["sl"] = min(pos["sl"], price + step)

        if exit_trade:

            entry_fee = utils.commission(pos["entry"], pos["qty"], symbol)
            exit_fee  = utils.commission(price, pos["qty"], symbol)

            total_fee = entry_fee + exit_fee
            net = pnl - total_fee

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

            # ✅ RESET LEVELS
            state["base_price"] = round(price / step) * step
            state["last_traded_zone"] = None
            state["levels_logged"] = False

            return

    # ================= ENTRY =================
    if not pos:

        balance = state["balance"]

        if balance < MIN_BALANCE:
            utils.log(f"⚠️ Balance low: {balance}, required: {MIN_BALANCE}")
            return

        # ❌ ANTI-CHOP FILTER
        if state.get("last_traded_zone") == zone:
            return

        # BUY BREAKOUT
        if price >= up_level:

            state["position"] = {
                "side": "long",
                "entry": price,
                "qty": DEFAULT_CONTRACTS[symbol],
                "entry_time": now,
                "sl": price - step
            }

            state["last_traded_zone"] = zone

            utils.log(f"🟢 {symbol} LONG @ {price} | SL: {price - step}", tg=True)

        # SELL BREAKDOWN
        elif price <= down_level:

            state["position"] = {
                "side": "short",
                "entry": price,
                "qty": DEFAULT_CONTRACTS[symbol],
                "entry_time": now,
                "sl": price + step
            }

            state["last_traded_zone"] = zone

            utils.log(f"🔴 {symbol} SHORT @ {price} | SL: {price + step}", tg=True)

# ================= MAIN =================

def run():

    state = {
        s: {
            "position": None,
            "last_candle_time": None,
            "last_exit_candle": None,
            "balance": 5000,
            "base_price": None,
            "last_traded_zone": None,
            "levels_logged": False
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

            time.sleep(3)

        except Exception as e:
            utils.log(f"🚨 Runtime error: {e}", tg=True)
            time.sleep(5)

# ================= START =================

if __name__ == "__main__":
    run()