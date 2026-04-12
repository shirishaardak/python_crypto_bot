import os
import time
import pandas as pd
import numpy as np
from datetime import datetime
from dotenv import load_dotenv
import subprocess

from utils import TradingUtils

load_dotenv()

# ================= CONFIG =================

BOT_NAME = "simple_momentum_bot"

SYMBOLS = ["BTCUSD"]

DEFAULT_CONTRACTS = {"BTCUSD": 50}
CONTRACT_SIZE = {"BTCUSD": 0.001}
TAKER_FEE = 0.0005

TIMEFRAME = "1h"
DAYS = 15

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

# ================= STRATEGY =================

def process_symbol(symbol, df, price, state, is_new_candle):

    pos = state["position"]
    now = datetime.now()

    step = 200 if symbol == "BTCUSD" else 20

    # ===== DYNAMIC LEVELS =====
    zone = int(np.floor(price / step))
    down_level = zone * step
    up_level = (zone + 1) * step

    if is_new_candle:
        utils.log(f"📊 {symbol} PRICE: {round(price)} | UP: {up_level} | DOWN: {down_level}", tg=True)

    # ===== SIMPLE MOMENTUM =====
    prev_close = df["close"].iloc[-2]
    momentum_up = price > prev_close
    momentum_down = price < prev_close

    # ================= EXIT =================
    if pos:

        exit_trade = False
        pnl = 0

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

            emoji = "🟢" if net > 0 else "🔴"
            utils.log(f"{emoji} EXIT {symbol} @ {price} | PNL: {round(net,6)}", tg=True)
            utils.log(f"💰 Balance: {round(state['balance'],2)}", tg=True)

            state["position"] = None

            # ✅ RESET ZONE AFTER EXIT (KEY FIX)
            state["last_traded_zone"] = None

            return

    # ================= ENTRY =================
    if not pos:

        if state["balance"] < MIN_BALANCE:
            return

        # Anti-chop
        if state.get("last_traded_zone") == zone:
            return

        # ===== LONG =====
        if price >= up_level and momentum_up:

            state["position"] = {
                "side": "long",
                "entry": price,
                "qty": DEFAULT_CONTRACTS[symbol],
                "entry_time": now,
                "sl": price - step
            }

            state["last_traded_zone"] = zone

            utils.log(f"🟢 LONG {symbol} @ {price} | SL: {price - step}", tg=True)

        # ===== SHORT =====
        elif price <= down_level and momentum_down:

            state["position"] = {
                "side": "short",
                "entry": price,
                "qty": DEFAULT_CONTRACTS[symbol],
                "entry_time": now,
                "sl": price + step
            }

            state["last_traded_zone"] = zone

            utils.log(f"🔴 SHORT {symbol} @ {price} | SL: {price + step}", tg=True)

# ================= MAIN =================

def run():

    state = {
        s: {
            "position": None,
            "last_candle_time": None,
            "balance": 5000,
            "last_traded_zone": None
        } for s in SYMBOLS
    }

    utils.log("🚀 SIMPLE MOMENTUM BOT STARTED", tg=True)

    while True:

        try:
            for symbol in SYMBOLS:

                df = utils.fetch_candles(symbol)

                if df is None or len(df) < 50:
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