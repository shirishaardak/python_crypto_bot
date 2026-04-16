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

BUFFER = 0.005  # 0.2%

TAKER_FEE = 0.0005

TIMEFRAME = "1d"
DAYS = 15

MIN_BALANCE = 1000

# Optional cooldown (in seconds) to avoid rapid re-entry
COOLDOWN = 60

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

# ================= POSITION SIZING =================

def calculate_qty(symbol, price, balance):
    risk_amount = balance * RISK_PER_TRADE
    reversal_percent = REVERSAL[symbol]

    stop_distance = price * (reversal_percent / 100)

    qty = risk_amount / (stop_distance * CONTRACT_SIZE[symbol])

    return max(1, int(qty))


# ================= STRATEGY =================

def process_symbol(symbol, df, price, state):

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

    reversal = REVERSAL[symbol]

    # ================= EXIT =================
    if pos:

        exit_trade = False
        pnl = 0

        if pos["side"] == "long":

            if price > pos["highest_price"]:
                pos["highest_price"] = price

            drawdown = ((pos["highest_price"] - price) / pos["highest_price"]) * 100

            if drawdown >= reversal:
                pnl = (price - pos["entry"]) * CONTRACT_SIZE[symbol] * pos["qty"]
                exit_trade = True

        elif pos["side"] == "short":

            if price < pos["lowest_price"]:
                pos["lowest_price"] = price

            drawup = ((price - pos["lowest_price"]) / pos["lowest_price"]) * 100

            if drawup >= reversal:
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

            sym_state["position"] = None
            sym_state["last_exit_time"] = now  # cooldown tracker
            return

    # ================= ENTRY =================
    if not pos:

        # Cooldown check
        if sym_state.get("last_exit_time"):
            seconds_since_exit = (now - sym_state["last_exit_time"]).seconds
            if seconds_since_exit < COOLDOWN:
                return

        if state["balance"] < MIN_BALANCE:
            utils.log(f"⚠️ Balance low: {round(state['balance'],2)}")
            return

        long_level = prev_close * (1 + BUFFER)
        short_level = prev_close * (1 - BUFFER)

        print(f"{symbol} | Price: {price}")
        print(f"{symbol} | Long Level: {round(long_level,2)} | Short Level: {round(short_level,2)}")

        qty = calculate_qty(symbol, price, state["balance"])

        if price > long_level:

            sym_state["position"] = {
                "side": "long",
                "entry": price,
                "qty": qty,
                "entry_time": now,
                "highest_price": price
            }

            utils.log(f"🟢 BUY {symbol} @ {price} | Qty: {qty}", tg=True)

        elif price < short_level:

            sym_state["position"] = {
                "side": "short",
                "entry": price,
                "qty": qty,
                "entry_time": now,
                "lowest_price": price
            }

            utils.log(f"🔴 SELL {symbol} @ {price} | Qty: {qty}", tg=True)


# ================= MAIN =================

def run():

    state = {
        "balance": 10000,
        "symbols": {
            s: {
                "position": None,
                "last_prev_close": None,
                "last_exit_time": None
            } for s in SYMBOLS
        }
    }

    utils.log("🚀 BOT STARTED (UNLIMITED MODE)", tg=True)

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

            time.sleep(3)

        except Exception as e:
            utils.log(f"🚨 Runtime error: {e}", tg=True)
            time.sleep(5)


# ================= START =================

if __name__ == "__main__":
    run()