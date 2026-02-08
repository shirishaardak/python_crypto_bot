import os
import time
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timedelta, time as dt_time
import pandas_ta as ta
from scipy.signal import argrelextrema
from dotenv import load_dotenv

load_dotenv()

# ================= SETTINGS =================
SYMBOLS = ["BTCUSD", "ETHUSD"]

DEFAULT_CONTRACTS = {"BTCUSD": 100, "ETHUSD": 100}
CONTRACT_SIZE = {"BTCUSD": 0.001, "ETHUSD": 0.01}
TAKER_FEE = 0.0005

TIMEFRAME = "15m"
DAYS = 15

STOP_LOSS = {"BTCUSD": 200, "ETHUSD": 20}
TRAIL_STEP = {"BTCUSD": 100, "ETHUSD": 10}

BASE_DIR = os.getcwd()
SAVE_DIR = os.path.join(BASE_DIR, "data", "hybrid_trend_capture_time")
os.makedirs(SAVE_DIR, exist_ok=True)

TRADE_CSV = os.path.join(SAVE_DIR, "live_trades.csv")

# ================= UTILITIES =================
def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}")

def commission(price, qty, symbol):
    return price * CONTRACT_SIZE[symbol] * qty * TAKER_FEE

def save_trade(trade):
    trade_copy = trade.copy()
    for t in ["entry_time", "exit_time"]:
        trade_copy[t] = trade_copy[t].strftime("%Y-%m-%d %H:%M:%S")

    cols = ["entry_time","exit_time","symbol","side","entry_price","exit_price","qty","net_pnl"]
    pd.DataFrame([trade_copy])[cols].to_csv(
        TRADE_CSV,
        mode="a",
        header=not os.path.exists(TRADE_CSV),
        index=False
    )

def save_processed_data(data, symbol):
    path = os.path.join(SAVE_DIR, f"{symbol}_processed.csv")

    out = pd.DataFrame({
        "time": data.index,
        "HA_open": data["HA_open"],
        "HA_high": data["HA_high"],
        "HA_low": data["HA_low"],
        "HA_close": data["HA_close"],
        "trendline": data["trendline"]
    })

    out.to_csv(path, index=False)

# ================= DATA =================
def fetch_price(symbol):
    r = requests.get(
        f"https://api.india.delta.exchange/v2/tickers/{symbol}",
        timeout=5
    )
    return float(r.json()["result"]["mark_price"])

def fetch_candles(symbol, resolution=TIMEFRAME, days=DAYS, tz="Asia/Kolkata"):
    start = int((datetime.now() - timedelta(days=days)).timestamp())

    r = requests.get(
        "https://api.india.delta.exchange/v2/history/candles",
        params={
            "resolution": resolution,
            "symbol": symbol,
            "start": str(start),
            "end": str(int(time.time()))
        },
        timeout=10
    )

    df = pd.DataFrame(
        r.json()["result"],
        columns=["time", "open", "high", "low", "close", "volume"]
    )

    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True).dt.tz_convert(tz)
    df.set_index("time", inplace=True)

    return df.astype(float).sort_index()

# ================= TRENDLINE =================
def calculate_trendline(df, order=9):
    data = df.copy().reset_index(drop=True)

    # ========= Heikin Ashi =========
    data["HA_close"] = (
        data["open"] + data["high"] + data["low"] + data["close"]
    ) / 4

    ha_open = np.zeros(len(data))
    ha_open[0] = (data["open"].iloc[0] + data["close"].iloc[0]) / 2

    for i in range(1, len(data)):
        ha_open[i] = (ha_open[i - 1] + data["HA_close"].iloc[i - 1]) / 2

    data["HA_open"] = ha_open
    data["HA_high"] = np.maximum.reduce(
        [data["HA_open"], data["HA_close"], data["high"]]
    )
    data["HA_low"] = np.minimum.reduce(
        [data["HA_open"], data["HA_close"], data["low"]]
    )

    # ========= Trend Levels =========
    data["UPPER"] = data["HA_high"].rolling(order).max()
    data["LOWER"] = data["HA_low"].rolling(order).min()

    data["ATR"] = ta.atr(data["HA_high"], data["HA_low"], data["HA_close"], length=14)
    data["ATR_MA"] = data["ATR"].rolling(order).mean()

    # ========= Trendline =========
    trendline = np.zeros(len(data))
    trend = data["HA_close"].iloc[0]
    trendline[0] = trend

    ha_close = data["HA_close"].values
    upper = data["UPPER"].values
    lower = data["LOWER"].values

    for i in range(1, len(data)):
        if not np.isnan(upper[i - 1]) and not np.isnan(lower[i - 1]):

            if ha_close[i] > upper[i - 1] and ha_close[i] > trend:
                trend = lower[i]

            elif ha_close[i] < lower[i - 1] and ha_close[i] < trend:
                trend = upper[i]

        trendline[i] = trend

    data["trendline"] = trendline

    return data

