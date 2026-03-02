import os
import time
import requests
import pandas as pd
from datetime import datetime, timedelta
from dotenv import load_dotenv
import pandas_ta as ta
import numpy as np

load_dotenv()

# ================= SETTINGS =================
SYMBOLS = ["BTCUSD", "ETHUSD"]

DEFAULT_CONTRACTS = {"BTCUSD": 100, "ETHUSD": 100}
CONTRACT_SIZE = {"BTCUSD": 0.001, "ETHUSD": 0.01}

TRAIL_POINTS = {"BTCUSD": 100, "ETHUSD": 10}

TAKER_FEE = 0.0005
EXECUTION_TF = "5m"
DAYS = 5

LAST_CANDLE_TIME = {}

# ================= DIRECTORIES =================
BASE_DIR = os.getcwd()
SAVE_DIR = os.path.join(BASE_DIR, "data", "supertrend_reverse_strategy")
os.makedirs(SAVE_DIR, exist_ok=True)

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

        df.rename(columns=str.title, inplace=True)

        df["Time"] = pd.to_datetime(
            df["Time"], unit="s", utc=True
        ).dt.tz_convert("Asia/Kolkata")

        df.set_index("Time", inplace=True)
        df.sort_index(inplace=True)

        return df.astype(float).dropna()

    except:
        return None


# ================= INDICATORS =================
def calculate_heikin_ashi(df):
    ha = df.copy()

    ha["HA_close"] = (ha["Open"] + ha["High"] + ha["Low"] + ha["Close"]) / 4
    ha["HA_open"] = 0.0

    ha.iloc[0, ha.columns.get_loc("HA_open")] = (
        ha["Open"].iloc[0] + ha["Close"].iloc[0]
    ) / 2

    for i in range(1, len(ha)):
        ha.iloc[i, ha.columns.get_loc("HA_open")] = (
            ha["HA_open"].iloc[i-1] + ha["HA_close"].iloc[i-1]
        ) / 2

    return ha


def add_supertrend(df):
    st = ta.supertrend(
        high=df["High"],
        low=df["Low"],
        close=df["Close"],
        length=10,
        multiplier=3
    )
    df["SUPERTREND"] = st.filter(like="SUPERT").iloc[:, 0]
    return df


def save_processed_data(df, symbol):

    path = os.path.join(SAVE_DIR, f"{symbol}_processed.csv")

    out = df.copy()

    cols = [
        "HA_open",
        "HA_close",
        "SUPERTREND"
    ]

    existing = [c for c in cols if c in out.columns]

    if existing:
        out = out[existing].copy()
        out["time"] = df.index
        out = out.tail(500)
        out.to_csv(path, index=False)


# ================= STRATEGY =================
def process_symbol(symbol, df, price, state, allow_entry):

    df = add_supertrend(df)
    ha = calculate_heikin_ashi(df)

    # save_processed_data(ha, symbol)

    if len(ha) < 3:
        return

    last = ha.iloc[-2]
    prev = ha.iloc[-3]

    pos = state["position"]
    trail_points = TRAIL_POINTS[symbol]

    # ================= ENTRY =================
    if allow_entry and pos is None:

        long_signal = (
            last.HA_close > prev.HA_open and
            last.HA_close > prev.HA_close and
            last.Close > last.SUPERTREND
        )

        short_signal = (
            last.HA_close < prev.HA_open and
            last.HA_close < prev.HA_close and
            last.Close < last.SUPERTREND
        )

        if long_signal or short_signal:

            side = "long" if long_signal else "short"
            initial_sl = last.SUPERTREND

            state["position"] = {
                "side": side,
                "entry": price,
                "qty": DEFAULT_CONTRACTS[symbol],
                "initial_sl": initial_sl,
                "sl": initial_sl,
                "trail_steps": 0
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
                    process_symbol(symbol, df, price, state[symbol], allow_entry=False)
                elif new_candle:
                    process_symbol(symbol, df, price, state[symbol], allow_entry=True)

            time.sleep(10)

        except Exception as e:
            log(f"Runtime error: {e}")
            time.sleep(5)


if __name__ == "__main__":
    run()