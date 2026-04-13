import os
import time
import pandas as pd
from datetime import datetime
from dotenv import load_dotenv

from utils import TradingUtils

load_dotenv()

# ================= CONFIG =================

BOT_NAME = "price_breakout_strategy"

SYMBOLS = ["BTCUSD", "ETHUSD"]

DEFAULT_CONTRACTS = {"BTCUSD": 100, "ETHUSD": 100}
CONTRACT_SIZE = {"BTCUSD": 0.001, "ETHUSD": 0.01}

BREAKOUT = 0.3      # % breakout
STOP_BUFFER = 0.2   # % initial SL
TSL = 0.1           # % trailing SL

TAKER_FEE = 0.0005

TIMEFRAME = "1d"
DAYS = 15

MIN_BALANCE = 5000

# ================= INIT UTILS =================

utils = TradingUtils(
    contract_size=CONTRACT_SIZE,
    taker_fee=TAKER_FEE,
    timeframe=TIMEFRAME,
    days=DAYS,
    telegram_token=os.getenv("testing_strategy_my_aglo_bot"),
    telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID"),
    bot_name=BOT_NAME
)

# ================= LEVELS =================

def get_levels(df):
    if df is None or len(df) < 3:
        return None, None, None

    prev_close = df.iloc[-2]["Close"]

    breakout_value = prev_close * (BREAKOUT / 100)

    buy_level = prev_close + breakout_value
    sell_level = prev_close - breakout_value

    return prev_close, buy_level, sell_level

# ================= STRATEGY =================

def process_symbol(symbol, df, price, state, is_new_candle):

    pos = state["position"]
    now = datetime.now()

    prev_close, buy_level, sell_level = get_levels(df)

    if prev_close is None:
        return

    # Print levels only when changed
    if state.get("last_prev_close") != prev_close:
        print(f"{symbol} | Close: {round(prev_close,2)} | Buy: {round(buy_level,2)} | Sell: {round(sell_level,2)}")
        state["last_prev_close"] = prev_close

    # ================= TRAILING SL =================
    if pos:

        if pos["side"] == "long":
            new_sl = price - (price * TSL / 100)
            if new_sl > pos["sl"]:
                pos["sl"] = new_sl
                utils.log(f"🔁 TSL Updated {symbol} SL: {round(pos['sl'],2)}")

        elif pos["side"] == "short":
            new_sl = price + (price * TSL / 100)
            if new_sl < pos["sl"]:
                pos["sl"] = new_sl
                utils.log(f"🔁 TSL Updated {symbol} SL: {round(pos['sl'],2)}")

    # ================= EXIT =================
    if pos:

        exit_trade = False
        pnl = 0

        if pos["side"] == "long" and price <= pos["sl"]:
            pnl = (price - pos["entry"]) * CONTRACT_SIZE[symbol] * pos["qty"]
            exit_trade = True

        elif pos["side"] == "short" and price >= pos["sl"]:
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

    # ================= ENTRY =================
    if not pos and is_new_candle:

        today = now.date()

        if state.get("last_trade_day") == today:
            return

        balance = state["balance"]

        if balance < MIN_BALANCE:
            utils.log(f"⚠️ Balance low: {balance}")
            return

        # BUY BREAKOUT
        if price > buy_level:

            sl = price - (price * STOP_BUFFER / 100)

            state["position"] = {
                "side": "long",
                "entry": price,
                "qty": DEFAULT_CONTRACTS[symbol],
                "entry_time": now,
                "sl": sl
            }

            utils.log(f"🟢 BUY {symbol} @ {price} | SL: {round(sl,2)}", tg=True)

        # SELL BREAKDOWN
        elif price < sell_level:

            sl = price + (price * STOP_BUFFER / 100)

            state["position"] = {
                "side": "short",
                "entry": price,
                "qty": DEFAULT_CONTRACTS[symbol],
                "entry_time": now,
                "sl": sl
            }

            utils.log(f"🔴 SELL {symbol} @ {price} | SL: {round(sl,2)}", tg=True)

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

    utils.log("🚀 BOT STARTED (PRO BREAKOUT MODE)", tg=True)

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