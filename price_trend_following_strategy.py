import os
import time
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import pandas_ta as ta
from scipy.signal import argrelextrema

# ================= SETTINGS =================
SYMBOLS = ["BTCUSD", "ETHUSD"]

DEFAULT_CONTRACTS = {"BTCUSD": 100, "ETHUSD": 100}
CONTRACT_SIZE = {"BTCUSD": 0.001, "ETHUSD": 0.01}
MAX_SL = {"BTCUSD": 500, "ETHUSD": 30}
TAKER_FEE = 0.0005

TIMEFRAME = "15m"
DAYS = 15

BASE_DIR = os.getcwd()
SAVE_DIR = os.path.join(BASE_DIR, "data", "price_trend_following_strategy")
os.makedirs(SAVE_DIR, exist_ok=True)

TRADE_CSV = os.path.join(SAVE_DIR, "live_trades.csv")

# ================= UTILITIES =================
def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}")

def commission(price, qty, symbol):
    notional = price * CONTRACT_SIZE[symbol] * qty
    return notional * TAKER_FEE

def save_trade(trade):
    pd.DataFrame([trade]).to_csv(
        TRADE_CSV,
        mode="a",
        header=not os.path.exists(TRADE_CSV),
        index=False
    )

def save_processed_data(df, ha, symbol):
    path = os.path.join(SAVE_DIR, f"{symbol}_processed.csv")

    out = pd.DataFrame({
        "time": df.index,
        "HA_open": ha["HA_open"],
        "HA_high": ha["HA_high"],
        "HA_low": ha["HA_low"],
        "HA_close": ha["HA_close"],
        "max_high": ha["max_high"],
        "max_low": ha["max_low"],
        "Trendline": ha["Trendline"],
        "ATR": ha["ATR"],
        "ATR_MA": ha["ATR_MA"],
        "ATR_OK": ha["ATR"] > ha["ATR_MA"]
    })

    out.to_csv(path, index=False)

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

def fetch_candles(symbol, resolution=TIMEFRAME, days=DAYS, tz="Asia/Kolkata"):
    start = int((datetime.now() - timedelta(days=days)).timestamp())

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
        r.raise_for_status()

        df = pd.DataFrame(
            r.json()["result"],
            columns=["time", "open", "high", "low", "close", "volume"]
        )
        df.rename(columns=str.title, inplace=True)

        df["Time"] = pd.to_datetime(df["Time"], unit="s", utc=True).dt.tz_convert(tz)
        df.set_index("Time", inplace=True)
        df.sort_index(inplace=True)

        for col in ["Open", "High", "Low", "Close", "Volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")

        return df.dropna()

    except Exception as e:
        log(f"{symbol} fetch error: {e}")
        return None

# ================= TRENDLINE + HA =================
def calculate_trendline(df):
    ha = ta.ha(df["Open"], df["High"], df["Low"], df["Close"])
    ha = ha.reset_index(drop=True)

    order = 21

    max_idx = argrelextrema(
        ha["HA_high"].values, np.greater_equal, order=order
    )[0]
    min_idx = argrelextrema(
        ha["HA_low"].values, np.less_equal, order=order
    )[0]

    ha["max_high"] = np.nan
    ha["max_low"] = np.nan

    ha.loc[max_idx, "max_high"] = ha.loc[max_idx, "HA_high"]
    ha.loc[min_idx, "max_low"] = ha.loc[min_idx, "HA_low"]

    ha[["max_high", "max_low"]] = ha[["max_high", "max_low"]].ffill()

    ha["Trendline"] = np.nan
    trendline = ha["HA_close"].iloc[0]
    ha.loc[0, "Trendline"] = trendline

    for i in range(1, len(ha)):
        if ha.loc[i, "HA_high"] == ha.loc[i, "max_high"]:
            trendline = ha.loc[i, "HA_low"]
        elif ha.loc[i, "HA_low"] == ha.loc[i, "max_low"]:
            trendline = ha.loc[i, "HA_high"]

        ha.loc[i, "Trendline"] = trendline

    return ha

