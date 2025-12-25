import os
import time
import requests
import pandas as pd
from datetime import datetime, timedelta
import pandas_ta as ta
from scipy.signal import argrelextrema
import numpy as np

# ================= SETTINGS ===============================
SYMBOLS = ["BTCUSD", "ETHUSD"]

DEFAULT_CONTRACTS = {"BTCUSD": 30, "ETHUSD": 30}
CONTRACT_SIZE = {"BTCUSD": 0.001, "ETHUSD": 0.01}
TAKER_FEE = 0.0005

SAVE_DIR = os.path.join(os.getcwd(), "data", "price_trend_following_strategy")
os.makedirs(SAVE_DIR, exist_ok=True)
TRADE_CSV = os.path.join(SAVE_DIR, "live_trades.csv")

# ================= UTILITIES ==============================
def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}")

def commission(price, contracts, symbol):
    notional = price * CONTRACT_SIZE[symbol] * contracts
    return notional * TAKER_FEE

def save_trade_row(trade):
    df = pd.DataFrame([trade])
    if not os.path.exists(TRADE_CSV):
        df.to_csv(TRADE_CSV, index=False)
    else:
        df.to_csv(TRADE_CSV, mode="a", header=False, index=False)

def save_processed_data(df, symbol):
    path = os.path.join(SAVE_DIR, f"{symbol}_processed.csv")
    df_out = pd.DataFrame({
        "time": df.index.astype(str),
        "HA_open": df["HA_open"],
        "HA_high": df["HA_high"],
        "HA_low": df["HA_low"],
        "HA_close": df["HA_close"],
        "Trendline": df["Trendline"]
    })
    df_out.to_csv(path, index=False)

# ================= DATA FETCH =============================
def fetch_ticker_price(symbol):
    url = f"https://api.india.delta.exchange/v2/tickers/{symbol}"
    try:
        r = requests.get(url, timeout=5)
        price = float(r.json()["result"]["mark_price"])
        return price
    except:
        return None

def fetch_candles(symbol, resolution="1h", days=30, tz="Asia/Kolkata"):
    start = int((datetime.now() - timedelta(days=days)).timestamp())
    params = {
        "resolution": resolution,
        "symbol": symbol,
        "start": start,
        "end": int(time.time())
    }

    url = "https://api.india.delta.exchange/v2/history/candles"
    r = requests.get(url, params=params, timeout=10)
    data = r.json().get("result", [])
    if not data:
        return None

    df = pd.DataFrame(data, columns=["time","open","high","low","close","volume"])
    df.columns = df.columns.str.title()

    df["Time"] = pd.to_datetime(df["Time"], unit="s", utc=True).dt.tz_convert(tz)
    df.set_index("Time", inplace=True)
    df = df.sort_index().astype(float)
    df = df[~df.index.duplicated(keep="last")]
    return df

# ================= TRENDLINE (NON-REPAINT) =================
def calculate_trendline(df):

    ha = ta.ha(df["Open"], df["High"], df["Low"], df["Close"])

    # 🔒 CRITICAL FIX: ignore live candle
    ha = ha.iloc[:-1].copy()
    ha.reset_index(drop=True, inplace=True)

    max_idx = argrelextrema(ha["HA_high"].values, np.greater_equal, order=21)[0]
    min_idx = argrelextrema(ha["HA_low"].values, np.less_equal, order=21)[0]

    ha["max_high"] = np.nan
    ha["max_low"] = np.nan

    ha.loc[max_idx, "max_high"] = ha.loc[max_idx, "HA_high"]
    ha.loc[min_idx, "max_low"] = ha.loc[min_idx, "HA_low"]

    ha["max_high"] = ha["max_high"].ffill()
    ha["max_low"] = ha["max_low"].ffill()

    ha["Trendline"] = np.nan

    trendline = ha["HA_high"].iloc[0]
    ha.loc[0, "Trendline"] = trendline

    for i in range(1, len(ha)):
        if ha["HA_high"].iloc[i] == ha["max_high"].iloc[i]:
            trendline = ha["HA_high"].iloc[i]
        elif ha["HA_low"].iloc[i] == ha["max_low"].iloc[i]:
            trendline = ha["HA_low"].iloc[i]
        ha.loc[i, "Trendline"] = trendline

    return ha

