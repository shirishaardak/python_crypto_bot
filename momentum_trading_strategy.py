import os
import time
from datetime import datetime
from dotenv import load_dotenv
import traceback
import pandas as pd

from utils import TradingUtils

load_dotenv()

# ================= CONFIG =================

BOT_NAME = "momentum_trading_strategy"

SYMBOLS = ["BTCUSD","ETHUSD"]

DEFAULT_CONTRACTS = {"BTCUSD":100,"ETHUSD":100}
CONTRACT_SIZE = {"BTCUSD":0.001,"ETHUSD":0.01}

TAKER_FEE = 0.0005
STEP_PCT = 0.003   # 0.3%

EMA_PERIOD = 50
CHANGE_FILTER = 0.3
COOLDOWN_SEC = 30

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

    if steps <= pos["trail_step"] or steps < 1:
        return

    pos["trail_step"] = steps

    if pos["side"] == "long":
        pos["stop"] = pos["entry"] * (1 + (steps - 1) * STEP_PCT)
    else:
        pos["stop"] = pos["entry"] * (1 - (steps - 1) * STEP_PCT)

    utils.log(f"{symbol} TSL → {pos['stop']}", tg=True)

# ================= EXIT =================

def exit_trade(symbol, price, pos, state):

    now = datetime.now()

    pnl = (
        (price - pos["entry"]) if pos["side"] == "long"
        else (pos["entry"] - price)
    ) * CONTRACT_SIZE[symbol] * pos["qty"]

    fee = utils.commission(price, pos["qty"], symbol)
    net_pnl = pnl - fee

    try:
        account = utils.get_balance()
    except:
        account = {"balance": 0}

    utils.save_trade({
        "symbol": symbol,
        "side": pos["side"],
        "entry_time": pos["entry_time"],
        "exit_time": now,
        "entry_price": pos["entry"],
        "exit_price": price,
        "qty": pos["qty"],
        "net_pnl": round(net_pnl, 6),
        "balance": round(account.get("balance", 0), 2)
    })

    utils.log(
        f"{symbol} EXIT | Side: {pos['side']} | Entry: {pos['entry']} | Exit: {price} | PnL: {net_pnl}",
        tg=True
    )

    # ✅ FIX: FULL RESET AFTER EXIT
    state["position"] = None
    state["cooldown"] = time.time() + COOLDOWN_SEC
    state["confirm"] = None
    state["last_price"] = price

    set_levels(state, price)

# ================= CORE LOGIC =================

def process_symbol(symbol, state):

    price = utils.fetch_price(symbol)
    if price is None:
        return

    ticker = utils.safe_get(f"https://api.india.delta.exchange/v2/tickers/{symbol}")
    try:
        change = float(ticker["result"]["ltp_change_24h"])
    except:
        change = 0

    # FIRST TIME SETUP
    if state["UP"] is None:
        set_levels(state, price)
        state["last_price"] = price
        state["confirm"] = None   # ✅ safety reset
        return

    # COOLDOWN CHECK
    if time.time() < state["cooldown"]:
        return

    # ================= ENTRY =================
    if state["position"] is None:

        # -------- LONG --------
        if (
            state["last_price"] <= state["UP"] and price > state["UP"] and
            change > CHANGE_FILTER
        ):
            if state["confirm"] != "long":
                state["confirm"] = "long"
                return

            state["confirm"] = None

            state["position"] = {
                "side": "long",
                "entry": price,
                "stop": price * (1 - STEP_PCT),
                "qty": DEFAULT_CONTRACTS[symbol],
                "trail_step": 0,
                "entry_time": datetime.now()
            }

            utils.log(f"{symbol} BUY {price}", tg=True)

        # -------- SHORT --------
        elif (
            state["last_price"] >= state["DOWN"] and price < state["DOWN"] and
            change < -CHANGE_FILTER
        ):
            if state["confirm"] != "short":
                state["confirm"] = "short"
                return

            state["confirm"] = None

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
            "last_price":None,
            "cooldown":0,
            "confirm":None
        }
        for s in SYMBOLS
    }

    utils.log("🚀 PRO BREAKOUT BOT STARTED", tg=True)

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