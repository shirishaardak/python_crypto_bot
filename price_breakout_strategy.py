import os
import time
from datetime import datetime
from dotenv import load_dotenv

from utils import TradingUtils

load_dotenv()

# ================= CONFIG =================

BOT_NAME = "price_breakout_strategy"

SYMBOLS = ["BTCUSD", "ETHUSD"]

CONTRACT_SIZE = {
    "BTCUSD": 1,     # FIXED (was 0.001 → caused distortion)
    "ETHUSD": 1
}

RISK_PER_TRADE = 0.01   # 1% portfolio risk (TOTAL, not per symbol)

REVERSAL = {
    "BTCUSD": 0.5,
    "ETHUSD": 1.0
}

BUFFER = 0.005

TAKER_FEE = 0.0005

TIMEFRAME = "1d"
DAYS = 15

MIN_BALANCE = 1000

COOLDOWN = 60

MAX_OPEN_POSITIONS = 2   # portfolio cap

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

def calculate_qty(symbol, price, balance, open_positions):
    """
    Portfolio-safe sizing:
    - Risk is shared across open positions
    """
    if open_positions >= MAX_OPEN_POSITIONS:
        return 0

    risk_amount = (balance * RISK_PER_TRADE) / MAX_OPEN_POSITIONS
    reversal_percent = REVERSAL[symbol]

    stop_distance = price * (reversal_percent / 100)

    if stop_distance <= 0:
        return 0

    qty = risk_amount / (stop_distance * CONTRACT_SIZE[symbol])

    qty = int(qty)

    return qty if qty >= 1 else 0


# ================= STRATEGY =================

def process_symbol(symbol, df, price, state):

    sym_state = state["symbols"][symbol]
    pos = sym_state["position"]
    now = datetime.now()

    if df is None or len(df) < 3:
        return

    prev_close = df.iloc[-2]["Close"]

    if sym_state.get("last_prev_close") != prev_close:
        print(f"{symbol} | Prev Close: {round(prev_close, 2)}")
        sym_state["last_prev_close"] = prev_close

    reversal = REVERSAL[symbol]

    # Count active positions across portfolio
    open_positions = sum(
        1 for s in state["symbols"].values() if s["position"]
    )

    # ================= EXIT =================

    if pos:

        exit_trade = False
        pnl = 0

        if pos["side"] == "long":

            pos["highest_price"] = max(pos["highest_price"], price)

            drawdown = ((pos["highest_price"] - price) / pos["highest_price"]) * 100

            if drawdown >= reversal:
                pnl = (price - pos["entry"]) * CONTRACT_SIZE[symbol] * pos["qty"]
                exit_trade = True

        elif pos["side"] == "short":

            pos["lowest_price"] = min(pos["lowest_price"], price)

            drawup = ((price - pos["lowest_price"]) / pos["lowest_price"]) * 100

            if drawup >= reversal:
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

            emoji = "🟢" if net > 0 else "🔴"
            utils.log(f"{emoji} EXIT {symbol} @ {price} | PNL: {round(net, 6)}", tg=True)
            utils.log(f"💰 Balance: {round(state['balance'], 2)}", tg=True)

            sym_state["position"] = None
            sym_state["last_exit_time"] = now
            return

    # ================= ENTRY =================

    if not pos:

        # cooldown
        if sym_state.get("last_exit_time"):
            if (now - sym_state["last_exit_time"]).seconds < COOLDOWN:
                return

        if state["balance"] < MIN_BALANCE:
            return

        if open_positions >= MAX_OPEN_POSITIONS:
            return

        long_level = prev_close * (1 + BUFFER)
        short_level = prev_close * (1 - BUFFER)

        qty = calculate_qty(symbol, price, state["balance"], open_positions)

        if qty == 0:
            return

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

    utils.log("🚀 BOT STARTED (PORTFOLIO SAFE MODE)", tg=True)

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


if __name__ == "__main__":
    run()