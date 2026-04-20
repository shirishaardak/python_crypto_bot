import os
import time
from datetime import datetime
import pytz
import ta

from dotenv import load_dotenv
from utils import TradingUtils

load_dotenv()

# ================= CONFIG =================

BOT_NAME = "trend_following_strategy"

SYMBOLS = ["BTCUSD", "ETHUSD"]

CONTRACT_SIZE = {"BTCUSD": 0.001, "ETHUSD": 0.01}

GRID_SIZE = {
    "BTCUSD": 200,
    "ETHUSD": 30
}

GRID_QTY = {
    "BTCUSD": 100,
    "ETHUSD": 100
}

MAX_GRIDS = 3
TAKER_FEE = 0.0005

TIMEFRAME = "1d"
DAYS = 15

SLEEP_TIME = 2

# ===== RESET TIME =====
RESET_HOUR = 2   # 2 AM IST

# ===== RISK =====
STOP_LOSS_MULTIPLIER = 0.7
MAX_DRAWDOWN = 0.05
TREND_BREAK_MULTIPLIER = 4

# ===== ADX =====
ADX_THRESHOLD = 20

# ================= TIMEZONE =================

IST = pytz.timezone("Asia/Kolkata")

def get_ist_time():
    return datetime.now(IST)

# ================= INDICATORS =================

def add_indicators(df):
    adx_indicator = ta.trend.ADXIndicator(
        high=df["High"],
        low=df["Low"],
        close=df["Close"],
        window=14
    )

    df["adx"] = adx_indicator.adx()
    df["+di"] = adx_indicator.adx_pos()
    df["-di"] = adx_indicator.adx_neg()

    return df

# ================= INIT =================

utils = TradingUtils(
    contract_size=CONTRACT_SIZE,
    taker_fee=TAKER_FEE,
    timeframe=TIMEFRAME,
    days=DAYS,
    telegram_token=os.getenv("trend_following_strategy_bot"),
    telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID"),
    bot_name=BOT_NAME
)

# ================= STRATEGY =================

def process_symbol(symbol, df, price, state):

    sym = state["symbols"][symbol]
    now = get_ist_time()
    today = now.date()

    if df is None or len(df) < 20:
        return

    df = add_indicators(df)

    # ================= RESET LOGIC =================

    # 🟢 FIRST RUN RESET
    if not sym["initialized"]:
        sym["initialized"] = True
        sym["last_day"] = today

        sym["base"] = round(price, 2)
        sym["logged"] = False

        utils.log(f"🟢 FIRST RESET {symbol} base -> {round(price,2)}", tg=True)

    # ⏰ DAILY 2 AM RESET
    elif (
        now.hour == RESET_HOUR and
        now.minute < 5 and
        sym["last_day"] != today
    ):
        sym["last_day"] = today

        sym["base"] = round(price, 2)
        sym["logged"] = False

        utils.log(f"🔄 2AM RESET {symbol} base -> {round(price,2)}", tg=True)

    base = sym["base"]

    if not sym["logged"]:
        print(f"{symbol} Base Price: {round(base,2)}")
        sym["logged"] = True

    gap = GRID_SIZE[symbol]
    qty = GRID_QTY[symbol]
    positions = sym["positions"]
    last_price = sym.get("last_price")

    # ===== ADX FILTER =====
    adx = df.iloc[-1]["adx"]
    prev_adx = df.iloc[-2]["adx"]
    plus_di = df.iloc[-1]["+di"]
    minus_di = df.iloc[-1]["-di"]

    adx_up = adx > prev_adx and adx > ADX_THRESHOLD
    bullish = plus_di > minus_di
    bearish = minus_di > plus_di

    allow_entry = abs(price - base) <= gap * TREND_BREAK_MULTIPLIER

    # ===== LEVELS =====
    buy_levels = [round(base - i * gap, 2) for i in range(1, MAX_GRIDS + 1)]
    sell_levels = [round(base + i * gap, 2) for i in range(1, MAX_GRIDS + 1)]

    # ================= ENTRY =================
    if last_price is not None and allow_entry and adx_up:

        # BUY
        if bullish:
            for level in sell_levels:
                if last_price < level <= price and not any(
                    p["grid"] == level and p["side"] == "long" for p in positions
                ):
                    positions.append({
                        "side": "long",
                        "entry": price,
                        "grid": level,
                        "qty": qty,
                        "sl": price - gap * STOP_LOSS_MULTIPLIER,
                        "time": now
                    })
                    utils.log(f"🚀 BUY {symbol} @ {round(price,2)}", tg=True)

        # SHORT
        if bearish:
            for level in buy_levels:
                if last_price > level >= price and not any(
                    p["grid"] == level and p["side"] == "short" for p in positions
                ):
                    positions.append({
                        "side": "short",
                        "entry": price,
                        "grid": level,
                        "qty": qty,
                        "sl": price + gap * STOP_LOSS_MULTIPLIER,
                        "time": now
                    })
                    utils.log(f"⚡ SHORT {symbol} @ {round(price,2)}", tg=True)

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

    # ================= EXIT =================
    for p in positions[:]:

        exit_trade = False

        if p["side"] == "long":
            if price >= p["entry"] + gap or price <= p["sl"]:
                pnl = (price - p["entry"]) * CONTRACT_SIZE[symbol] * p["qty"]
                exit_trade = True

        else:
            if price <= p["entry"] - gap or price >= p["sl"]:
                pnl = (p["entry"] - price) * CONTRACT_SIZE[symbol] * p["qty"]
                exit_trade = True

        if not exit_trade:
            continue

        fee = utils.commission(p["entry"], p["qty"], symbol) + \
              utils.commission(price, p["qty"], symbol)

        net = pnl - fee
        state["balance"] += net

        utils.save_trade({
            "symbol": symbol,
            "side": p["side"],
            "entry_price": p["entry"],
            "exit_price": price,
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

    utils.log("🚀 BOT STARTED (FIRST RUN + 2AM RESET, LIVE PRICE)", tg=True)

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