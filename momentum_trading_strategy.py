import os
import time
from datetime import datetime
from dotenv import load_dotenv
import subprocess

from utils import TradingUtils

load_dotenv()

# ================= CONFIG =================

BOT_NAME = "momentum_trading_strategy"

SYMBOLS = ["BTCUSD"]

DEFAULT_CONTRACTS = {"BTCUSD": 50}
CONTRACT_SIZE = {"BTCUSD": 0.001}
TAKER_FEE = 0.0005

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

    step = 200 if symbol == "BTCUSD" else 20
    buffer = step * 0.2

    # ===== BASE PRICE SET =====
    if state.get("base_price") is None:
        state["base_price"] = round(price / step) * step
        state["levels_logged"] = False

    base = state["base_price"]

    # ===== LEVELS =====
    up_level = base + step
    down_level = base - step

    # ===== LOG LEVELS =====
    if not state.get("levels_logged"):
        utils.log(f"📊 BASE {symbol}: {base} | UP: {up_level} | DOWN: {down_level}", tg=True)
        state["levels_logged"] = True

    last_price = state.get("last_price")

    if last_price is None:
        state["last_price"] = price
        return

    # ===== IMPROVED MOMENTUM =====
    momentum_up = price > last_price + (step * 0.2)
    momentum_down = price < last_price - (step * 0.2)

    # ===== STRONGER MOVE FILTER =====
    min_move = abs(price - last_price) > (step * 0.3)

    # ===== COOLDOWN =====
    if time.time() - state.get("cooldown", 0) < 300:
        state["last_price"] = price
        return

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

        # TAKE PROFIT
        if pos["side"] == "long" and price >= pos["tp"]:
            pnl = (price - pos["entry"]) * CONTRACT_SIZE[symbol] * pos["qty"]
            exit_trade = True

        elif pos["side"] == "short" and price <= pos["tp"]:
            pnl = (pos["entry"] - price) * CONTRACT_SIZE[symbol] * pos["qty"]
            exit_trade = True

        # TRAILING SL (earlier)
        if pos["side"] == "long":
            move = price - pos["entry"]
            if move >= step * 0.5:
                pos["sl"] = max(pos["sl"], price - step * 0.5)

        elif pos["side"] == "short":
            move = pos["entry"] - price
            if move >= step * 0.5:
                pos["sl"] = min(pos["sl"], price + step * 0.5)

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

            # RESET BASE + COOLDOWN
            new_base = round(price / step) * step
            state["base_price"] = new_base
            state["levels_logged"] = False
            state["cooldown"] = time.time()

            utils.log(f"🔄 RESET BASE {symbol} → {new_base}", tg=True)

            state["last_price"] = price
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
                "tp": price + step
            }

            utils.log(f"🟢 LONG {symbol} @ {price} | SL: {price - step} | TP: {price + step}", tg=True)

        # ===== SHORT =====
        elif price <= down_level - buffer and momentum_down and min_move:

            state["position"] = {
                "side": "short",
                "entry": price,
                "qty": DEFAULT_CONTRACTS[symbol],
                "entry_time": now,
                "sl": price + step,
                "tp": price - step
            }

            utils.log(f"🔴 SHORT {symbol} @ {price} | SL: {price + step} | TP: {price - step}", tg=True)

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

    utils.log("🚀 IMPROVED MOMENTUM BOT STARTED", tg=True)

    while True:

        try:
            for symbol in SYMBOLS:

                price = utils.fetch_price(symbol)

                if price is None:
                    continue

                process_symbol(symbol, price, state[symbol])

            auto_git_push()
            time.sleep(1)

        except Exception as e:
            utils.log(f"🚨 Error: {e}", tg=True)
            time.sleep(2)


# ================= START =================

if __name__ == "__main__":
    run()