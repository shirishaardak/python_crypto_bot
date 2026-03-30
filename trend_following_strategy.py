import os
import time
import pandas as pd
import numpy as np
from datetime import datetime
import pandas_ta as ta
from dotenv import load_dotenv

from utils import TradingUtils

load_dotenv()

# ================= CONFIG =================

BOT_NAME = "trend_following_strategy"

SYMBOLS = ["BTCUSD", "ETHUSD"]

DEFAULT_CONTRACTS = {"BTCUSD": 100, "ETHUSD": 100}
CONTRACT_SIZE = {"BTCUSD": 0.001, "ETHUSD": 0.01}

TAKER_FEE = 0.0005

TIMEFRAME = "15m"
DAYS = 5

TAKE_PROFIT = {"BTCUSD": 300, "ETHUSD": 30}
STOP_LOSS   = {"BTCUSD": 100, "ETHUSD": 10}

ADX_LENGTH = 9
ADX_THRESHOLD = 25

# ================= INIT UTILS =================

utils = TradingUtils(
    contract_size=CONTRACT_SIZE,
    taker_fee=TAKER_FEE,
    timeframe=TIMEFRAME,
    days=DAYS,
    telegram_token=os.getenv("trend_following_strategy_bot"),
    telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID"),
    bot_name=BOT_NAME
)

# ================= OPTIONAL SAVE =================

def save_processed_data(df, ha, symbol):
    path = os.path.join(utils.SAVE_DIR, f"{symbol}_processed.csv")

    out = pd.DataFrame({
        "time": df.index,
        "HA_open": ha["HA_open"],
        "HA_high": ha["HA_high"],
        "HA_low": ha["HA_low"],
        "HA_close": ha["HA_close"],
        "trendline": ha["trendline"],
    })

    out.to_csv(path, index=False)

# ================= TRENDLINE =================

def calculate_trendline(df):

    ha = ta.ha(df["Open"], df["High"], df["Low"], df["Close"]).reset_index(drop=True)

    adx = ta.adx(ha["HA_high"], ha["HA_low"], ha["HA_close"], length=ADX_LENGTH)
    ha["ADX"] = adx[f"ADX_{ADX_LENGTH}"]

    ha["UPPER"] = ha["HA_high"].rolling(21).max()
    ha["LOWER"] = ha["HA_low"].rolling(21).min()

    trendline = np.zeros(len(ha))
    trend = ha["HA_close"].iloc[0]
    trendline[0] = trend

    for i in range(1, len(ha)):

        if ha["HA_high"].iloc[i] == ha["UPPER"].iloc[i]:
            trend = ha["HA_low"].iloc[i]

        elif ha["HA_low"].iloc[i] == ha["LOWER"].iloc[i]:
            trend = ha["HA_high"].iloc[i]

        trendline[i] = trend

    ha["trendline"] = trendline

    return ha

# ================= STRATEGY =================

def process_symbol(symbol, df, price, state):

    ha = calculate_trendline(df)

    last = ha.iloc[-2]
    prev = ha.iloc[-3]

    pos = state["position"]
    now = datetime.now()

    adx_ok = last.ADX > ADX_THRESHOLD

    # ENTRY
    if pos is None and adx_ok:

        if last.HA_close > last.trendline and last.HA_close > prev.HA_close and last.HA_close > prev.HA_open:

            state["position"] = {
                "side": "long",
                "entry": price,
                "stop": last.trendline - STOP_LOSS[symbol],
                "tp": price + TAKE_PROFIT[symbol],
                "qty": DEFAULT_CONTRACTS[symbol],
                "entry_time": now
            }

            utils.log(f"🟢 {symbol} LONG ENTRY @ {price}", tg=True)
            return

        if last.HA_close < last.trendline and last.HA_close < prev.HA_close and last.HA_close < prev.HA_open:

            state["position"] = {
                "side": "short",
                "entry": price,
                "stop": last.trendline + STOP_LOSS[symbol],
                "tp": price - TAKE_PROFIT[symbol],
                "qty": DEFAULT_CONTRACTS[symbol],
                "entry_time": now
            }

            utils.log(f"🔴 {symbol} SHORT ENTRY @ {price}", tg=True)
            return

    # EXIT
    if pos:

        exit_trade = False

        if pos["side"] == "long":
            if price <= pos["stop"] or last.HA_close <= last.trendline:
                pnl = (price - pos["entry"]) * CONTRACT_SIZE[symbol] * pos["qty"]
                exit_trade = True

        else:
            if price >= pos["stop"] or last.HA_close >= last.trendline:
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
            utils.log(f"{emoji} {symbol} EXIT @ {price} | PNL {round(net,6)}", tg=True)

            state["position"] = None

# ================= MAIN =================

def run():

    state = {s: {"position": None, "last_candle_time": None} for s in SYMBOLS}

    utils.log("🚀 Trend Following Strategy LIVE", tg=True)

    while True:

        try:
            for symbol in SYMBOLS:

                df = utils.fetch_candles(symbol)

                if df is None or len(df) < 100:
                    continue

                latest_candle_time = df.index[-1]

                if state[symbol]["last_candle_time"] == latest_candle_time:
                    continue

                state[symbol]["last_candle_time"] = latest_candle_time

                price = utils.fetch_price(symbol)

                if price is None:
                    continue

                process_symbol(symbol, df, price, state[symbol])

            time.sleep(60)

        except Exception as e:
            utils.log(f"Runtime error: {e}", tg=True)
            time.sleep(5)


if __name__ == "__main__":
    run()