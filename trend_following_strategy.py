import os
import time
from datetime import datetime
from dotenv import load_dotenv

from utils import TradingUtils

load_dotenv()

# ================= CONFIG =================

BOT_NAME = "trend_following_strategy"

SYMBOLS = ["BTCUSD", "ETHUSD"]

INITIAL_BALANCE = 5000

DEFAULT_CONTRACTS = {"BTCUSD": 100, "ETHUSD": 100}
CONTRACT_SIZE = {"BTCUSD": 0.001, "ETHUSD": 0.01}

TAKER_FEE = 0.0005

STEP = 0.35
COOLDOWN_SEC = 30

# ================= INIT =================

utils = TradingUtils(
    contract_size=CONTRACT_SIZE,
    taker_fee=TAKER_FEE,
    timeframe="1h",
    days=5,
    telegram_token=os.getenv("trend_following_strategy_bot"),
    telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID"),
    bot_name=BOT_NAME
)

# ================= HELPERS =================

def get_24h_change(symbol):
    try:
        ticker = utils.safe_get(
            f"https://api.india.delta.exchange/v2/tickers/{symbol}"
        )
        return float(ticker["result"]["ltp_change_24h"])
    except:
        return None


def is_cross_up(prev, curr, level):
    return prev is not None and prev < level and curr >= level


def is_cross_down(prev, curr, level):
    return prev is not None and prev > level and curr <= level


# ================= STRATEGY =================

def process_symbol(symbol, price, state, account):

    pos = state["position"]
    change = get_24h_change(symbol)

    if change is None:
        return

    prev_change = state.get("last_change")
    contract = CONTRACT_SIZE[symbol]

    # ================= EXIT =================
    if pos:

        exit_trade = False
        pnl = 0

        # ===== LONG EXIT =====
        if pos["side"] == "long":

            # EXIT ONLY: momentum turns negative
            if change < 0:
                pnl = (price - pos["entry"]) * contract * pos["qty"]
                exit_trade = True

        # ===== SHORT EXIT =====
        elif pos["side"] == "short":

            # EXIT ONLY: momentum turns positive
            if change > 0:
                pnl = (pos["entry"] - price) * contract * pos["qty"]
                exit_trade = True

        if exit_trade:

            entry_fee = pos["entry_fee"]
            exit_fee = utils.commission(price, pos["qty"], symbol)

            net_pnl = pnl - entry_fee - exit_fee
            account["balance"] += net_pnl

            utils.save_trade({
                "symbol": symbol,
                "side": pos["side"],
                "entry_time": pos["entry_time"],
                "exit_time": datetime.now(),
                "entry_price": pos["entry"],
                "exit_price": price,
                "qty": pos["qty"],
                "net_pnl": round(net_pnl, 6),
                "balance": round(account["balance"], 2)
            })

            utils.log(
                f"❌ EXIT {symbol} | PNL: {round(net_pnl,2)} | Balance: {round(account['balance'],2)}",
                tg=True
            )

            state["position"] = None
            state["cooldown"] = time.time()
            return

    # ================= ENTRY =================
    if not pos and prev_change is not None:

        # cooldown check
        if time.time() - state.get("cooldown", 0) < COOLDOWN_SEC:
            return

        qty = DEFAULT_CONTRACTS[symbol]

        # ===== LONG ENTRY =====
        if is_cross_up(prev_change, change, 0.5):

            entry_fee = utils.commission(price, qty, symbol)

            state["position"] = {
                "side": "long",
                "entry": price,
                "qty": qty,
                "entry_time": datetime.now(),
                "entry_fee": entry_fee
            }

            utils.log(
                f"🟢 LONG {symbol} | Price: {price} | 24h: {round(change,3)}",
                tg=True
            )

        # ===== SHORT ENTRY =====
        elif is_cross_down(prev_change, change, -0.5):

            entry_fee = utils.commission(price, qty, symbol)

            state["position"] = {
                "side": "short",
                "entry": price,
                "qty": qty,
                "entry_time": datetime.now(),
                "entry_fee": entry_fee
            }

            utils.log(
                f"🔴 SHORT {symbol} | Price: {price} | 24h: {round(change,3)}",
                tg=True
            )

    state["last_change"] = change


# ================= MAIN =================

def run():

    state = {
        s: {
            "position": None,
            "last_change": None,
            "cooldown": 0
        } for s in SYMBOLS
    }

    account = {"balance": INITIAL_BALANCE}

    utils.log(f"🚀 BOT STARTED | Balance: ${INITIAL_BALANCE}", tg=True)

    while True:
        try:
            for symbol in SYMBOLS:

                price = utils.fetch_price(symbol)

                if price is None:
                    continue

                process_symbol(symbol, price, state[symbol], account)

            time.sleep(3)

        except Exception as e:
            utils.log(f"ERROR: {e}", tg=True)
            time.sleep(5)


# ================= START =================

if __name__ == "__main__":
    run()