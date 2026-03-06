import os
import time
import requests
import pandas as pd
from datetime import datetime, timedelta
from dotenv import load_dotenv
import pandas_ta as ta
import numpy as np
import traceback

load_dotenv()

# ================= SETTINGS =================
SYMBOLS = ["BTCUSD", "ETHUSD"]

DEFAULT_CONTRACTS = {"BTCUSD": 100, "ETHUSD": 100}
CONTRACT_SIZE = {"BTCUSD": 0.001, "ETHUSD": 0.01}

TRAIL_POINTS = {"BTCUSD": 1, "ETHUSD": 1}

TAKER_FEE = 0.0005
EXECUTION_TF = "1m"
DAYS = 5

LAST_CANDLE_TIME = {}

# ================= DIRECTORIES =================
BASE_DIR = os.getcwd()
SAVE_DIR = os.path.join(BASE_DIR, "data", "supertrend_reverse_strategy")
os.makedirs(SAVE_DIR, exist_ok=True)

TRADE_CSV = os.path.join(SAVE_DIR, "live_trades.csv")

if not os.path.exists(TRADE_CSV):
    pd.DataFrame(columns=[
        "entry_time","exit_time","symbol","side",
        "entry_price","exit_price","qty","net_pnl"
    ]).to_csv(TRADE_CSV, index=False)

# ================= UTIL =================
def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}")

def commission(price, qty, symbol):
    notional = price * CONTRACT_SIZE[symbol] * qty
    return notional * TAKER_FEE

# ================= DATA =================
def fetch_price(symbol):
    try:
        r = requests.get(
            f"https://api.india.delta.exchange/v2/tickers/{symbol}",
            timeout=5
        )
        return float(r.json()["result"]["mark_price"])
    except:
        return None

def fetch_candles(symbol, resolution):
    start = int((datetime.now() - timedelta(days=DAYS)).timestamp())

    params = {
        "resolution": resolution,
        "symbol": symbol,
        "start": str(start),
        "end": str(int(time.time()))
    }

    try:
        r = requests.get(
            "https://api.india.delta.exchange/v2/history/candles",
            params=params,
            timeout=10
        )

        df = pd.DataFrame(
            r.json()["result"],
            columns=["time","open","high","low","close","volume"]
        )

        # Normalize columns to lowercase (IMPORTANT FIX)
        df.columns = df.columns.str.lower()

        df["time"] = pd.to_datetime(
            df["time"], unit="s", utc=True
        ).dt.tz_convert("Asia/Kolkata")

        df.set_index("time", inplace=True)
        df.sort_index(inplace=True)

        return df.astype(float).dropna()

    except:
        return None

# ================= INDICATORS =================
def calculate_trendline(df, order=21):
    data = df.copy().reset_index(drop=True)

    data["ha_close"] = (
        data["open"] + data["high"] + data["low"] + data["close"]
    ) / 4

    ha_open = np.zeros(len(data))
    ha_open[0] = (data["open"].iloc[0] + data["close"].iloc[0]) / 2

    for i in range(1, len(data)):
        ha_open[i] = (ha_open[i - 1] + data["ha_close"].iloc[i - 1]) / 2

    data["ha_open"] = ha_open
    data["ha_high"] = np.maximum.reduce(
        [data["ha_open"], data["ha_close"], data["high"]]
    )
    data["ha_low"] = np.minimum.reduce(
        [data["ha_open"], data["ha_close"], data["low"]]
    )

    data["upper"] = data["ha_high"].rolling(order).max()
    data["lower"] = data["ha_low"].rolling(order).min()

    st = ta.supertrend(
        high=data["ha_high"],
        low=data["ha_low"],
        close=data["ha_close"],
        length=21,
        multiplier=3
    )

    data["supertrend"] = st.iloc[:, 0]

    return data

# ================= STRATEGY =================
def process_symbol(symbol, df, price, state, allow_entry):

    ha = calculate_trendline(df)

    if len(ha) < 30:
        return

    last = ha.iloc[-2]
    prev = ha.iloc[-4]

    pos = state["position"]
    trail_points = TRAIL_POINTS[symbol]

    # ================= ENTRY =================
    if allow_entry and pos is None:

        long_signal = (
            last.close > prev.upper and
            last.close > last.supertrend
        )

        short_signal = (
            last.close < prev.lower and
            last.close < last.supertrend
        )

        if long_signal or short_signal:

            side = "long" if long_signal else "short"
            initial_sl = last.supertrend

            state["position"] = {
                "side": side,
                "entry": price,
                "qty": DEFAULT_CONTRACTS[symbol],
                "initial_sl": initial_sl,
                "sl": initial_sl,
                "trail_steps": 0,
                "entry_time": datetime.now()
            }

            log(f"{symbol} {side.upper()} ENTRY @ {price} | SL: {initial_sl}")
            return

    # ================= MANAGEMENT =================
    if pos:

        exit_trade = False

        if pos["side"] == "long":

            move = price - pos["entry"]
            steps = int(move // trail_points)

            if steps > pos["trail_steps"]:
                pos["sl"] = pos["initial_sl"] + (steps * trail_points)
                pos["trail_steps"] = steps
                log(f"{symbol} TRAIL SL MOVED TO {pos['sl']}")

            if price <= pos["sl"]:
                exit_trade = True

            pnl = (price - pos["entry"]) * CONTRACT_SIZE[symbol] * pos["qty"]

        else:

            move = pos["entry"] - price
            steps = int(move // trail_points)

            if steps > pos["trail_steps"]:
                pos["sl"] = pos["initial_sl"] - (steps * trail_points)
                pos["trail_steps"] = steps
                log(f"{symbol} TRAIL SL MOVED TO {pos['sl']}")

            if price >= pos["sl"]:
                exit_trade = True

            pnl = (pos["entry"] - price) * CONTRACT_SIZE[symbol] * pos["qty"]

        if exit_trade:

            fee = commission(price, pos["qty"], symbol) + \
                  commission(pos["entry"], pos["qty"], symbol)

            net = pnl - fee

            log(f"{symbol} EXIT @ {price} | NET PNL: {round(net,6)}")

            trade_data = {
                "entry_time": pos["entry_time"],
                "exit_time": datetime.now(),
                "symbol": symbol,
                "side": pos["side"],
                "entry_price": pos["entry"],
                "exit_price": price,
                "qty": pos["qty"],
                "net_pnl": round(net, 6)
            }

            pd.DataFrame([trade_data]).to_csv(
                TRADE_CSV,
                mode="a",
                header=False,
                index=False
            )

            state["position"] = None

# ================= MAIN =================
def run():

    state = {s: {"position": None} for s in SYMBOLS}

    log("🚀 Strategy LIVE Started")

    while True:
        try:
            for symbol in SYMBOLS:

                df = fetch_candles(symbol, EXECUTION_TF)
                if df is None or len(df) < 50:
                    continue

                price = fetch_price(symbol)
                if price is None:
                    continue

                new_candle = df.index[-2] != LAST_CANDLE_TIME.get(symbol)
                if new_candle:
                    LAST_CANDLE_TIME[symbol] = df.index[-2]

                if state[symbol]["position"]:
                    process_symbol(symbol, df, price, state[symbol], False)
                elif new_candle:
                    process_symbol(symbol, df, price, state[symbol], True)

            time.sleep(10)

        except Exception as e:
            log("Runtime error:")
            traceback.print_exc()
            time.sleep(5)

if __name__ == "__main__":
    run()