import os
import time
from datetime import datetime
import pytz
from dotenv import load_dotenv

from utils import TradingUtils

load_dotenv()

# ================= CONFIG =================

BOT_NAME = "grid_strategy_real"

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

SLEEP_TIME = 2

# ===== TRADING WINDOW (IST) =====
TRADING_START = 2   # 2 AM
TRADING_END = 13    # 1 PM

# ===== RISK =====
STOP_LOSS_MULTIPLIER = 1.5
MAX_DRAWDOWN = 0.05
TREND_BREAK_MULTIPLIER = 4

# ================= TIMEZONE =================

IST = pytz.timezone("Asia/Kolkata")

def get_ist_time():
    return datetime.now(IST)

def get_ist_hour():
    return get_ist_time().hour

def can_enter_trade():
    return TRADING_START <= get_ist_hour() < TRADING_END

def can_reset_base(now):
    # Reset only once between 2:00–2:05 AM
    return now.hour == TRADING_START and now.minute < 5

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

    sym = state["symbols"][symbol]
    now = get_ist_time()

    if df is None or len(df) < 3:
        return

    prev_close = df.iloc[-2]["Close"]
    today = now.date()

    # ===== DAILY RESET (STRICT 2 AM) =====
    if can_reset_base(now) and sym["last_day"] != today:
        sym["base"] = prev_close
        sym["last_day"] = today
        sym["logged"] = False
        utils.log(f"🔄 RESET {symbol} base -> {round(prev_close,2)}", tg=True)

    if sym["base"] is None:
        return

    base = sym["base"]

    if not sym["logged"]:
        print(f"{symbol} Base Price: {round(base,2)}")
        sym["logged"] = True

    gap = GRID_SIZE[symbol]
    qty = GRID_QTY[symbol]
    positions = sym["positions"]
    last_price = sym.get("last_price")

    # ===== TREND FILTER =====
    allow_entry = abs(price - base) <= gap * TREND_BREAK_MULTIPLIER

    # ===== LEVELS =====
    buy_levels = [round(base - i * gap, 2) for i in range(1, MAX_GRIDS + 1)]
    sell_levels = [round(base + i * gap, 2) for i in range(1, MAX_GRIDS + 1)]

    # ================= ENTRY (ONLY DURING WINDOW) =================
    if last_price is not None and allow_entry and can_enter_trade():

        # ===== LONG =====
        for level in buy_levels:
            if last_price > level >= price and not any(
                p["grid"] == level and p["side"] == "long" for p in positions
            ):
                entry = price

                positions.append({
                    "side": "long",
                    "entry": entry,
                    "grid": level,
                    "qty": qty,
                    "sl": entry - gap * STOP_LOSS_MULTIPLIER,
                    "time": now
                })

                utils.log(f"🟢 BUY {symbol} @ {round(entry,2)}", tg=True)

        # ===== SHORT =====
        for level in sell_levels:
            if last_price < level <= price and not any(
                p["grid"] == level and p["side"] == "short" for p in positions
            ):
                entry = price

                positions.append({
                    "side": "short",
                    "entry": entry,
                    "grid": level,
                    "qty": qty,
                    "sl": entry + gap * STOP_LOSS_MULTIPLIER,
                    "time": now
                })

                utils.log(f"🔴 SHORT {symbol} @ {round(entry,2)}", tg=True)

    # ================= DRAWDOWN =================

    floating = 0

    for p in positions:
        if p["side"] == "long":
            floating += (price - p["entry"]) * CONTRACT_SIZE[symbol] * p["qty"]
        else:
            floating += (p["entry"] - price) * CONTRACT_SIZE[symbol] * p["qty"]

    if floating < -state["balance"] * MAX_DRAWDOWN:
        utils.log(f"🚨 MAX DRAWDOWN {symbol} - CLOSE ALL", tg=True)
        positions.clear()
        return

    # ================= EXIT (ALWAYS ACTIVE) =================

    for p in positions[:]:

        exit_trade = False

        if p["side"] == "long":
            tp = p["entry"] + gap

            if price >= tp:
                exit_price = price
                pnl = (exit_price - p["entry"]) * CONTRACT_SIZE[symbol] * p["qty"]
                exit_trade = True

            elif price <= p["sl"]:
                exit_price = price
                pnl = (exit_price - p["entry"]) * CONTRACT_SIZE[symbol] * p["qty"]
                exit_trade = True
                utils.log(f"🛑 SL LONG {symbol}", tg=True)

        else:
            tp = p["entry"] - gap

            if price <= tp:
                exit_price = price
                pnl = (p["entry"] - exit_price) * CONTRACT_SIZE[symbol] * p["qty"]
                exit_trade = True

            elif price >= p["sl"]:
                exit_price = price
                pnl = (p["entry"] - exit_price) * CONTRACT_SIZE[symbol] * p["qty"]
                exit_trade = True
                utils.log(f"🛑 SL SHORT {symbol}", tg=True)

        if not exit_trade:
            continue

        fee = utils.commission(p["entry"], p["qty"], symbol) + \
              utils.commission(exit_price, p["qty"], symbol)

        net = pnl - fee
        state["balance"] += net

        utils.save_trade({
            "symbol": symbol,
            "side": p["side"],
            "entry_price": p["entry"],
            "exit_price": exit_price,
            "qty": p["qty"],
            "net_pnl": round(net, 6),
            "entry_time": p["time"],
            "exit_time": now
        })

        utils.log(f"💰 EXIT {symbol} {p['side']} PNL: {round(net,6)}", tg=True)
        utils.log(f"💰 Balance: {round(state['balance'],2)}", tg=True)

        positions.remove(p)

    # ===== SAVE PRICE =====
    sym["last_price"] = price


# ================= MAIN =================

def run():

    state = {
        "balance": 10000,
        "symbols": {
            s: {
                "positions": [],
                "base": None,
                "last_day": None,
                "logged": False,
                "last_price": None
            } for s in SYMBOLS
        }
    }

    utils.log("🚀 REAL GRID BOT STARTED (FIXED WINDOW LOGIC)", tg=True)

    while True:
        try:
            for symbol in SYMBOLS:

                df = utils.fetch_candles(symbol)
                price = utils.fetch_price(symbol)

                if df is None or price is None:
                    continue

                process_symbol(symbol, df, price, state)

            time.sleep(SLEEP_TIME)

        except Exception as e:
            utils.log(f"🚨 ERROR: {e}", tg=True)
            time.sleep(5)


# ================= START =================

if __name__ == "__main__":
    run()