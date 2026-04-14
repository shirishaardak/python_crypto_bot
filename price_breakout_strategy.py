import os
import time
from datetime import datetime
from dotenv import load_dotenv

from utils import TradingUtils

load_dotenv()

# ================= CONFIG =================

BOT_NAME = "price_breakout_strategy"

SYMBOLS = ["BTCUSD", "ETHUSD"]

DEFAULT_CONTRACTS = {"BTCUSD": 100, "ETHUSD": 100}
CONTRACT_SIZE = {"BTCUSD": 0.001, "ETHUSD": 0.01}

REVERSAL = 1.0  # % reversal to exit

TAKER_FEE = 0.0005

TIMEFRAME = "1d"
DAYS = 15

MIN_BALANCE = 5000

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

def process_symbol(symbol, df, price, state, is_new_candle):

    sym_state = state["symbols"][symbol]
    pos = sym_state["position"]
    now = datetime.now()

    if df is None or len(df) < 3:
        return

    prev_close = df.iloc[-2]["Close"]

    # Print once per day
    if sym_state.get("last_prev_close") != prev_close:
        print(f"{symbol} | Prev Close: {round(prev_close,2)}")
        sym_state["last_prev_close"] = prev_close

    # ================= EXIT =================
    if pos:

        exit_trade = False
        pnl = 0

        if pos["side"] == "long":

            if price > pos["highest_price"]:
                pos["highest_price"] = price

            drawdown = ((pos["highest_price"] - price) / pos["highest_price"]) * 100

            if drawdown >= REVERSAL:
                pnl = (price - pos["entry"]) * CONTRACT_SIZE[symbol] * pos["qty"]
                exit_trade = True

        elif pos["side"] == "short":

            if price < pos["lowest_price"]:
                pos["lowest_price"] = price

            drawup = ((price - pos["lowest_price"]) / pos["lowest_price"]) * 100

            if drawup >= REVERSAL:
                pnl = (pos["entry"] - price) * CONTRACT_SIZE[symbol] * pos["qty"]
                exit_trade = True

        if exit_trade:

            entry_fee = utils.commission(pos["entry"], pos["qty"], symbol)
            exit_fee  = utils.commission(price, pos["qty"], symbol)

            net = pnl - (entry_fee + exit_fee)

            # ✅ UPDATE GLOBAL BALANCE
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

            sym_state["position"] = None
            sym_state["last_trade_day"] = now.date()
            return

    # ================= ENTRY =================
    if not pos and is_new_candle:

        today = now.date()

        if sym_state.get("last_trade_day") == today:
            return

        # ✅ CHECK GLOBAL BALANCE
        if state["balance"] < MIN_BALANCE:
            utils.log(f"⚠️ Balance low: {round(state['balance'],2)}")
            return

        if price > prev_close:

            sym_state["position"] = {
                "side": "long",
                "entry": price,
                "qty": DEFAULT_CONTRACTS[symbol],
                "entry_time": now,
                "highest_price": price
            }

            utils.log(f"🟢 BUY {symbol} @ {price}", tg=True)

        elif price < prev_close:

            sym_state["position"] = {
                "side": "short",
                "entry": price,
                "qty": DEFAULT_CONTRACTS[symbol],
                "entry_time": now,
                "lowest_price": price
            }

            utils.log(f"🔴 SELL {symbol} @ {price}", tg=True)

# ================= MAIN =================

def run():

    state = {
        "balance": 5000,  # ✅ GLOBAL BALANCE
        "symbols": {
            s: {
                "position": None,
                "last_candle_time": None,
                "last_trade_day": None,
                "last_prev_close": None
            } for s in SYMBOLS
        }
    }

    utils.log("🚀 BOT STARTED (TREND FOLLOW MODE)", tg=True)

    while True:
        try:
            for symbol in SYMBOLS:

                df = utils.fetch_candles(symbol)

                if df is None or len(df) < 10:
                    continue

                sym_state = state["symbols"][symbol]

                latest_candle_time = df.index[-2]

                is_new_candle = sym_state["last_candle_time"] != latest_candle_time

                if is_new_candle:
                    sym_state["last_candle_time"] = latest_candle_time

                price = utils.fetch_price(symbol)

                if price is None:
                    continue

                process_symbol(symbol, df, price, state, is_new_candle)

            time.sleep(3)

        except Exception as e:
            utils.log(f"🚨 Runtime error: {e}", tg=True)
            time.sleep(5)

# ================= START =================

if __name__ == "__main__":
    run()