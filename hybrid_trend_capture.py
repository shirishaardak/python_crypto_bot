import os
import time
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import pandas_ta as ta
from dotenv import load_dotenv
import traceback

load_dotenv()

# ================= TELEGRAM =================

TELEGRAM_TOKEN = os.getenv("BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("CHAT_ID")
_last_tg = {}

def send_telegram(msg, key=None, cooldown=30):
    try:
        if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
            return

        now = time.time()

        if key:
            if key in _last_tg and now - _last_tg[key] < cooldown:
                return
            _last_tg[key] = now

        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

        requests.post(
            url,
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": msg,
                "parse_mode": "Markdown"
            },
            timeout=5
        )

    except Exception:
        print("Telegram Error:", traceback.format_exc())


# ================= SETTINGS =================

SYMBOLS = ["BTCUSD","ETHUSD"]

DEFAULT_CONTRACTS = {"BTCUSD":100,"ETHUSD":100}

STOP = {"BTCUSD":150,"ETHUSD":15}

CONTRACT_SIZE = {"BTCUSD":0.001,"ETHUSD":0.01}

TAKER_FEE = 0.0005

TIMEFRAME = "1m"
DAYS = 5

BASE_DIR = os.getcwd()
SAVE_DIR = os.path.join(BASE_DIR,"data","hybrid_fast_bot")
os.makedirs(SAVE_DIR,exist_ok=True)

TRADE_CSV = os.path.join(SAVE_DIR,"live_trades.csv")


# ================= UTIL =================

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


def commission(price,qty,symbol):
    return price * CONTRACT_SIZE[symbol] * qty * TAKER_FEE


# ================= LIVE PRICE =================

def fetch_price(symbol):
    try:
        r = requests.get(
            f"https://api.india.delta.exchange/v2/tickers/{symbol}",
            timeout=5
        )
        return float(r.json()["result"]["mark_price"])

    except Exception as e:
        log(f"{symbol} PRICE fetch error: {e}")
        send_telegram(f"⚠️ {symbol} price fetch error")
        return None


# ================= SAVE TRADE =================

def save_trade(trade):
    df = pd.DataFrame([trade])

    df.to_csv(
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
        "trendline": ha["Trendline"],
    })
    out.to_csv(path, index=False)
# ================= DATA =================

def fetch_candles(symbol):

    start = int((datetime.now()-timedelta(days=DAYS)).timestamp())

    r = requests.get(
        "https://api.india.delta.exchange/v2/history/candles",
        params={
            "resolution":TIMEFRAME,
            "symbol":symbol,
            "start":str(start),
            "end":str(int(time.time()))
        },
        timeout=10
    )

    data = r.json()["result"]

    df = pd.DataFrame(
        data,
        columns=["time","open","high","low","close","volume"]
    )

    df["time"] = pd.to_datetime(df["time"],unit="s")
    df.set_index("time",inplace=True)
    df.sort_index(inplace=True)

    return df.astype(float)


# ================= HEIKIN ASHI =================

def calculate_heikin_ashi(df):

    ha = pd.DataFrame(index=df.index)

    ha["HA_close"] = (df.open+df.high+df.low+df.close)/4

    ha_open = [(df.open.iloc[0]+df.close.iloc[0])/2]

    for i in range(1,len(df)):
        ha_open.append((ha_open[i-1]+ha["HA_close"].iloc[i-1])/2)

    ha["HA_open"] = ha_open

    ha["HA_high"] = ha[["HA_open","HA_close"]].join(df.high).max(axis=1)
    ha["HA_low"] = ha[["HA_open","HA_close"]].join(df.low).min(axis=1)

    return ha


# ================= INDICATORS =================

