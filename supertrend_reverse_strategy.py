import os
import time
import requests
import pandas as pd
import pandas_ta as ta
from datetime import datetime, timedelta
from dotenv import load_dotenv
import traceback
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import numpy as np
import subprocess

load_dotenv()

# ================= SESSION =================

session = requests.Session()

retry = Retry(
    total=5,
    backoff_factor=1,
    status_forcelist=[429,500,502,503,504]
)

adapter = HTTPAdapter(max_retries=retry)
session.mount("https://", adapter)

# ================= TELEGRAM =================

TELEGRAM_TOKEN = os.getenv("TEL_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TEL_CHAT_ID")

_last_tg = {}

BOT_NAME = "supertrend_reverse_strategy"

def send_telegram(msg, key=None, cooldown=30):
    try:
        now = time.time()

        if key and key in _last_tg and now - _last_tg[key] < cooldown:
            return

        if key:
            _last_tg[key] = now

        # ADD BOT NAME PREFIX
        msg = f"{BOT_NAME} | {msg}"

        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

        requests.post(
            url,
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg},
            timeout=5
        )

    except Exception:
        pass

# ================= GIT CONFIG =================


last_git_push = time.time()

def auto_git_push():
    global last_git_push

    if time.time() - last_git_push < 3600:
        return

    try:
        # Stage changes
        subprocess.run("git add -A", shell=True, check=True)

        # Commit only if there are changes
        res = subprocess.run('git diff --cached --quiet || git commit -m "auto update"', shell=True, capture_output=True, text=True)
        if res.returncode == 0:
            log("No changes to commit")
        else:
            log("✅ Changes committed")

        # Push to origin
        res = subprocess.run("git push origin main", shell=True, capture_output=True, text=True)
        if res.returncode == 0:
            log("✅ Git Auto Push Done")
            send_telegram("📤 Git Auto Push Done")
        else:
            log(f"Git Push Failed: {res.stderr}")

        last_git_push = time.time()

    except Exception as e:
        log(f"Git Error: {e}")

# ================= SETTINGS =================

SYMBOLS = ["BTCUSD","ETHUSD"]

DEFAULT_CONTRACTS = {"BTCUSD":100,"ETHUSD":100}
CONTRACT_SIZE = {"BTCUSD":0.001,"ETHUSD":0.01}

stop = {"BTCUSD":200,"ETHUSD":15}
TGT = {"BTCUSD":100,"ETHUSD":10}

TAKER_FEE = 0.0005

TIMEFRAME = "5m"
DAYS = 1
ADX_LENGTH= 14

# ================= SAVE DIRECTORY =================

BASE_DIR = os.getcwd()
SAVE_DIR = os.path.join(BASE_DIR,"data","supertrend_reverse_strategy")

os.makedirs(SAVE_DIR,exist_ok=True)

TRADE_CSV = os.path.join(SAVE_DIR,"live_trades.csv")

# ================= UTIL =================

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

def commission(price,qty,symbol):
    return price * CONTRACT_SIZE[symbol] * qty * TAKER_FEE

# ================= SAFE REQUEST =================

def safe_get(url, params=None):
    try:
        r = session.get(url, params=params, timeout=10)
        if r.status_code != 200:
            return None
        return r.json()
    except:
        return None

# ================= DATA =================

def fetch_price(symbol):
    data = safe_get(f"https://api.india.delta.exchange/v2/tickers/{symbol}")
    try:
        return float(data["result"]["mark_price"])
    except:
        return None

def fetch_candles(symbol):

    start = int((datetime.now()-timedelta(days=DAYS)).timestamp())

    data = safe_get(
        "https://api.india.delta.exchange/v2/history/candles",
        params={
            "resolution":TIMEFRAME,
            "symbol":symbol,
            "start":str(start),
            "end":str(int(time.time()))
        }
    )

    if not data or "result" not in data:
        return pd.DataFrame()

    df = pd.DataFrame(
        data["result"],
        columns=["time","open","high","low","close","volume"]
    )

    if df.empty:
        return df

    df["time"] = pd.to_datetime(df["time"],unit="s")
    df.set_index("time",inplace=True)
    df.sort_index(inplace=True)

    return df.astype(float)

# ================= INDICATORS =================