# ================= STRATEGY =================
def process_symbol(symbol, df, price, state):
    data = calculate_trendline(df)
    # save_processed_data(data, symbol)

    last = data.iloc[-2]
    prev = data.iloc[-3]
    candle_time = data.index[-2]
    pos = state["position"]

    cross_up = last.HA_close > prev.HA_open and last.HA_close > prev.HA_close and last.HA_close > last.trendline
    cross_down = last.HA_close < prev.HA_open and last.HA_close < prev.HA_close and last.HA_close < last.trendline

    # ===== ENTRY TIME WINDOW =====
    now_ist = datetime.now()
    entry_window_start = dt_time(16, 0)   # 4 AM IST
    entry_window_end = dt_time(6, 0)    # 6 PM IST
    in_entry_window = entry_window_start <= now_ist.time() <= entry_window_end

    # ===== ENTRY LOGIC =====
    if pos is None and state["last_candle"] != candle_time and last.ATR > last.ATR_MA:
        if cross_up:
            state["position"] = {
                "side": "long",
                "entry": price,
                "stop": price - last.ATR,
                "qty": DEFAULT_CONTRACTS[symbol],
                "entry_time": datetime.now(),
                "last_trail_price": price
            }
            state["last_candle"] = candle_time
            log(f"{symbol} LONG ENTRY @ {price}")
            return

        if cross_down:
            state["position"] = {
                "side": "short",
                "entry": price,
                "stop": price + last.ATR,
                "qty": DEFAULT_CONTRACTS[symbol],
                "entry_time": datetime.now(),
                "last_trail_price": price
            }
            state["last_candle"] = candle_time
            log(f"{symbol} SHORT ENTRY @ {price}")
            return

    # ===== EXIT + TRAILING LOGIC =====
    # Exit can happen anytime, regardless of time window
    if pos:
        step = TRAIL_STEP[symbol]

        if pos["side"] == "long":
            moved = last.HA_close - pos["last_trail_price"]
            if moved >= step:
                steps = int(moved // step)
                pos["stop"] += steps * step
                pos["last_trail_price"] += steps * step

            if last.HA_close < pos['stop']:
                exit_trade(symbol, price, pos, state)

        if pos["side"] == "short":
            moved = pos["last_trail_price"] - last.HA_close
            if moved >= step:
                steps = int(moved // step)
                pos["stop"] -= steps * step
                pos["last_trail_price"] -= steps * step

            if last.HA_close > pos['stop']:
                exit_trade(symbol, price, pos, state)

def exit_trade(symbol, price, pos, state):
    pnl = (
        (price - pos["entry"]) if pos["side"] == "long"
        else (pos["entry"] - price)
    ) * CONTRACT_SIZE[symbol] * pos["qty"]

    net = pnl - commission(price, pos["qty"], symbol)

    save_trade({
        "symbol": symbol,
        "side": pos["side"],
        "entry_price": pos["entry"],
        "exit_price": price,
        "qty": pos["qty"],
        "net_pnl": round(net, 6),
        "entry_time": pos["entry_time"],
        "exit_time": datetime.now()
    })

    log(f"{symbol} {pos['side'].upper()} EXIT | Net PnL: {round(net, 6)}")
    state["position"] = None

# ================= MAIN LOOP =================
def run():
    if not os.path.exists(TRADE_CSV):
        pd.DataFrame(
            columns=["entry_time","exit_time","symbol","side","entry_price","exit_price","qty","net_pnl"]
        ).to_csv(TRADE_CSV, index=False)

    state = {s: {"position": None, "last_candle": None} for s in SYMBOLS}

    log("Heikin Ashi Bot Started (Processed Data + Step Trailing SL Enabled)")

    while True:
        try:
            for symbol in SYMBOLS:
                df = fetch_candles(symbol)
                if len(df) < 100:
                    continue

                price = fetch_price(symbol)
                process_symbol(symbol, df, price, state[symbol])

            time.sleep(5)

        except Exception as e:
            log(f"ERROR: {e}")
            time.sleep(5)

if __name__ == "__main__":
    run()
