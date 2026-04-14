import os
import time
from datetime import datetime
from dotenv import load_dotenv
import subprocess

from utils import TradingUtils

load_dotenv()

# ================= CONFIG =================

BOT_NAME = "momentum_reversal_dynamic_step"

SYMBOLS = ["BTCUSD", "ETHUSD"]

DEFAULT_CONTRACTS = {"BTCUSD": 100, "ETHUSD": 100}
CONTRACT_SIZE = {"BTCUSD": 0.001, "ETHUSD": 0.01}
TAKER_FEE = 0.0005

MIN_BALANCE = 500

REVERSAL = 0.5   # % reversal exit
USE_HARD_SL = True

MIN_STEP = 50    # minimum step to avoid noise

last_git_push = time.time()



# ================= INIT =================

utils = TradingUtils(
    contract_size=CONTRACT_SIZE,
    taker_fee=TAKER_FEE,
    timeframe="1h",
    days=1,
    telegram_token=os.getenv("testing_strategy_my_aglo_bot"),
    telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID"),
    bot_name=BOT_NAME
)

# ================= STRATEGY =================

def process_symbol(symbol, price, state):

    now = datetime.now()
    pos = state["position"]

    # ===== DYNAMIC STEP (0.2%) =====
    step = max(price * 0.002, MIN_STEP)
    step = round(step, 2)

    buffer = step * 0.2

    # ===== BASE PRICE =====
    if state.get("base_price") is None:
        state["base_price"] = round(price / step) * step
        state["levels_logged"] = False

    base = state["base_price"]

    up_level = base + step
    down_level = base - step

    if not state.get("levels_logged"):
        utils.log(f"📊 BASE {symbol}: {base} | UP: {up_level} | DOWN: {down_level}", tg=True)
        state["levels_logged"] = True

    last_price = state.get("last_price")

    if last_price is None:
        state["last_price"] = price
        return

    # ===== MOMENTUM =====
    momentum_up = price > last_price + (step * 0.1)
    momentum_down = price < last_price - (step * 0.1)

    min_move = abs(price - last_price) > (step * 0.2)

    # ===== COOLDOWN =====
    if time.time() - state.get("cooldown", 0) < 300:
        state["last_price"] = price
        return

    # ================= EXIT =================
    if pos:

        exit_trade = False
        pnl = 0

        # ===== HARD SL =====
        if USE_HARD_SL:
            if pos["side"] == "long" and price <= pos["sl"]:
                pnl = (price - pos["entry"]) * CONTRACT_SIZE[symbol] * pos["qty"]
                exit_trade = True

            elif pos["side"] == "short" and price >= pos["sl"]:
                pnl = (pos["entry"] - price) * CONTRACT_SIZE[symbol] * pos["qty"]
                exit_trade = True

        # ===== REVERSAL EXIT =====
        if not exit_trade:

            if pos["side"] == "long":

                if price > pos["highest_price"]:
                    pos["highest_price"] = price

                drawdown = ((pos["highest_price"] - price) / pos["highest_price"]) * 100

                if drawdown >= REVERSAL:
                    pnl = (price - pos["entry"]) * CONTRACT_SIZE[symbol] * pos["qty"]
                    exit_trade = True

            elif pos["side"] == "short":

                if price < pos["lowest_price"]:
                    pos["lowest_price"] = price

                drawup = ((price - pos["lowest_price"]) / pos["lowest_price"]) * 100

                if drawup >= REVERSAL:
                    pnl = (pos["entry"] - price) * CONTRACT_SIZE[symbol] * pos["qty"]
                    exit_trade = True

        # ===== EXECUTE EXIT =====
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

            # RESET STATE
            state["position"] = None
            state["base_price"] = round(price / step) * step
            state["levels_logged"] = False
            state["cooldown"] = time.time()

            return

    # ================= ENTRY =================
    if not pos:

        if state["balance"] < MIN_BALANCE:
            state["last_price"] = price
            return

        # ===== LONG =====
        if price >= up_level + buffer and momentum_up and min_move:

            state["position"] = {
                "side": "long",
                "entry": price,
                "qty": DEFAULT_CONTRACTS[symbol],
                "entry_time": now,
                "sl": price - step,
                "highest_price": price
            }

            utils.log(f"🟢 LONG {symbol} @ {price}", tg=True)

        # ===== SHORT =====
        elif price <= down_level - buffer and momentum_down and min_move:

            state["position"] = {
                "side": "short",
                "entry": price,
                "qty": DEFAULT_CONTRACTS[symbol],
                "entry_time": now,
                "sl": price + step,
                "lowest_price": price
            }

            utils.log(f"🔴 SHORT {symbol} @ {price}", tg=True)

    state["last_price"] = price


# ================= MAIN =================

def run():

    state = {
        s: {
            "position": None,
            "balance": 5000,
            "last_price": None,
            "base_price": None,
            "levels_logged": False,
            "cooldown": 0
        } for s in SYMBOLS
    }

    utils.log("🚀 DYNAMIC STEP REVERSAL BOT STARTED", tg=True)

    while True:

        try:
            for symbol in SYMBOLS:

                price = utils.fetch_price(symbol)

                if price is None:
                    continue

                process_symbol(symbol, price, state[symbol])

            time.sleep(1)

        except Exception as e:
            utils.log(f"🚨 Error: {e}", tg=True)
            time.sleep(2)


# ================= START =================

if __name__ == "__main__":
    run()