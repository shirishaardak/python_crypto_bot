import os
import time
import pandas as pd
import numpy as np
from datetime import datetime
import pandas_ta as ta
from dotenv import load_dotenv
from delta_rest_client import DeltaRestClient, OrderType
import traceback
import subprocess

from utils import TradingUtils

load_dotenv()

# ================= CONFIG =================

BOT_NAME = "price_trend_strategy"

SYMBOLS = ["BTCUSD"]

DEFAULT_CONTRACTS = {"BTCUSD": 50}
CONTRACT_SIZE = {"BTCUSD": 0.001}
STOPLOSS = {"BTCUSD": 400}
TAKER_FEE = 0.0005

TIMEFRAME = "15m"
DAYS = 15

last_git_push = time.time()

# ================= AUTO GIT =================

def auto_git_push():
    global last_git_push

    if time.time() - last_git_push < 3600:
        return

    try:
        subprocess.run("git add -A", shell=True)

        res = subprocess.run(
            'git diff --cached --quiet || git commit -m "auto update"',
            shell=True
        )

        if res.returncode != 0:
            utils.log("✅ Changes committed")

        res = subprocess.run("git push origin main", shell=True)

        if res.returncode == 0:
            utils.log("✅ Git Push Done", tg=True)

        last_git_push = time.time()

    except Exception as e:
        utils.log(f"Git Error: {e}")

# ================= INIT UTILS =================

utils = TradingUtils(
    contract_size=CONTRACT_SIZE,
    taker_fee=TAKER_FEE,
    timeframe=TIMEFRAME,
    days=DAYS,
    telegram_token=os.getenv("price_trend_strategy_bot"),
    telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID"),
    bot_name=BOT_NAME
)

# ================= DELTA API =================

client = DeltaRestClient(
    base_url="https://api.india.delta.exchange",
    api_key=os.getenv("DELTA_API_KEY"),
    api_secret=os.getenv("DELTA_API_SECRET")
)

# ================= PRODUCT IDS =================

def get_product_ids(symbols):
    try:
        data = utils.safe_get("https://api.india.delta.exchange/v2/products")

        product_map = {}
        for p in data["result"]:
            if p["symbol"] in symbols and p["contract_type"] == "perpetual_futures":
                product_map[p["symbol"]] = p["id"]

        return product_map

    except Exception as e:
        utils.log(f"Product ID fetch error: {e}", tg=True)
        return {}

PRODUCT_IDS = get_product_ids(SYMBOLS)

if not PRODUCT_IDS:
    raise Exception("❌ Product IDs not loaded")

# ================= ORDER =================

def place_market_order(symbol, side, qty):
    try:
        order = client.place_order(
            product_id=PRODUCT_IDS[symbol],
            order_type=OrderType.MARKET,
            side=side,
            size=qty
        )

        if not order or ("success" in order and not order["success"]):
            utils.log(f"❌ {symbol} ORDER FAILED: {order}", tg=True)
            return None

        utils.log(f"✅ {symbol} ORDER SUCCESS", tg=True)
        return order

    except Exception as e:
        utils.log(f"❌ {symbol} Order Exception: {e}", tg=True)
        return None

# ================= TRENDLINE =================

def calculate_trendline(df):

    ha = ta.ha(df["Open"], df["High"], df["Low"], df["Close"]).reset_index(drop=True)

    order = 21
    ha["UPPER"] = ha["HA_high"].rolling(order).max()
    ha["LOWER"] = ha["HA_low"].rolling(order).min()

    ha["Trendline"] = np.nan
    trend = ha.loc[0, "HA_close"]
    ha.loc[0, "Trendline"] = trend

    for i in range(1, len(ha)):

        if ha.loc[i, "HA_high"] == ha.loc[i, "UPPER"]:
            trend = ha.loc[i, "HA_low"]

        elif ha.loc[i, "HA_low"] == ha.loc[i, "LOWER"]:
            trend = ha.loc[i, "HA_high"]

        ha.loc[i, "Trendline"] = trend

    return ha

# ================= STRATEGY =================

