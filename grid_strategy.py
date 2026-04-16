import os
import time
from datetime import datetime
from dotenv import load_dotenv

from utils import TradingUtils

load_dotenv()

# ================= CONFIG =================

BOT_NAME = "grid_strategy_final"

SYMBOLS = ["BTCUSD", "ETHUSD"]

CONTRACT_SIZE = {"BTCUSD": 0.001, "ETHUSD": 0.01}

GRID_SIZE = {
    "BTCUSD": 200,
    "ETHUSD": 10
}

GRID_QTY = {
    "BTCUSD": 100,
    "ETHUSD": 100
}

MAX_GRIDS = 5
TAKER_FEE = 0.0005

TIMEFRAME = "1d"
DAYS = 15

SLEEP_TIME = 3

# ===== LOW VOL TIME WINDOW (IST) =====
LOW_VOL_START = 2   # 2 AM
LOW_VOL_END = 13    # 1 PM

# ================= INIT =================

utils = TradingUtils(
    contract_size=CONTRACT_SIZE,
    taker_fee=TAKER_FEE,
    timeframe=TIMEFRAME,
    days=DAYS,
    telegram_token=os.getenv("price_trend_strategy_bot"),
    telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID"),
    bot_name=BOT_NAME
)

# ================= HELPERS =================

def is_low_vol_time():
    hour = datetime.now().hour
    return LOW_VOL_START <= hour < LOW_VOL_END

# ================= STRATEGY =================

def process_symbol(symbol, df, price, state):

    sym_state = state["symbols"][symbol]
    now = datetime.now()

    if df is None or len(df) < 3:
        return

    prev_close = df.iloc[-2]["Close"]
    current_day = now.date()

    # ================= DAILY RESET =================
    if sym_state.get("last_base_update_day") != current_day:
        sym_state["base_price"] = prev_close
        sym_state["last_base_update_day"] = current_day
        sym_state["last_logged_base"] = None

    base = sym_state["base_price"]

    # ================= PRINT BASE =================
    if sym_state.get("last_logged_base") != base:
        print(f"{symbol} Base Price: {round(base,2)}")
        sym_state["last_logged_base"] = base

    grid_gap = GRID_SIZE[symbol]
    qty = GRID_QTY[symbol]

    positions = sym_state["positions"]

    # ================= GRID LEVELS =================
    buy_levels = [base - i * grid_gap for i in range(1, MAX_GRIDS + 1)]
    sell_levels = [base + i * grid_gap for i in range(1, MAX_GRIDS + 1)]

    allow_entry = is_low_vol_time()

    # ================= LONG ENTRY =================
    if allow_entry:
        for level in buy_levels:

            already = any(p["entry"] == level and p["side"] == "long" for p in positions)

            if price <= level and not already:
                positions.append({
                    "side": "long",
                    "entry": level,
                    "qty": qty,
                    "time": now
                })

                utils.log(f"🟢 BUY {symbol} @ {level}", tg=True)

    # ================= SHORT ENTRY =================
    if allow_entry:
        for level in sell_levels:

            already = any(p["entry"] == level and p["side"] == "short" for p in positions)

            if price >= level and not already:
                positions.append({
                    "side": "short",
                    "entry": level,
                    "qty": qty,
                    "time": now
                })

                utils.log(f"🔴 SHORT {symbol} @ {level}", tg=True)

    # ================= EXIT (FIXED) =================
    for pos in positions[:]:

        exit_trade = False

        if pos["side"] == "long":
            target = pos["entry"] + grid_gap

            if price >= target:
                pnl = (target - pos["entry"]) * CONTRACT_SIZE[symbol] * pos["qty"]
                exit_trade = True

        elif pos["side"] == "short":
            target = pos["entry"] - grid_gap

            if price <= target:
                pnl = (pos["entry"] - target) * CONTRACT_SIZE[symbol] * pos["qty"]
                exit_trade = True

        # 🚨 Skip if no exit condition met
        if not exit_trade:
            continue

        # Fees
        entry_fee = utils.commission(pos["entry"], pos["qty"], symbol)
        exit_fee = utils.commission(target, pos["qty"], symbol)

        net = pnl - (entry_fee + exit_fee)

        state["balance"] += net

        utils.save_trade({
            "symbol": symbol,
            "side": pos["side"],
            "entry_price": pos["entry"],
            "exit_price": target,
            "qty": pos["qty"],
            "net_pnl": round(net, 6),
            "entry_time": pos["time"],
            "exit_time": now
        })

        utils.log(f"💰 EXIT {pos['side']} {symbol} @ {target} | PNL: {round(net,6)}", tg=True)
        utils.log(f"💰 Balance: {round(state['balance'],2)}", tg=True)

        positions.remove(pos)

    # ================= HYBRID RESET =================
    if len(positions) == 0:

        if abs(price - base) > grid_gap:

            sym_state["base_price"] = price
            sym_state["last_logged_base"] = None

            utils.log(f"🔄 BASE RESET (ALL EXIT) {symbol} -> {round(price,2)}", tg=True)

# ================= MAIN =================

def run():

    state = {
        "balance": 10000,
        "symbols": {
            s: {
                "positions": [],
                "base_price": None,
                "last_base_update_day": None,
                "last_logged_base": None
            } for s in SYMBOLS
        }
    }

    utils.log("🚀 FINAL GRID BOT STARTED", tg=True)

    while True:
        try:
            for symbol in SYMBOLS:

                df = utils.fetch_candles(symbol)
                if df is None or len(df) < 10:
                    continue

                price = utils.fetch_price(symbol)
                if price is None:
                    continue

                process_symbol(symbol, df, price, state)

            time.sleep(SLEEP_TIME)

        except Exception as e:
            utils.log(f"🚨 Error: {e}", tg=True)
            time.sleep(5)

# ================= START =================

if __name__ == "__main__":
    run()