def build_indicators(df):

    ha = calculate_heikin_ashi(df)

    # === RANGE CHANNEL ===
    ha["UPPER"] = ha["HA_high"].rolling(21).max()
    ha["LOWER"] = ha["HA_low"].rolling(21).min()

    # === TRENDLINE LOGIC ===
    trendline = np.zeros(len(ha))
    trend = ha["HA_close"].iloc[0]
    trendline[0] = trend

    for i in range(1, len(ha)):
        ha_close = ha["HA_close"].iloc[i]
        ha_high = ha["HA_high"].iloc[i]
        ha_low = ha["HA_low"].iloc[i]

        upper = ha["UPPER"].iloc[i-1]
        lower = ha["LOWER"].iloc[i-1]

        if ha_high > upper and ha_close > trend:
            trend = lower
        elif ha_low < lower and ha_close < trend:
            trend = upper

        trendline[i] = trend

    ha["Trendline"] = trendline

    # === ADD ADX ===
    adx = ta.adx(df["high"], df["low"], df["close"], length=14)

    # Merge ADX into HA dataframe
    ha["ADX"] = adx["ADX_14"]
    ha["+DI"] = adx["DMP_14"]
    ha["-DI"] = adx["DMN_14"]

    return ha

# ================= EXIT =================

def exit_trade(symbol, price, pos, state, candle_time):

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
        "net_pnl":round(net,6)
    }

    save_trade(trade)

    log(f"{symbol} EXIT {net}")

    send_telegram(
        f"✅ {symbol} {pos['side']} EXIT\nPnL {round(net,6)}"
    )

    # 🔥 FIX: block same candle re-entry
    state["position"] = None
    state["last_candle"] = candle_time


# ================= STRATEGY =================

def process_symbol(symbol, df, state):

    ha = build_indicators(df)
    save_processed_data(df, ha, symbol)

    if len(ha) < 50:
        return

    last = ha.iloc[-2]
    prev = ha.iloc[-3]

    price = fetch_price(symbol)
    if price is None:
        return

    candle_time = ha.index[-2]

    # ✅ ENTRY
    if state["position"] is None and state["last_candle"] != candle_time:

        # Strong candle filter (optional but powerful)
        body = abs(last.HA_close - last.HA_open)
        range_ = last.HA_high - last.HA_low
        strong_candle = body > 0.6 * range_

        cross_up = last.HA_close > last.Trendline and last.HA_close > prev.HA_close and last.HA_close > prev.HA_open and last.ADX > 30 
        cross_down = last.HA_close < last.Trendline and last.HA_close < prev.HA_close and last.HA_close < prev.HA_open and last.ADX > 30

        if cross_up:

            state["position"] = {
                "side":"long",
                "entry":price,
                "stop":last.Trendline - STOP[symbol],
                "qty":DEFAULT_CONTRACTS[symbol],
                "entry_time":datetime.now()
            }

            state["last_candle"] = candle_time

            log(f"{symbol} LONG {price}")
            send_telegram(f"🟢 {symbol} LONG {price}")

        elif cross_down:

            state["position"] = {
                "side":"short",
                "entry":price,
                "stop":last.Trendline + STOP[symbol],
                "qty":DEFAULT_CONTRACTS[symbol],
                "entry_time":datetime.now()
            }

            state["last_candle"] = candle_time

            log(f"{symbol} SHORT {price}")
            send_telegram(f"🔴 {symbol} SHORT {price}")

    # ✅ EXIT (safe reference)
    if state["position"]:

        pos = state["position"]

        if pos["side"] == "long":
            if last.HA_close < last.Trendline or price < pos["stop"]:
                exit_trade(symbol, price, pos, state, candle_time)

        else:
            if last.HA_close > last.Trendline or price > pos["stop"]:
                exit_trade(symbol, price, pos, state, candle_time)


# ================= MAIN LOOP =================

def run():

    state = {
        s:{"position":None,"last_candle":None}
        for s in SYMBOLS
    }

    log("BOT STARTED")
    send_telegram("🚀 Bot Started")

    while True:

        try:

            for symbol in SYMBOLS:

                df = fetch_candles(symbol)

                if len(df) < 50:
                    continue

                process_symbol(symbol, df, state[symbol])

            time.sleep(10)

        except Exception:

            log("ERROR")
            log(traceback.format_exc())
            send_telegram("⚠️ BOT ERROR")
            time.sleep(10)


if __name__=="__main__":
    run()