def process_symbol(symbol, df, price, state, is_new_candle):

    ha = calculate_trendline(df)

    last = ha.iloc[-2]   # CLOSED candle (for entry)
    prev = ha.iloc[-3]

    pos  = state["position"]
    now = datetime.now()

    # ================= TRAILING SL =================
    if pos:
        move = price - pos["entry"] if pos["side"] == "long" else pos["entry"] - price

        steps = int(move // 200)

        if steps > 0:
            if pos["side"] == "long":
                new_sl = pos["entry"] - STOPLOSS[symbol] + (steps * 200)
                if new_sl > pos["sl"]:
                    pos["sl"] = new_sl
                    utils.log(f"🔁 {symbol} TRAIL SL → {pos['sl']}")
            else:
                new_sl = pos["entry"] + STOPLOSS[symbol] - (steps * 200)
                if new_sl < pos["sl"]:
                    pos["sl"] = new_sl
                    utils.log(f"🔁 {symbol} TRAIL SL → {pos['sl']}")

    # ================= EXIT =================
    if pos:

        exit_trade = False
        pnl = 0

        # LONG EXIT
        if pos["side"] == "long" and (price <= pos["sl"] or last.HA_close < last.Trendline):

            order = place_market_order(symbol, "sell", pos["qty"])

            if order:
                pnl = (price - pos["entry"]) * CONTRACT_SIZE[symbol] * pos["qty"]
                exit_trade = True

        # SHORT EXIT
        if pos["side"] == "short" and (price >= pos["sl"] or last.HA_close > last.Trendline):

            order = place_market_order(symbol, "buy", pos["qty"])

            if order:
                pnl = (pos["entry"] - price) * CONTRACT_SIZE[symbol] * pos["qty"]
                exit_trade = True

        if exit_trade:

            fee = utils.commission(price, pos["qty"], symbol)
            net = pnl - fee

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
            utils.log(f"{emoji} {symbol} EXIT @ {price} | PNL: {round(net,6)}", tg=True)

            state["position"] = None
            state["last_exit_candle"] = df.index[-2]

            return

    # ================= ENTRY =================
    if not pos and is_new_candle:

        if state.get("last_exit_candle") == df.index[-1]:
            return

        # LONG ENTRY
        if last.HA_close > last.Trendline and last.HA_close > prev.HA_close and last.HA_close > prev.HA_open:

            order = place_market_order(symbol, "buy", DEFAULT_CONTRACTS[symbol])

            if order:
                state["position"] = {
                    "side": "long",
                    "entry": price,
                    "qty": DEFAULT_CONTRACTS[symbol],
                    "entry_time": now,
                    "sl": price - STOPLOSS[symbol]   # ✅ initial SL
                }

                utils.log(f"🟢 {symbol} LONG @ {price} | SL: {price - STOPLOSS[symbol]}", tg=True)

        # SHORT ENTRY
        elif last.HA_close < last.Trendline and last.HA_close < prev.HA_close and last.HA_close < prev.HA_open:

            order = place_market_order(symbol, "sell", DEFAULT_CONTRACTS[symbol])

            if order:
                state["position"] = {
                    "side": "short",
                    "entry": price,
                    "qty": DEFAULT_CONTRACTS[symbol],
                    "entry_time": now,
                    "sl": price + STOPLOSS[symbol]   # ✅ initial SL
                }

                utils.log(f"🔴 {symbol} SHORT @ {price} | SL: {price + STOPLOSS[symbol]}", tg=True)

# ================= MAIN =================

def run():

    state = {
        s: {
            "position": None,
            "last_candle_time": None,
            "last_exit_candle": None
        } for s in SYMBOLS
    }

    utils.log("🚀 LIVE BOT STARTED", tg=True)

    while True:

        try:
            for symbol in SYMBOLS:

                df = utils.fetch_candles(symbol)

                if df is None or len(df) < 100:
                    continue

                latest_candle_time = df.index[-1]

                is_new_candle = state[symbol]["last_candle_time"] != latest_candle_time

                if is_new_candle:
                    state[symbol]["last_candle_time"] = latest_candle_time

                price = utils.fetch_price(symbol)

                if price is None:
                    continue

                process_symbol(symbol, df, price, state[symbol], is_new_candle)

                auto_git_push()

            time.sleep(3)  # faster loop for live exit

        except Exception as e:
            utils.log(f"🚨 Runtime error: {e}", tg=True)
            time.sleep(5)

# ================= START =================

if __name__ == "__main__":
    run()