import os
import time
from datetime import datetime
from dotenv import load_dotenv

from utils import TradingUtils

load_dotenv()

# ================= CONFIG =================

BOT_NAME = "trend_follow_strategy"

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

    pos = state["position"]
    now = datetime.now()

    if df is None or len(df) < 3:
        return

    prev_close = df.iloc[-2]["Close"]

    # Print once per day
    if state.get("last_prev_close") != prev_close:
        print(f"{symbol} | Prev Close: {round(prev_close,2)}")
        state["last_prev_close"] = prev_close

    # ================= EXIT (TREND REVERSAL) =================
    if pos:

        exit_trade = False
        pnl = 0

        if pos["side"] == "long":

            # update highest price
            if price > pos["highest_price"]:
                pos["highest_price"] = price

            # calculate drawdown
            drawdown = ((pos["highest_price"] - price) / pos["highest_price"]) * 100

            if drawdown >= REVERSAL:
                pnl = (price - pos["entry"]) * CONTRACT_SIZE[symbol] * pos["qty"]
                exit_trade = True

        elif pos["side"] == "short":

            # update lowest price
            if price < pos["lowest_price"]:
                pos["lowest_price"] = price

            # calculate bounce
            drawup = ((price - pos["lowest_price"]) / pos["lowest_price"]) * 100

            if drawup >= REVERSAL:
                pnl = (pos["entry"] - price) * CONTRACT_SIZE[symbol] * pos["qty"]
                exit_trade = True

        if exit_trade:

            entry_fee = utils.commission(pos["entry"], pos["qty"], symbol)
            exit_fee  = utils.commission(price, pos["qty"], symbol)

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

            emoji = "🟢" if net > 0 else "🔴"
            utils.log(f"{emoji} EXIT {symbol} @ {price} | PNL: {round(net,6)}", tg=True)
            utils.log(f"💰 Balance: {round(state['balance'],2)}", tg=True)

            state["position"] = None
            state["last_trade_day"] = now.date()
            return

    # ================= ENTRY (TREND FOLLOW) =================
    if not pos and is_new_candle:

        today = now.date()

        if state.get("last_trade_day") == today:
            return

        if state["balance"] < MIN_BALANCE:
            utils.log(f"⚠️ Balance low: {state['balance']}")
            return

        # BUY if price above previous close
        if price > prev_close:

            state["position"] = {
                "side": "long",
                "entry": price,
                "qty": DEFAULT_CONTRACTS[symbol],
                "entry_time": now,
                "highest_price": price
            }

            utils.log(f"🟢 BUY {symbol} @ {price}", tg=True)

        # SELL if price below previous close
        elif price < prev_close:

            state["position"] = {
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
        s: {
            "position": None,
            "last_candle_time": None,
            "last_trade_day": None,
            "last_prev_close": None,
            "balance": 5000
        } for s in SYMBOLS
    }

    utils.log("🚀 BOT STARTED (TREND FOLLOW MODE)", tg=True)

    while True:
        try:
            for symbol in SYMBOLS:

                df = utils.fetch_candles(symbol)

                if df is None or len(df) < 10:
                    continue

                latest_candle_time = df.index[-2]

                is_new_candle = state[symbol]["last_candle_time"] != latest_candle_time

                if is_new_candle:
                    state[symbol]["last_candle_time"] = latest_candle_time

                price = utils.fetch_price(symbol)

                if price is None:
                    continue

                process_symbol(symbol, df, price, state[symbol], is_new_candle)

            time.sleep(3)

        except Exception as e:
            utils.log(f"🚨 Runtime error: {e}", tg=True)
            time.sleep(5)

# ================= START =================

if __name__ == "__main__":
    run()