def build_indicators(df):

    ha = ta.ha(df["open"], df["high"], df["low"], df["close"]).reset_index(drop=True)    
    df["SUPERTREND"] = ta.supertrend(
        high=df["HA_high"],
        low=df["HA_low"],
        close=df["HA_close"],
        length=21,
        multiplier=2.5
    )["SUPERT_21_2.5"]

    ha["UPPER"] = ha["HA_high"].rolling(21).max()
    ha["LOWER"] = ha["HA_low"].rolling(21).min()

    adx = ta.adx(df["HA_high"], df["HA_low"], df["HA_close"], length=ADX_LENGTH)
    df["ADX"] = adx[f"ADX_{ADX_LENGTH}"]
    df["ADX_MA"] = df["ADX"].rolling(5).mean()

    return df

# ================= SAVE TRADE =================

def save_trade(trade):

    trade_copy = trade.copy()

    for t in ["entry_time","exit_time"]:
        trade_copy[t] = trade_copy[t].strftime("%Y-%m-%d %H:%M:%S")

    cols = [
        "entry_time","exit_time","symbol","side",
        "entry_price","exit_price","qty","net_pnl"
    ]

    pd.DataFrame([trade_copy])[cols].to_csv(
        TRADE_CSV,
        mode="a",
        header=not os.path.exists(TRADE_CSV),
        index=False
    )

# ================= EXIT =================

def exit_trade(symbol, price, pos, state):

    pnl = (
        (price-pos["entry"]) if pos["side"]=="long"
        else (pos["entry"]-price)
    ) * CONTRACT_SIZE[symbol] * pos["qty"]

    net = pnl - commission(price,pos["qty"],symbol)

    trade = {
        "entry_time":pos["entry_time"],
        "exit_time":datetime.now(),
        "symbol":symbol,
        "side":pos["side"],
        "entry_price":pos["entry"],
        "exit_price":price,
        "qty":pos["qty"],
        "net_pnl":net
    }

    save_trade(trade)

    log(f"{symbol} EXIT {net}")
    send_telegram(f"✅ {symbol} EXIT PnL {round(net,6)}")

    state["position"] = None

# ================= STRATEGY =================

def process_symbol(symbol, df, state):

    if df.empty or len(df) < 50:
        return

    df = build_indicators(df)

    last = df.iloc[-2]
    prev = df.iloc[-4]

    price = fetch_price(symbol)
    if price is None:
        return

    pos = state["position"]

    cross_up = last.HA_close > last.SUPERTREND and last.HA_close < prev.UPPER and last.ADX > last.ADX_MA
    cross_down = last.HA_close < last.SUPERTREND and last.HA_close > prev.UPPER and last.ADX > last.ADX_MA

    candle_time = df.index[-2]

    if pos is None and state["last_candle"] != candle_time:

        if cross_up:
            state["position"]={
                "symbol":symbol,"side":"long","entry":price,
                "stop":last.SUPERTREND,
                "best_price":price,
                "qty":DEFAULT_CONTRACTS[symbol],
                "entry_time":datetime.now()
            }

            state["last_candle"]=candle_time
            log(f"{symbol} LONG {price}")
            send_telegram(f"🟢 {symbol} LONG {price}")

        elif cross_down:
            state["position"]={
                "symbol":symbol,"side":"short","entry":price,
                "stop":last.SUPERTREND,
                "best_price":price,
                "qty":DEFAULT_CONTRACTS[symbol],
                "entry_time":datetime.now()
            }

            state["last_candle"]=candle_time
            log(f"{symbol} SHORT {price}")
            send_telegram(f"🔴 {symbol} SHORT {price}")

    if pos:
        distance = abs(last.close - last.SUPERTREND)
        if pos["side"] == "long":
            if price > pos["best_price"]:
                pos["best_price"] = price
                pos["stop"] = max(pos["stop"], price - distance)

        elif pos["side"] == "short":
            if price < pos["best_price"]:
                pos["best_price"] = price
                pos["stop"] = min(pos["stop"], price + distance)

        if pos["side"] == "long" and price <= pos["stop"]:
            exit_trade(symbol, price, pos, state)
            return

        if pos["side"] == "short" and price >= pos["stop"]:
            exit_trade(symbol, price, pos, state)
            return

# ================= MAIN =================

def run():

    state={
        s:{"position":None,"last_candle":None}
        for s in SYMBOLS
    }

    log("BOT STARTED")
    send_telegram("🚀 Bot Started")

    while True:

        try:
            for symbol in SYMBOLS:

                df = fetch_candles(symbol)

                if not df.empty:
                    process_symbol(symbol,df,state[symbol])

                time.sleep(1)

            auto_git_push()

            time.sleep(2)

        except Exception:
            log(traceback.format_exc())
            send_telegram("⚠️ BOT ERROR")
            time.sleep(5)

if __name__=="__main__":
    run()