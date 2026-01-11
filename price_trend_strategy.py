import os
import time
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import pandas_ta as ta
from dotenv import load_dotenv
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")


# ================= TELEGRAM =================
TELEGRAM_TOKEN = "PASTE_YOUR_BOT_TOKEN"
TELEGRAM_CHAT_ID = "-100XXXXXXXXXX"

last_alert = {}

def send_telegram(msg, key=None, cooldown=30):
    try:
        now = time.time()
        if key:
            if key in last_alert and now - last_alert[key] < cooldown:
                return
            last_alert[key] = now

        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg}, timeout=5)
    except:
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
    return price * CONTRACT_SIZE[symbol] * qty * TAKER_FEE

def save_trade(trade):
    trade = trade.copy()
    for t in ["entry_time","exit_time"]:
        trade[t] = trade[t].strftime("%Y-%m-%d %H:%M:%S")

    pd.DataFrame([trade]).to_csv(
        TRADE_CSV, mode="a", header=not os.path.exists(TRADE_CSV), index=False
    )

# ================= DATA =================
def fetch_price(symbol):
    try:
        r = requests.get(f"https://api.india.delta.exchange/v2/tickers/{symbol}", timeout=5)
        return float(r.json()["result"]["mark_price"])
    except Exception as e:
        log(f"{symbol} PRICE API ERROR: {e}", tg=True, key=f"{symbol}_price")
        return None

def fetch_candles(symbol):
    start = int((datetime.now() - timedelta(days=DAYS)).timestamp())
    params = {"resolution": TIMEFRAME, "symbol": symbol, "start": start, "end": int(time.time())}

    try:
        r = requests.get("https://api.india.delta.exchange/v2/history/candles", params=params, timeout=10)
        r.raise_for_status()

        df = pd.DataFrame(r.json()["result"], columns=["time","open","high","low","close","volume"])
        df.rename(columns=str.title, inplace=True)
        df["Time"] = pd.to_datetime(df["Time"], unit="s", utc=True)
        df.set_index("Time", inplace=True)
        return df.astype(float)

    except Exception as e:
        log(f"{symbol} API ERROR: {e}", tg=True, key=f"{symbol}_api")
        return None

# ================= TRENDLINE =================
def calculate_trendline(df):
    ha = ta.ha(df["Open"], df["High"], df["Low"], df["Close"])
    ha["ATR"] = ta.atr(ha["HA_high"], ha["HA_low"], ha["HA_close"], 14)
    ha["ATR_MA"] = ha["ATR"].rolling(21).mean()
    ha["SUPERTREND"] = ta.supertrend(ha["HA_high"],ha["HA_low"],ha["HA_close"],21,2.5)['SUPERT_21_2.5']

    order = 21
    ha["UPPER"] = ha["HA_high"].rolling(order).max()
    ha["LOWER"] = ha["HA_low"].rolling(order).min()

    trend = ha["HA_close"].iloc[0]
    ha["Trendline"] = trend

    for i in range(1,len(ha)):
        if ha["HA_high"].iloc[i] == ha["UPPER"].iloc[i]:
            trend = ha["HA_low"].iloc[i]
        elif ha["HA_low"].iloc[i] == ha["LOWER"].iloc[i]:
            trend = ha["HA_high"].iloc[i]
        ha["Trendline"].iloc[i] = trend

    return ha

# ================= STRATEGY =================
def process_symbol(symbol, df, price, state):
    ha = calculate_trendline(df)
    last, prev = ha.iloc[-2], ha.iloc[-3]
    pos = state["position"]

    if pos is None and last.ATR > last.ATR_MA:
        if last.HA_close > last.Trendline and last.HA_close > prev.HA_close and last.HA_close > last.SUPERTREND:
            state["position"] = {"side":"long","entry":price,"qty":DEFAULT_CONTRACTS[symbol],"entry_time":datetime.now()}
            log(f"🟢 {symbol} LONG ENTRY @ {price}", tg=True)

        if last.HA_close < last.Trendline and last.HA_close < prev.HA_close and last.HA_close < last.SUPERTREND:
            state["position"] = {"side":"short","entry":price,"qty":DEFAULT_CONTRACTS[symbol],"entry_time":datetime.now()}
            log(f"🔴 {symbol} SHORT ENTRY @ {price}", tg=True)

    if pos:
        exit = False
        if pos["side"]=="long" and last.HA_close < last.Trendline:
            pnl = (price - pos["entry"]) * CONTRACT_SIZE[symbol] * pos["qty"]
            exit = True
        if pos["side"]=="short" and last.HA_close > last.Trendline:
            pnl = (pos["entry"] - price) * CONTRACT_SIZE[symbol] * pos["qty"]
            exit = True

        if exit:
            net = pnl - commission(price,pos["qty"],symbol)

            save_trade({
                "symbol":symbol,"side":pos["side"],"entry_price":pos["entry"],
                "exit_price":price,"qty":pos["qty"],"net_pnl":round(net,6),
                "entry_time":pos["entry_time"],"exit_time":datetime.now()
            })

            log(f"{symbol} {pos['side'].upper()} EXIT @ {price} | NET {round(net,6)}", tg=True)
            state["position"] = None

# ================= MAIN =================
def run():
    if not os.path.exists(TRADE_CSV):
        pd.DataFrame().to_csv(TRADE_CSV,index=False)

    state = {s:{"position":None} for s in SYMBOLS}
    log("🚀 HA Trendline Bot LIVE", tg=True)

    while True:
        try:
            for s in SYMBOLS:
                df = fetch_candles(s)
                if df is None or len(df)<100:
                    continue
                price = fetch_price(s)
                if price:
                    process_symbol(s,df,price,state[s])
            time.sleep(20)

        except Exception as e:
            log(f"🚨 BOT CRASH: {e}", tg=True, key="crash")
            time.sleep(5)

if __name__ == "__main__":
    run()