# ================= STRATEGY =================
def process_symbol(symbol, df, price, state):
    ha = calculate_trendline(df)

    # ===== HA ATR FILTER =====
    ha["ATR"] = ta.atr(
        ha["HA_high"],
        ha["HA_low"],
        ha["HA_close"],
        length=14
    )
    ha["ATR_MA"] = ha["ATR"].rolling(21).mean()

    save_processed_data(df, ha, symbol)

    atr = ha["ATR"].iloc[-2]
    atr_ma = ha["ATR_MA"].iloc[-2]

    if atr <= atr_ma:
        return

    last = ha.iloc[-2]
    current = ha.iloc[-1]
    prev = ha.iloc[-3]
    signal_time = df.index[-2]

    pos = state["position"]

    # ===== ENTRY =====
    if pos is None and state["last_trade_time"] != signal_time:

        if last.HA_close > last.Trendline and last.HA_close > prev.HA_open:
            state["position"] = {
                "side": "long",
                "entry": price,
                "stop": max(last.max_low, price - MAX_SL[symbol]),
                "qty": DEFAULT_CONTRACTS[symbol],
                "signal_time": signal_time,
                "entry_time": datetime.now()
            }
            state["last_trade_time"] = signal_time
            log(f"{symbol} LONG ENTRY @ {price}")
            return

        if last.HA_close < last.Trendline and last.HA_close < prev.HA_open:
            state["position"] = {
                "side": "short",
                "entry": price,
                "stop": min(last.max_high, price + MAX_SL[symbol]),
                "qty": DEFAULT_CONTRACTS[symbol],
                "signal_time": signal_time,
                "entry_time": datetime.now()
            }
            state["last_trade_time"] = signal_time
            log(f"{symbol} SHORT ENTRY @ {price}")
            return

    # ===== MANAGEMENT =====
    if pos:
        side = pos["side"]

        if side == "long" and last.HA_high == last.max_high:
            pos["stop"] = max(pos["stop"], last.HA_low)

        if side == "short" and last.HA_low == last.max_low:
            pos["stop"] = min(pos["stop"], last.HA_high)

        exit_trade = False

        if side == "long" and current.HA_close < current.Trendline:
            pnl = (price - pos["entry"]) * CONTRACT_SIZE[symbol] * pos["qty"]
            exit_trade = True

        if side == "short" and current.HA_close > current.Trendline:
            pnl = (pos["entry"] - price) * CONTRACT_SIZE[symbol] * pos["qty"]
            exit_trade = True

        if exit_trade:
            fee = commission(price, pos["qty"], symbol)
            net = pnl - fee

            save_trade({
                "symbol": symbol,
                "side": side,
                "entry_price": pos["entry"],
                "exit_price": price,
                "qty": pos["qty"],
                "net_pnl": round(net, 6),
                "signal_time": pos["signal_time"],
                "entry_time": pos["entry_time"],
                "exit_time": datetime.now()
            })

            log(f"{symbol} {side.upper()} EXIT @ {price} | NET {round(net,6)}")
            state["position"] = None

# ================= MAIN =================
def run():
    if not os.path.exists(TRADE_CSV):
        pd.DataFrame(columns=[
            "symbol", "side",
            "entry_price", "exit_price",
            "qty", "net_pnl",
            "signal_time",
            "entry_time", "exit_time"
        ]).to_csv(TRADE_CSV, index=False)

    state = {s: {"position": None, "last_trade_time": None} for s in SYMBOLS}

    log("🚀 15M HA Trendline + HA-ATR Strategy STARTED")

    while True:
        try:
            for symbol in SYMBOLS:
                df = fetch_candles(symbol)
                if df is None or len(df) < 100:
                    continue

                price = fetch_price(symbol)
                if price is None:
                    continue

                process_symbol(symbol, df, price, state[symbol])

            time.sleep(20)

        except Exception as e:
            log(f"Runtime error: {e}")
            time.sleep(5)

if __name__ == "__main__":
    run()
