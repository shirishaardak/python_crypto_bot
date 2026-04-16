import os
import time
from datetime import datetime
from dotenv import load_dotenv

from utils import TradingUtils

load_dotenv()

# ================= CONFIG =================

BOT_NAME = "price_breakout_strategy"

SYMBOLS = ["BTCUSD", "ETHUSD"]

CONTRACT_SIZE = {"BTCUSD": 0.001, "ETHUSD": 0.01}

RISK_PER_TRADE = 0.01  # 1%

REVERSAL = {"BTCUSD": 0.5, "ETHUSD": 1.0}

BUFFER = 0.005  # 0.5%

TAKER_FEE = 0.0005

MIN_BALANCE = 1000
COOLDOWN = 60

# ================= UTILS =================

# 1D (levels)
utils = TradingUtils(
    contract_size=CONTRACT_SIZE,
    taker_fee=TAKER_FEE,
    timeframe="1d",
    days=15,
    telegram_token=os.getenv("price_trend_strategy_bot"),
    telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID"),
    bot_name=BOT_NAME
)

# 15m (confirmation)
utils_15m = TradingUtils(
    contract_size=CONTRACT_SIZE,
    taker_fee=TAKER_FEE,
    timeframe="15m",
    days=2,
    telegram_token=os.getenv("price_trend_strategy_bot"),
    telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID"),
    bot_name=BOT_NAME
)

# ================= POSITION SIZING =================

def calculate_qty(symbol, price, balance):
    risk_amount = balance * RISK_PER_TRADE
    stop_distance = price * (REVERSAL[symbol] / 100)
    qty = risk_amount / (stop_distance * CONTRACT_SIZE[symbol])
    return max(1, int(qty))

# ================= STRATEGY =================

def process_symbol(symbol, df_1d, df_15m, price, state):

    sym_state = state["symbols"][symbol]
    pos = sym_state["position"]
    now = datetime.now()

    if df_1d is None or df_15m is None:
        return

    if len(df_1d) < 3 or len(df_15m) < 3:
        return

    prev_close = df_1d.iloc[-2]["Close"]

    prev_15m = df_15m.iloc[-3]["Close"]
    curr_15m = df_15m.iloc[-2]["Close"]

    # ================= LEVELS =================

    long_level = prev_close * (1 + BUFFER)
    short_level = prev_close * (1 - BUFFER)

    # ================= EXIT =================

    if pos:

        exit_trade = False
        pnl = 0

        if pos["side"] == "long":

            if price > pos["highest_price"]:
                pos["highest_price"] = price

            drawdown = ((pos["highest_price"] - price) / pos["highest_price"]) * 100

            if drawdown >= REVERSAL[symbol]:
                pnl = (price - pos["entry"]) * CONTRACT_SIZE[symbol] * pos["qty"]
                exit_trade = True

        elif pos["side"] == "short":

            if price < pos["lowest_price"]:
                pos["lowest_price"] = price

            drawup = ((price - pos["lowest_price"]) / pos["lowest_price"]) * 100

            if drawup >= REVERSAL[symbol]:
                pnl = (pos["entry"] - price) * CONTRACT_SIZE[symbol] * pos["qty"]
                exit_trade = True

        if exit_trade:

            entry_fee = utils.commission(pos["entry"], pos["qty"], symbol)
            exit_fee = utils.commission(price, pos["qty"], symbol)

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

            sym_state["position"] = None
            sym_state["last_exit_time"] = now

            utils.log(f"EXIT {symbol} | PNL: {round(net,6)}", tg=True)
            return

    # ================= ENTRY =================

    if not pos:

        # Cooldown
        if sym_state.get("last_exit_time"):
            if (now - sym_state["last_exit_time"]).seconds < COOLDOWN:
                return

        if state["balance"] < MIN_BALANCE:
            return

        qty = calculate_qty(symbol, price, state["balance"])

        # ✅ LONG → 15m cross ABOVE
        if prev_15m <= long_level and curr_15m > long_level:

            sym_state["position"] = {
                "side": "long",
                "entry": price,
                "qty": qty,
                "entry_time": now,
                "highest_price": price
            }

            utils.log(f"BUY {symbol} @ {price} | Qty: {qty}", tg=True)

        # ✅ SHORT → 15m cross BELOW
        elif prev_15m >= short_level and curr_15m < short_level:

            sym_state["position"] = {
                "side": "short",
                "entry": price,
                "qty": qty,
                "entry_time": now,
                "lowest_price": price
            }

            utils.log(f"SELL {symbol} @ {price} | Qty: {qty}", tg=True)

# ================= MAIN =================

def run():

    state = {
        "balance": 10000,
        "symbols": {
            s: {
                "position": None,
                "last_exit_time": None
            } for s in SYMBOLS
        }
    }

    utils.log("🚀 BOT STARTED (1D + 15m CONFIRMATION)", tg=True)

    while True:
        try:
            for symbol in SYMBOLS:

                df_1d = utils.fetch_candles(symbol)
                df_15m = utils_15m.fetch_candles(symbol)

                if df_1d is None or df_15m is None:
                    continue

                price = utils.fetch_price(symbol)

                if price is None:
                    continue

                process_symbol(symbol, df_1d, df_15m, price, state)

            time.sleep(3)

        except Exception as e:
            utils.log(f"ERROR: {e}", tg=True)
            time.sleep(5)

# ================= START =================

if __name__ == "__main__":
    run()