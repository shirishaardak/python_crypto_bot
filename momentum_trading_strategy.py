import os
import time
from datetime import datetime
from dotenv import load_dotenv
import traceback

from utils import TradingUtils

load_dotenv()

# ================= CONFIG =================

BOT_NAME = "momentum_trading_strategy"

SYMBOLS = ["BTCUSD","ETHUSD"]

DEFAULT_CONTRACTS = {"BTCUSD":100,"ETHUSD":100}
CONTRACT_SIZE = {"BTCUSD":0.001,"ETHUSD":0.01}

TAKER_FEE = 0.0005
STEP_PCT = 0.005   # 0.5%

# ================= INIT =================

utils = TradingUtils(
    contract_size=CONTRACT_SIZE,
    taker_fee=TAKER_FEE,
    timeframe="1m",
    days=1,
    telegram_token=os.getenv("testing_strategy_my_aglo_bot"),
    telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID"),
    bot_name=BOT_NAME
)

# ================= LEVEL SET =================

def set_levels(state, price):
    state["UP"] = price * (1 + STEP_PCT)
    state["DOWN"] = price * (1 - STEP_PCT)

# ================= TRAILING SL =================

def update_trailing_sl(symbol, price, pos):

    move_pct = abs(price - pos["entry"]) / pos["entry"]
    steps = int(move_pct // STEP_PCT)

    if steps <= pos["trail_step"]:
        return

    pos["trail_step"] = steps

    if pos["side"] == "long":
        pos["stop"] = pos["entry"] * (1 + (steps - 1) * STEP_PCT)
    else:
        pos["stop"] = pos["entry"] * (1 - (steps - 1) * STEP_PCT)

    utils.log(f"{symbol} TSL → {pos['stop']}", tg=True)

# ================= EXIT =================

def exit_trade(symbol, price, pos, state):

    pnl = (
        (price - pos["entry"]) if pos["side"] == "long"
        else (pos["entry"] - price)
    ) * CONTRACT_SIZE[symbol] * pos["qty"]

    net = pnl - utils.commission(price, pos["qty"], symbol)

    # ✅ YOUR SAVE TRADE ADDED HERE
    utils.save_trade({
        "symbol": symbol,
        "side": pos["side"],
        "entry_time": pos["entry_time"],
        "exit_time": datetime.now(),
        "entry_price": pos["entry"],
        "exit_price": price,
        "qty": pos["qty"],
        "net_pnl": round(net, 6)
    })

    utils.log(f"{symbol} EXIT {net}", tg=True)

    state["position"] = None

    # RESET LEVELS AFTER EXIT
    set_levels(state, price)

# ================= CORE LOGIC =================

def process_symbol(symbol, state):

    price = utils.fetch_price(symbol)
    if price is None:
        return

    # FIRST TIME SETUP
    if state["UP"] is None:
        set_levels(state, price)
        state["last_price"] = price
        return

    # ================= ENTRY =================
    if state["position"] is None:

        # BUY
        if state["last_price"] <= state["UP"] and price > state["UP"]:
            state["position"] = {
                "side": "long",
                "entry": price,
                "stop": price * (1 - STEP_PCT),
                "qty": DEFAULT_CONTRACTS[symbol],
                "trail_step": 0,
                "entry_time": datetime.now()
            }

            utils.log(f"{symbol} BUY {price}", tg=True)

        # SELL
        elif state["last_price"] >= state["DOWN"] and price < state["DOWN"]:
            state["position"] = {
                "side": "short",
                "entry": price,
                "stop": price * (1 + STEP_PCT),
                "qty": DEFAULT_CONTRACTS[symbol],
                "trail_step": 0,
                "entry_time": datetime.now()
            }

            utils.log(f"{symbol} SELL {price}", tg=True)

    # ================= EXIT + TSL =================
    else:
        pos = state["position"]

        update_trailing_sl(symbol, price, pos)

        if pos["side"] == "long" and price <= pos["stop"]:
            exit_trade(symbol, price, pos, state)

        elif pos["side"] == "short" and price >= pos["stop"]:
            exit_trade(symbol, price, pos, state)

    state["last_price"] = price

# ================= MAIN =================

def run():

    state = {
        s:{
            "position":None,
            "UP":None,
            "DOWN":None,
            "last_price":None
        }
        for s in SYMBOLS
    }

    utils.log("🚀 0.5% BREAKOUT BOT STARTED", tg=True)

    while True:
        try:
            for symbol in SYMBOLS:
                process_symbol(symbol, state[symbol])

            time.sleep(2)

        except Exception:
            utils.log(traceback.format_exc(), tg=True)
            time.sleep(5)

if __name__ == "__main__":
    run()