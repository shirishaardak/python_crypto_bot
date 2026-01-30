import os
import time
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import pandas_ta as ta
from dotenv import load_dotenv
load_dotenv()

# ================= SETTINGS =================
SYMBOLS = ["BTCUSD", "ETHUSD"]

DEFAULT_CONTRACTS = {"BTCUSD": 100, "ETHUSD": 100}
CONTRACT_SIZE = {"BTCUSD": 0.001, "ETHUSD": 0.01}
TAKER_FEE = 0.0005

TIMEFRAME = "5m"
DAYS = 5

TRADE_TARGET = {"BTCUSD": 100, "ETHUSD": 10}   # Target points per trade
STOP_LOSS    = {"BTCUSD": 300, "ETHUSD": 30}   # Stop loss points

BASE_DIR = os.getcwd()
SAVE_DIR = os.path.join(BASE_DIR, "data", "htc100_bot_terminal")
os.makedirs(SAVE_DIR, exist_ok=True)

TRADE_CSV = os.path.join(SAVE_DIR, "live_trades.csv")

# ================= UTILITIES =================
def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}")

def commission(price, qty, symbol):
    return price * CONTRACT_SIZE[symbol] * qty * TAKER_FEE

def save_trade(trade):
    trade = trade.copy()
    for c in ["entry_time", "exit_time"]:
        trade[c] = trade[c].strftime("%Y-%m-%d %H:%M:%S")
    pd.DataFrame([trade]).to_csv(
        TRADE_CSV,
        mode="a",
        header=not os.path.exists(TRADE_CSV),
        index=False
    )

def save_processed_data(df, symbol):
    path = os.path.join(SAVE_DIR, f"{symbol}_processed.csv")
    df.to_csv(path)

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
    df = df.astype(float)
    return df.sort_index()

# ================= STRATEGY =================
def process_symbol(symbol, df, price, state):
    """
    Hybrid Trend Capture 100 (HTC100)
    EMA9/EMA27 + ATR + ADX + RSI filters
    Fixed 100-point target, stop-loss 300 points
    Terminal logs only
    """
    # INDICATORS
    df["EMA9"]  = ta.ema(df["close"], length=9)
    df["EMA27"] = ta.ema(df["close"], length=27)

    df["ATR"] = ta.atr(df["high"], df["low"], df["close"], length=14)
    df["ATR_EMA"] = df["ATR"].rolling(21).mean()

    df["ADX"] = ta.adx(df["high"], df["low"], df["close"], length=14)["ADX_14"]
    df["ADX_EMA"] = df["ADX"].rolling(21).mean()

    df["RSI"] = ta.rsi(df["close"], length=14)

    save_processed_data(df, symbol)

    last = df.iloc[-2]
    prev = df.iloc[-3]
    pos = state["position"]

    # ENTRY CONDITIONS
    if pos is None:
        if last.ATR > last.ATR_EMA and last.ADX > 25 and last.ADX > last.ADX_EMA:
            # LONG
            if last.EMA9 > last.EMA27 and last.close > prev.close and last.RSI > 50:
                state["position"] = {
                    "side": "long",
                    "entry": price,
                    "stop": price - STOP_LOSS[symbol],
                    "target": price + TRADE_TARGET[symbol],
                    "qty": DEFAULT_CONTRACTS[symbol],
                    "entry_time": datetime.now()
                }
                log(f"📈 {symbol} LONG ENTRY | Price: {price}")
                return
            # SHORT
            if last.EMA9 < last.EMA27 and last.close < prev.close and last.RSI < 50:
                state["position"] = {
                    "side": "short",
                    "entry": price,
                    "stop": price + STOP_LOSS[symbol],
                    "target": price - TRADE_TARGET[symbol],
                    "qty": DEFAULT_CONTRACTS[symbol],
                    "entry_time": datetime.now()
                }
                log(f"📉 {symbol} SHORT ENTRY | Price: {price}")
                return

    # EXIT CONDITIONS
    if pos:
        exit_trade = False
        if pos["side"] == "long" and (price >= pos["target"] or price <= pos["stop"]):
            exit_trade = True
        elif pos["side"] == "short" and (price <= pos["target"] or price >= pos["stop"]):
            exit_trade = True

        if exit_trade:
            pnl = (price - pos["entry"]) if pos["side"] == "long" else (pos["entry"] - price)
            net = pnl * CONTRACT_SIZE[symbol] * pos["qty"] - commission(price, pos["qty"], symbol)

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
            log(f"✅ {symbol} {pos['side'].upper()} EXIT | Price: {price} | Net PnL: {round(net,6)}")
            state["position"] = None

# ================= MAIN =================
def run():
    if not os.path.exists(TRADE_CSV):
        pd.DataFrame(columns=[
            "entry_time", "exit_time", "symbol", "side",
            "entry_price", "exit_price", "qty", "net_pnl"
        ]).to_csv(TRADE_CSV, index=False)

    state = {s: {"position": None} for s in SYMBOLS}

    log("🚀 HTC100 Bot Started (Terminal Logs Only)")

    while True:
        try:
            for symbol in SYMBOLS:
                df = fetch_candles(symbol)
                if len(df) < 50:
                    continue
                price = fetch_price(symbol)
                process_symbol(symbol, df, price, state[symbol])
            time.sleep(20)
        except Exception as e:
            log(f"Error: {e}")
            time.sleep(5)

if __name__ == "__main__":
    run()
