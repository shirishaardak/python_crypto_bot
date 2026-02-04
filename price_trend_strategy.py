import os
import time
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import pandas_ta as ta
from requests.exceptions import RequestException, ConnectionError
from dotenv import load_dotenv
load_dotenv()

# ================= TELEGRAM =================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

_last_tg = {}

def send_telegram(msg, key=None, cooldown=30):
    try:
        now = time.time()
        if key and key in _last_tg and now - _last_tg[key] < cooldown:
            return
        if key:
            _last_tg[key] = now

        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg}, timeout=5)
    except Exception:
        pass

# ================= SETTINGS =================
SYMBOLS = ["BTCUSD", "ETHUSD"]
DEFAULT_CONTRACTS = {"BTCUSD": 100, "ETHUSD": 100}
CONTRACT_SIZE = {"BTCUSD": 0.001, "ETHUSD": 0.01}
TAKER_FEE = 0.0005

TIMEFRAME = "1h"
DAYS = 15

TAKE_PROFIT = {"BTCUSD": 300, "ETHUSD": 30}
STOP_LOSS   = {"BTCUSD": 200, "ETHUSD": 20}

BASE_DIR = os.getcwd()
SAVE_DIR = os.path.join(BASE_DIR, "data", "price_trend_strategy")
os.makedirs(SAVE_DIR, exist_ok=True)

TRADE_CSV = os.path.join(SAVE_DIR, "live_trades.csv")

# ================= UTILITIES =================
def log(msg, tg=False, key=None):
    text = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(text)
    if tg:
        send_telegram(text, key)

def commission(price, qty, symbol):
    notional = price * CONTRACT_SIZE[symbol] * qty
    return notional * TAKER_FEE

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

def save_processed_data(df, ha, symbol):
    path = os.path.join(SAVE_DIR, f"{symbol}_processed.csv")
    out = pd.DataFrame({
        "time": df.index,
        "HA_open": ha["HA_open"],
        "HA_high": ha["HA_high"],
        "HA_low": ha["HA_low"],
        "HA_close": ha["HA_close"],
        "Trendline": ha["Trendline"],
        "ATR": ha["ATR"],
        "ATR_MA": ha["ATR_MA"]
    })
    out.to_csv(path, index=False)

# ================= DATA =================
def fetch_price(symbol):
    try:
        r = requests.get(f"https://api.india.delta.exchange/v2/tickers/{symbol}", timeout=5)
        return float(r.json()["result"]["mark_price"])
    except Exception as e:
        log(f"{symbol} PRICE fetch error: {e}", tg=True, key=f"{symbol}_price")
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
            columns=["time","open","high","low","close","volume"]
        )

        df.rename(columns=str.title, inplace=True)
        df["Time"] = (
            pd.to_datetime(df["Time"], unit="s", utc=True)
              .dt.tz_convert(tz)
        )
        df.set_index("Time", inplace=True)
        df.sort_index(inplace=True)

        df = df.astype(float)
        return df.dropna()

    except Exception as e:
        log(f"{symbol} fetch error: {e}")
        return None

# ================= TRENDLINE =================
def calculate_trendline(df):
    ha = ta.ha(df["Open"], df["High"], df["Low"], df["Close"]).reset_index(drop=True)
    ha["ATR"] = ta.atr(ha["HA_high"], ha["HA_low"], ha["HA_close"], length=14)
    ha["ATR_MA"] = ha["ATR"].rolling(21).mean()
    ha["SUPERTREND"] = ta.supertrend(high=ha["HA_high"], low=ha["HA_low"], close=ha["HA_close"], length=21, multiplier=2.5)["SUPERT_21_2.5"]

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
def process_symbol(symbol, df, price, state):
    ha = calculate_trendline(df)
    # save_processed_data(df, ha, symbol)

    last = ha.iloc[-2]   # last closed candle
    prev = ha.iloc[-3]
    pos  = state["position"]
    now = datetime.now()

    # ===== ENTRY =====
    if pos is None and last.ATR > last.ATR_MA:
        if last.HA_close > last.Trendline and last.HA_close > prev.HA_close and last.HA_close > prev.HA_open and last.HA_close > last.SUPERTREND:
            state["position"] = {
                "side": "long", "entry": price, "stop": price - STOP_LOSS[symbol], "tp": price + TAKE_PROFIT[symbol],
                "qty": DEFAULT_CONTRACTS[symbol], "entry_time": now
            }
            log(f"🟢 {symbol} LONG ENTRY @ {price}", tg=True)
            return

        if last.HA_close < last.Trendline and last.HA_close < prev.HA_close and last.HA_close < prev.HA_open and last.HA_close < last.SUPERTREND:
            state["position"] = {
                "side": "short", "entry": price, "stop": price + STOP_LOSS[symbol], "tp": price - TAKE_PROFIT[symbol],
                "qty": DEFAULT_CONTRACTS[symbol], "entry_time": now
            }
            log(f"🔴 {symbol} SHORT ENTRY @ {price}", tg=True)
            return

    # ===== EXIT =====
    if pos:
        exit_trade = False
        pnl = 0
        if pos["side"] == "long" and last.HA_close < last.Trendline:
            pnl = (price - pos["entry"]) * CONTRACT_SIZE[symbol] * pos["qty"]
            exit_trade = True
        if pos["side"] == "short" and last.HA_close > last.Trendline:
            pnl = (pos["entry"] - price) * CONTRACT_SIZE[symbol] * pos["qty"]
            exit_trade = True

        if exit_trade:
            fee = commission(price, pos["qty"], symbol)
            net = pnl - fee
            save_trade({"symbol": symbol, "side": pos["side"], "entry_price": pos["entry"], "exit_price": price,
                        "qty": pos["qty"], "net_pnl": round(net,6), "entry_time": pos["entry_time"], "exit_time": now})
            emoji = "🟢" if net > 0 else "🔴"
            log(f"{emoji} {symbol} {pos['side'].upper()} EXIT @ {price}\nPNL: {round(net,6)}", tg=True)
            state["position"] = None

# ================= MAIN =================
def run():
    if not os.path.exists(TRADE_CSV):
        pd.DataFrame(columns=["entry_time","exit_time","symbol","side","entry_price","exit_price","qty","net_pnl"]).to_csv(TRADE_CSV, index=False)

    state = {s: {"position": None, "last_candle_time": None} for s in SYMBOLS}
    log("🚀 HA Trendline Breakout Strategy LIVE (Hourly Candle Check)", tg=True)

    while True:
        try:
            for symbol in SYMBOLS:
                df = fetch_candles(symbol)
                if df is None or len(df) < 100:
                    continue

                latest_candle_time = df.index[-1]

                # Only process when a new candle forms
                if state[symbol]["last_candle_time"] == latest_candle_time:
                    continue

                state[symbol]["last_candle_time"] = latest_candle_time

                price = fetch_price(symbol)
                if price is None:
                    continue

                process_symbol(symbol, df, price, state[symbol])

            time.sleep(60)  # check once per minute
        except Exception as e:
            log(f"🚨 Runtime error: {e}", tg=True, key="runtime")
            time.sleep(5)

if __name__ == "__main__":
    run()
