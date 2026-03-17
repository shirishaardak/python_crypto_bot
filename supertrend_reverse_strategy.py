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

TELEGRAM_TOKEN = os.getenv("BOT_TOK")
TELEGRAM_CHAT_ID = os.getenv("CHAT_")
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
        requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": msg
        }, timeout=5)

    except Exception:
        print("Telegram Error:", traceback.format_exc())

# ================= SETTINGS =================

SYMBOLS = ["BTCUSD","ETHUSD"]

DEFAULT_CONTRACTS = {"BTCUSD":100,"ETHUSD":100}
CONTRACT_SIZE = {"BTCUSD":0.001,"ETHUSD":0.01}


stop = {"BTCUSD":200,"ETHUSD":20}

TAKER_FEE = 0.0005
ATR_MULTIPLIER = 1.5

TIMEFRAME = "1m"
DAYS = 1

BASE_DIR = os.getcwd()
SAVE_DIR = os.path.join(BASE_DIR,"data","reverse_atr_bot")
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
        log(f"{symbol} price error: {e}")
        return None

# ================= SAVE =================

def save_trade(trade):
    df = pd.DataFrame([trade])
    df.to_csv(
        TRADE_CSV,
        mode="a",
        header=not os.path.exists(TRADE_CSV),
        index=False
    )

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

    # ATR
    df["ATR"] = ta.atr(df.high, df.low, df.close, length=14)
    ha["ATR"] = df["ATR"]
    ha["ATR_MA"] = ha["ATR"].rolling(21).mean()
    # Trend logic
    ha["UPPER"] = ha["HA_high"].rolling(21).max()
    ha["LOWER"] = ha["HA_low"].rolling(21).min()
    ha["UP"] = ha["HA_high"].rolling(5).max()
    ha["LOW"] = ha["HA_low"].rolling(5).min()

    trendline = np.zeros(len(ha))
    trend = ha["HA_close"].iloc[0]
    trendline[0] = trend

    for i in range(1,len(ha)):

        if ha["HA_high"].iloc[i] == ha["UPPER"].iloc[i]:
            trend = ha["LOW"].iloc[i-1]
        elif ha["HA_low"].iloc[i] == ha["LOWER"].iloc[i]:
            trend = ha["UP"].iloc[i-1]

        trendline[i] = trend

    ha["Trendline"] = trendline

    return ha

# ================= STRATEGY =================

def process_symbol(symbol, df, state):

    ha = build_indicators(df)

    if len(ha) < 50:
        return

    last = ha.iloc[-2]
    prev = ha.iloc[-3]

    price = fetch_price(symbol)
    if price is None:
        return

    pos = state["position"]

    cross_up = last.HA_close > last.Trendline and prev.HA_close <= prev.Trendline and last.ATR < last.ATR_MA
    cross_down = last.HA_close < last.Trendline and prev.HA_close >= prev.Trendline and last.ATR < last.ATR_MA

    candle_time = ha.index[-2]

    # ================= ENTRY (REVERSE) =================

    if pos is None and state["last_candle"] != candle_time:

        atr = last.ATR

        if cross_up:
            # 🔴 SHORT
            state["position"] = {
                "side": "short",
                "entry": price,
                "tsl": price + stop[symbol],
                "qty": DEFAULT_CONTRACTS[symbol],
                "entry_time": datetime.now()
            }

            state["last_candle"] = candle_time
            log(f"{symbol} SHORT {price}")
            send_telegram(f"🔴 {symbol} SHORT {price}")

        elif cross_down:
            # 🟢 LONG
            state["position"] = {
                "side": "long",
                "entry": price,
                "tsl": price - stop[symbol],
                "qty": DEFAULT_CONTRACTS[symbol],
                "entry_time": datetime.now()
            }

            state["last_candle"] = candle_time
            log(f"{symbol} LONG {price}")
            send_telegram(f"🟢 {symbol} LONG {price}")

    # ================= TRAILING =================

    if pos:

        atr = last.ATR

        if pos["side"] == "long":

            # new_tsl = price - atr * ATR_MULTIPLIER
            # pos["tsl"] = max(pos["tsl"], new_tsl)

            # # break-even
            # if price > pos["entry"] + atr:
            #     pos["tsl"] = max(pos["tsl"], pos["entry"])

            if last.HA_close > last.Trendline:
                exit_trade(symbol, price, pos, state)

        elif pos["side"] == "short":

            # new_tsl = price + atr * ATR_MULTIPLIER
            # pos["tsl"] = min(pos["tsl"], new_tsl)

            # # break-even
            # if price < pos["entry"] - atr:
            #     pos["tsl"] = min(pos["tsl"], pos["entry"])

            if last.HA_close < last.Trendline:
                exit_trade(symbol, price, pos, state)

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
        "net_pnl":round(net,6)
    }

    save_trade(trade)

    log(f"{symbol} EXIT {net}")
    send_telegram(f"✅ {symbol} EXIT PnL {round(net,6)}")

    state["position"] = None

# ================= MAIN =================

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
            log(traceback.format_exc())
            send_telegram("⚠️ BOT ERROR")
            time.sleep(10)

if __name__=="__main__":
    run()