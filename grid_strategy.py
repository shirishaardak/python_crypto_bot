import os
import time
from datetime import datetime
import pytz
import pandas as pd
from dotenv import load_dotenv

from utils import TradingUtils

load_dotenv()

# ================= CONFIG =================

BOT_NAME = "grid_strategy"

SYMBOLS = ["BTCUSD"]

CONTRACT_SIZE = {"BTCUSD": 0.001}

GRID_QTY = {"BTCUSD": 100}

MAX_GRIDS = 3

TAKER_FEE = 0.0005

TIMEFRAME = "1d"
DAYS = 20

SLEEP_TIME = 2

TRADING_START = 2
TRADING_END = 13

RESET_HOUR = 2

MAX_DRAWDOWN = 0.04

ATR_PERIOD = 14

IST = pytz.timezone("Asia/Kolkata")


# ================= UTILS =================

def get_ist_time():
    return datetime.now(IST)


def can_enter_trade(now):
    return TRADING_START <= now.hour < TRADING_END


def calculate_atr(df, period=ATR_PERIOD):
    df = df.copy()

    df["tr"] = pd.concat([
        df["high"] - df["low"],
        (df["high"] - df["close"].shift()).abs(),
        (df["low"] - df["close"].shift()).abs()
    ], axis=1).max(axis=1)

    return df["tr"].rolling(period).mean().iloc[-1]


def should_reset(now, sym, today):
    if not sym["initialized"]:
        sym["initialized"] = True
        return True

    if now.hour == RESET_HOUR and sym["last_day"] != today:
        return True

    return False


# ================= TRADING ENGINE =================

utils = TradingUtils(
    contract_size=CONTRACT_SIZE,
    taker_fee=TAKER_FEE,
    timeframe=TIMEFRAME,
    days=DAYS,
    telegram_token=os.getenv("price_trend_strategy_bot"),
    telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID"),
    bot_name=BOT_NAME
)


def process_symbol(symbol, df, price, state):

    sym = state["symbols"][symbol]
    now = get_ist_time()

    if df is None or len(df) < 20:
        return

    today = now.date()

    # ================= RESET =================
    if should_reset(now, sym, today):
        sym["base"] = price
        sym["positions"] = []
        sym["last_day"] = today
        utils.log(f"🔄 RESET {symbol} base -> {round(price,2)}", tg=True)

    if sym["base"] is None:
        return

    base = sym["base"]
    positions = sym["positions"]
    last_price = sym.get("last_price")

    # ================= ATR DYNAMIC SETTINGS =================
    atr = calculate_atr(df)

    grid_size = atr * 0.8
    sl_distance = atr * 1.2
    tp_distance = atr * 1.0

    qty = GRID_QTY[symbol]

    # ================= GRID LEVELS =================
    buy_levels = [base - i * grid_size for i in range(1, MAX_GRIDS + 1)]
    sell_levels = [base + i * grid_size for i in range(1, MAX_GRIDS + 1)]

    # ================= ENTRY =================
    if last_price and can_enter_trade(now):

        # LONG GRID
        for level in buy_levels:
            if last_price > level >= price:
                if not any(p["grid"] == level and p["side"] == "long" for p in positions):

                    positions.append({
                        "side": "long",
                        "entry": price,
                        "grid": level,
                        "qty": qty,
                        "sl": price - sl_distance,
                        "tp": price + tp_distance,
                        "time": now
                    })

                    utils.log(f"🟢 LONG {symbol} @ {round(price,2)}", tg=True)

        # SHORT GRID
        for level in sell_levels:
            if last_price < level <= price:
                if not any(p["grid"] == level and p["side"] == "short" for p in positions):

                    positions.append({
                        "side": "short",
                        "entry": price,
                        "grid": level,
                        "qty": qty,
                        "sl": price + sl_distance,
                        "tp": price - tp_distance,
                        "time": now
                    })

                    utils.log(f"🔴 SHORT {symbol} @ {round(price,2)}", tg=True)

    # ================= TRAILING STOP (SMART) =================
    for p in positions:

        if p["side"] == "long":
            profit = price - p["entry"]

            if profit > atr:
                new_sl = price - atr * 0.6
                if new_sl > p["sl"]:
                    p["sl"] = new_sl

        else:
            profit = p["entry"] - price

            if profit > atr:
                new_sl = price + atr * 0.6
                if new_sl < p["sl"]:
                    p["sl"] = new_sl

    # ================= EXIT LOGIC =================
    for p in positions[:]:

        exit_price = None

        # TAKE PROFIT
        if p["side"] == "long" and price >= p["tp"]:
            exit_price = p["tp"]

        elif p["side"] == "short" and price <= p["tp"]:
            exit_price = p["tp"]

        # STOP LOSS
        elif p["side"] == "long" and price <= p["sl"]:
            exit_price = p["sl"]

        elif p["side"] == "short" and price >= p["sl"]:
            exit_price = p["sl"]

        if exit_price is None:
            continue

        pnl = (
            (exit_price - p["entry"]) * CONTRACT_SIZE[symbol] * p["qty"]
            if p["side"] == "long"
            else (p["entry"] - exit_price) * CONTRACT_SIZE[symbol] * p["qty"]
        )

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

    # ================= DRAWDOWN CONTROL =================
    floating = sum(
        (price - p["entry"]) * CONTRACT_SIZE[symbol] * p["qty"]
        if p["side"] == "long"
        else (p["entry"] - price) * CONTRACT_SIZE[symbol] * p["qty"]
        for p in positions
    )

    if floating < -state["balance"] * MAX_DRAWDOWN:
        utils.log(f"🚨 MAX DRAWDOWN HIT {symbol} - CLEAR POSITIONS", tg=True)
        positions.clear()

    sym["last_price"] = price


# ================= MAIN LOOP =================

def run():

    state = {
        "balance": 10000,
        "symbols": {
            s: {
                "positions": [],
                "base": None,
                "last_day": None,
                "initialized": False,
                "last_price": None
            } for s in SYMBOLS
        }
    }

    utils.log("🚀 ADAPTIVE HEDGED GRID BOT STARTED", tg=True)

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