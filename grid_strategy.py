import os
import time
from datetime import datetime
import pytz
from dotenv import load_dotenv

from utils import TradingUtils

load_dotenv()

# ================= TIMEZONE =================
IST = pytz.timezone("Asia/Kolkata")

# ================= CONFIG =================

BOT_NAME = "grid_strategy"

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

MIN_BALANCE = 1000

SLEEP_TIME = 3

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

# ================= STRATEGY =================

def process_symbol(symbol, df, price, state):

    sym_state = state["symbols"][symbol]
    now = datetime.now(IST)

    if df is None or len(df) < 3:
        return

    prev_close = df.iloc[-2]["Close"]
    current_day = now.date()

    # ================= DAILY BASE RESET =================
    if sym_state.get("last_base_update_day") != current_day:
        sym_state["base_price"] = prev_close
        sym_state["last_base_update_day"] = current_day
        sym_state["last_logged_base"] = None  # force print

    base = sym_state["base_price"]

    # ================= PRINT ONLY ON RESET =================
    if sym_state.get("last_logged_base") != base:
        print(f"{symbol} Base Price Set: {round(base,2)}")
        sym_state["last_logged_base"] = base

    grid_gap = GRID_SIZE[symbol]
    qty = GRID_QTY[symbol]

    positions = sym_state["positions"]

    # ================= GRID LEVELS =================
    buy_levels = [base - i * grid_gap for i in range(1, MAX_GRIDS + 1)]

    # ================= BUY =================
    for level in buy_levels:

        already_bought = any(p["entry"] == level for p in positions)

        if price <= level and not already_bought:

            positions.append({
                "side": "long",
                "entry": level,
                "qty": qty,
                "time": now
            })

            utils.log(f"🟢 GRID BUY {symbol} @ {level} | Qty: {qty}", tg=True)

    # ================= SELL =================
    for pos in positions[:]:

        target = pos["entry"] + grid_gap

        if price >= target:

            pnl = (target - pos["entry"]) * CONTRACT_SIZE[symbol] * pos["qty"]

            entry_fee = utils.commission(pos["entry"], pos["qty"], symbol)
            exit_fee = utils.commission(target, pos["qty"], symbol)

            net = pnl - (entry_fee + exit_fee)

            state["balance"] += net

            utils.save_trade({
                "symbol": symbol,
                "side": "long",
                "entry_price": pos["entry"],
                "exit_price": target,
                "qty": pos["qty"],
                "net_pnl": round(net, 6),
                "entry_time": pos["time"],
                "exit_time": now
            })

            utils.log(f"💰 GRID SELL {symbol} @ {target} | PNL: {round(net,6)}", tg=True)
            utils.log(f"💰 Balance: {round(state['balance'],2)}", tg=True)

            positions.remove(pos)

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

    utils.log("🚀 GRID BOT STARTED (IST TIME)", tg=True)

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
            utils.log(f"🚨 Runtime error: {e}", tg=True)
            time.sleep(5)

# ================= START =================

if __name__ == "__main__":
    run()