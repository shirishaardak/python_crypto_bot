import os
import time
from datetime import datetime
import pytz
from dotenv import load_dotenv

from utils import TradingUtils

load_dotenv()

# ================= CONFIG =================

BOT_NAME = "grid_strategy_real"

SYMBOLS = ["BTCUSD"]

CONTRACT_SIZE = {"BTCUSD": 0.001}

GRID_SIZE = {
    "BTCUSD": 200
}

GRID_QTY = {
    "BTCUSD": 100
}

MAX_GRIDS = 3
TAKER_FEE = 0.0005

TIMEFRAME = "1d"
DAYS = 15

SLEEP_TIME = 2

TRADING_START = 2
TRADING_END = 13

RESET_HOUR = 2

STOP_LOSS_MULTIPLIER = 1.5
MAX_DRAWDOWN = 0.05
TREND_BREAK_MULTIPLIER = 4

IST = pytz.timezone("Asia/Kolkata")


def get_ist_time():
    return datetime.now(IST)


def can_enter_trade(now):
    return TRADING_START <= now.hour < TRADING_END


def should_reset(now, sym, today):
    if not sym["initialized"]:
        sym["initialized"] = True
        return True

    if now.hour == RESET_HOUR and now.minute < 5 and sym["last_day"] != today:
        return True

    return False


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

    today = now.date()

    # ===== RESET =====
    if should_reset(now, sym, today):
        sym["base"] = price
        sym["last_day"] = today
        sym["logged"] = False
        utils.log(f"🔄 RESET {symbol} base -> {round(price,2)}", tg=True)

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

    allow_entry = abs(price - base) <= gap * TREND_BREAK_MULTIPLIER

    buy_levels = [round(base - i * gap, 2) for i in range(1, MAX_GRIDS + 1)]
    sell_levels = [round(base + i * gap, 2) for i in range(1, MAX_GRIDS + 1)]

    # ================= ENTRY =================
    if (
        last_price is not None
        and allow_entry
        and can_enter_trade(now)
        and len(positions) < MAX_GRIDS
    ):

        # LONG
        for level in buy_levels:
            if last_price > level >= price and not any(
                p["grid"] == level and p["side"] == "long" for p in positions
            ):
                entry = price

                positions.append({
                    "side": "long",
                    "entry": entry,
                    "grid": level,
                    "grid_index": buy_levels.index(level) + 1,
                    "qty": qty,
                    "sl": entry - gap * STOP_LOSS_MULTIPLIER,
                    "time": now
                })

                utils.log(f"🟢 BUY {symbol} @ {round(entry,2)}", tg=True)

        # SHORT
        for level in sell_levels:
            if last_price < level <= price and not any(
                p["grid"] == level and p["side"] == "short" for p in positions
            ):
                entry = price

                positions.append({
                    "side": "short",
                    "entry": entry,
                    "grid": level,
                    "grid_index": sell_levels.index(level) + 1,
                    "qty": qty,
                    "sl": entry + gap * STOP_LOSS_MULTIPLIER,
                    "time": now
                })

                utils.log(f"🔴 SHORT {symbol} @ {round(entry,2)}", tg=True)

    # ================= TRAILING SL =================
    for p in positions:

        entry_grid = p["grid_index"]

        if p["side"] == "long":

            current_grid = int((price - base) // gap)
            move = current_grid + entry_grid

            if move >= 2:
                new_sl = base + (move - 1 - entry_grid) * gap

                if new_sl > p["sl"]:
                    new_sl = min(new_sl, price - 1e-6)
                    p["sl"] = new_sl
                    utils.log(
                        f"🔼 TSL LONG {symbol} (G{entry_grid}) -> {round(new_sl,2)}",
                        tg=True
                    )

        else:

            current_grid = int((base - price) // gap)
            move = current_grid + entry_grid

            if move >= 2:
                new_sl = base - (move - 1 - entry_grid) * gap

                if new_sl < p["sl"]:
                    new_sl = max(new_sl, price + 1e-6)
                    p["sl"] = new_sl
                    utils.log(
                        f"🔽 TSL SHORT {symbol} (G{entry_grid}) -> {round(new_sl,2)}",
                        tg=True
                    )

    # ================= TAKE PROFIT LEVEL 4 =================
    tp_distance = GRID_SIZE[symbol] * 4

    if abs(price - base) >= tp_distance:
        utils.log(f"🎯 TP LEVEL 4 HIT {symbol} - CLOSE ALL", tg=True)
        positions.clear()
        sym["last_price"] = price
        return

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

    # ================= EXIT (ONLY SL) =================
    for p in positions[:]:

        if p["side"] == "long" and price <= p["sl"]:
            exit_price = price
            pnl = (exit_price - p["entry"]) * CONTRACT_SIZE[symbol] * p["qty"]

        elif p["side"] == "short" and price >= p["sl"]:
            exit_price = price
            pnl = (p["entry"] - exit_price) * CONTRACT_SIZE[symbol] * p["qty"]

        else:
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
                "last_price": None,
                "initialized": False
            } for s in SYMBOLS
        }
    }

    utils.log("🚀 GRID BOT STARTED (TSL + TP LEVEL 4 MODE)", tg=True)

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


if __name__ == "__main__":
    run()