# ================= STRATEGY ================================
def process_price_trend(symbol, price, positions, last_close, prev_close, df):

    contracts = DEFAULT_CONTRACTS[symbol]
    size = CONTRACT_SIZE[symbol]

    # 🔒 use confirmed trendline
    trendline = df["Trendline"].iloc[-2]

    # ================= ENTRY =================
    if positions[symbol]["long"] is None:
        if last_close > trendline and last_close > prev_close:
            positions[symbol]["long"] = {
                "entry": price,
                "stop": trendline,
                "contracts": contracts,
                "entry_time": datetime.now(),
                "anchor": trendline
            }
            log(f"{symbol} LONG ENTRY @ {price} | TL {trendline}")

    if positions[symbol]["short"] is None:
        if last_close < trendline and last_close < prev_close:
            positions[symbol]["short"] = {
                "entry": price,
                "stop": trendline,
                "contracts": contracts,
                "entry_time": datetime.now(),
                "anchor": trendline
            }
            log(f"{symbol} SHORT ENTRY @ {price} | TL {trendline}")

    # ================= LONG MGMT =================
    pos = positions[symbol]["long"]
    if pos:
        if df["Trendline"].iloc[-1] == df["HA_high"].iloc[-1]:
            pos["stop"] = max(pos["stop"], df["HA_low"].iloc[-1])

        pos["stop"] = max(pos["stop"], pos["anchor"])

        if price < pos["stop"]:
            pnl = (price - pos["entry"]) * size * pos["contracts"]
            fee = commission(price, pos["contracts"], symbol)
            save_trade_row({
                "symbol": symbol,
                "side": "long",
                "entry_time": pos["entry_time"],
                "exit_time": datetime.now(),
                "entry": pos["entry"],
                "exit": price,
                "net": pnl - fee
            })
            log(f"{symbol} LONG EXIT @ {price}")
            positions[symbol]["long"] = None

    # ================= SHORT MGMT =================
    pos = positions[symbol]["short"]
    if pos:
        if df["Trendline"].iloc[-1] == df["HA_low"].iloc[-1]:
            pos["stop"] = min(pos["stop"], df["HA_high"].iloc[-1])

        pos["stop"] = min(pos["stop"], pos["anchor"])

        if price > pos["stop"]:
            pnl = (pos["entry"] - price) * size * pos["contracts"]
            fee = commission(price, pos["contracts"], symbol)
            save_trade_row({
                "symbol": symbol,
                "side": "short",
                "entry_time": pos["entry_time"],
                "exit_time": datetime.now(),
                "entry": pos["entry"],
                "exit": price,
                "net": pnl - fee
            })
            log(f"{symbol} SHORT EXIT @ {price}")
            positions[symbol]["short"] = None

# ================= MAIN LOOP ===============================
def run_live():

    positions = {s: {"long": None, "short": None} for s in SYMBOLS}

    log("🚀 NON-REPAINT HA TRENDLINE STRATEGY STARTED")

    while True:
        try:
            for symbol in SYMBOLS:
                df = fetch_candles(symbol)
                if df is None or len(df) < 100:
                    continue

                ha = calculate_trendline(df)
                if len(ha) < 5:
                    continue

                save_processed_data(ha, symbol)

                last_close = ha["HA_close"].iloc[-2]
                prev_close = ha["HA_close"].iloc[-3]

                price = fetch_ticker_price(symbol)
                if price is None:
                    continue

                process_price_trend(
                    symbol, price, positions,
                    last_close, prev_close, ha
                )

            time.sleep(20)

        except Exception as e:
            log(f"ERROR: {e}")
            time.sleep(5)

# ================= START ===============================
if __name__ == "__main__":
    run_live()
