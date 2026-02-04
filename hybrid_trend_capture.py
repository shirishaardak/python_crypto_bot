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
DAYS = 5

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
def calculate_trendline(df):
    data = df.copy()

    # Heikin Ashi
    data["HA_close"] = (data["open"] + data["high"] + data["low"] + data["close"]) / 4
    data["HA_open"] = np.nan
    data.iloc[0, data.columns.get_loc("HA_open")] = (
        data["open"].iloc[0] + data["close"].iloc[0]
    ) / 2

    for i in range(1, len(data)):
        data.iloc[i, data.columns.get_loc("HA_open")] = (
            data["HA_open"].iloc[i - 1] + data["HA_close"].iloc[i - 1]
        ) / 2

    data["HA_high"] = data[["HA_open", "HA_close", "high"]].max(axis=1)
    data["HA_low"] = data[["HA_open", "HA_close", "low"]].min(axis=1)

    # Trendline logic
    high_vals = data["HA_high"].values
    low_vals = data["HA_low"].values

    max_idx = argrelextrema(high_vals, np.greater_equal, order=21)[0]
    min_idx = argrelextrema(low_vals, np.less_equal, order=21)[0]

    data["smoothed_high"] = np.nan
    data["smoothed_low"] = np.nan

    data.iloc[max_idx, data.columns.get_loc("smoothed_high")] = data["HA_high"].iloc[max_idx]
    data.iloc[min_idx, data.columns.get_loc("smoothed_low")] = data["HA_low"].iloc[min_idx]

    data[["smoothed_high", "smoothed_low"]] = data[["smoothed_high", "smoothed_low"]].ffill()

    data["trendline"] = np.nan
    trendline = data["HA_close"].iloc[0]
    data.iloc[0, data.columns.get_loc("trendline")] = trendline

    for i in range(1, len(data)):
        if data["HA_high"].iloc[i] == data["smoothed_high"].iloc[i]:
            trendline = data["HA_low"].iloc[i]
        elif data["HA_low"].iloc[i] == data["smoothed_low"].iloc[i]:
            trendline = data["HA_high"].iloc[i]
        data.iloc[i, data.columns.get_loc("trendline")] = trendline

    return data

# ================= STRATEGY =================
def process_symbol(symbol, df, price, state):
    data = calculate_trendline(df)
    save_processed_data(data, symbol)

    last = data.iloc[-2]
    prev = data.iloc[-3]
    candle_time = data.index[-2]
    pos = state["position"]

    cross_up = prev.HA_close <= prev.trendline and last.HA_close > last.trendline
    cross_down = prev.HA_close >= prev.trendline and last.HA_close < last.trendline

    # ===== ENTRY TIME WINDOW =====
    now_ist = datetime.now()
    entry_window_start = dt_time(4, 0)   # 4 AM IST
    entry_window_end = dt_time(18, 0)    # 6 PM IST
    in_entry_window = entry_window_start <= now_ist.time() <= entry_window_end

    # ===== ENTRY LOGIC =====
    if pos is None and state["last_candle"] != candle_time:
        if cross_up:
            state["position"] = {
                "side": "long",
                "entry": price,
                "stop": price - STOP_LOSS[symbol],
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
                "stop": price + STOP_LOSS[symbol],
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
