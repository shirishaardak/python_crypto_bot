import os
import time
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import pandas_ta as ta
from dotenv import load_dotenv
import traceback
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

load_dotenv()

# ================= SESSION =================

session = requests.Session()

retry = Retry(
    total=5,
    backoff_factor=1,
    status_forcelist=[429,500,502,503,504],
    allowed_methods=["GET"]
)

adapter = HTTPAdapter(max_retries=retry)
session.mount("https://", adapter)
session.mount("http://", adapter)

# ================= TELEGRAM =================

TELEGRAM_TOKEN = os.getenv("BOT_TOKE")
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

        session.post(
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

TGT = {"BTCUSD":200,"ETHUSD":20}
STOPLOSS = {"BTCUSD":100,"ETHUSD":10}

CONTRACT_SIZE = {"BTCUSD":0.001,"ETHUSD":0.01}

TRAIL_STEP = {"BTCUSD":100,"ETHUSD":10}

TAKER_FEE = 0.0005

TIMEFRAME = "1m"
DAYS = 3

BASE_DIR = os.getcwd()
SAVE_DIR = os.path.join(BASE_DIR,"data","hybrid_fast_bot")
os.makedirs(SAVE_DIR,exist_ok=True)

TRADE_CSV = os.path.join(SAVE_DIR,"live_trades.csv")

# ================= UTIL =================

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

def commission(price,qty,symbol):
    return price * CONTRACT_SIZE[symbol] * qty * TAKER_FEE

# ================= TRAILING SL =================

def update_trailing_sl(symbol, price, pos):

    step_size = TRAIL_STEP[symbol]

    if pos["side"] == "long":
        move = price - pos["entry"]
    else:
        move = pos["entry"] - price

    steps_crossed = int(move // step_size)

    if steps_crossed > pos["trail_step"]:

        diff_steps = steps_crossed - pos["trail_step"]

        if pos["side"] == "long":
            pos["stop"] += diff_steps * step_size
        else:
            pos["stop"] -= diff_steps * step_size

        pos["trail_step"] = steps_crossed

        log(f"{symbol} SL Trailed → {pos['stop']}")

# ================= LIVE PRICE =================

def fetch_price(symbol):
    try:
        r = session.get(
            f"https://api.india.delta.exchange/v2/tickers/{symbol}",
            timeout=5
        )
        return float(r.json()["result"]["mark_price"])
    except Exception as e:
        log(f"{symbol} PRICE error: {e}")
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
    try:
        start = int((datetime.now()-timedelta(days=DAYS)).timestamp())

        r = session.get(
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

    except:
        return pd.DataFrame()

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

    ha["UPPER"] = ha["HA_high"].rolling(42).max()
    ha["LOWER"] = ha["HA_low"].rolling(42).min()

    trendline = np.zeros(len(ha))
    trend = ha["HA_close"].iloc[0]

    for i in range(len(ha)):
        if ha["HA_high"].iloc[i] == ha["UPPER"].iloc[i]:
            trend = ha["UPPER"].iloc[i]
        elif ha["HA_low"].iloc[i] == ha["LOWER"].iloc[i]:
            trend = ha["LOWER"].iloc[i]

        trendline[i] = trend

    ha["Trendline"] = trendline

    adx = ta.adx(df["high"], df["low"], df["close"], length=14)
    ha["ADX"] = adx["ADX_14"]

    return ha

# ================= EXIT =================

def exit_trade(symbol, price, pos, state, candle_time):

    pnl = (
        (price-pos["entry"]) if pos["side"]=="long"
        else (pos["entry"]-price)
    ) * CONTRACT_SIZE[symbol] * pos["qty"]

    net = pnl - commission(price,pos["qty"],symbol)

    save_trade({
        "entry":pos["entry"],
        "exit":price,
        "symbol":symbol,
        "side":pos["side"],
        "pnl":net
    })

    log(f"{symbol} EXIT {net}")
    send_telegram(f"EXIT {symbol} PnL {net}")

    state["position"] = None
    state["last_candle"] = candle_time

# ================= STRATEGY =================

def process_symbol(symbol, df, state):

    if df.empty:
        return

    ha = build_indicators(df)

    if len(ha) < 50:
        return

    last = ha.iloc[-2]
    prev = ha.iloc[-3]

    price = fetch_price(symbol)
    if price is None:
        return

    candle_time = ha.index[-2]

    if state["position"] is None and state["last_candle"] != candle_time:

        if last.HA_close > last.Trendline and last.HA_close > prev.HA_close:

            state["position"] = {
                "side":"long",
                "entry":price,
                "stop":price - STOPLOSS[symbol],
                "TGT":price + TGT[symbol],
                "qty":DEFAULT_CONTRACTS[symbol],
                "trail_step":0,
                "entry_time":datetime.now()
            }

            log(f"{symbol} LONG {price}")

        elif last.HA_close < last.Trendline and last.HA_close < prev.HA_close:

            state["position"] = {
                "side":"short",
                "entry":price,
                "stop":price + STOPLOSS[symbol],
                "TGT":price - TGT[symbol],
                "qty":DEFAULT_CONTRACTS[symbol],
                "trail_step":0,
                "entry_time":datetime.now()
            }

            log(f"{symbol} SHORT {price}")

    if state["position"]:

        pos = state["position"]

        # 🔥 TRAILING SL
        update_trailing_sl(symbol, price, pos)

        if pos["side"] == "long":
            if price < pos["stop"]:
                exit_trade(symbol, price, pos, state, candle_time)
        else:
            if price > pos["stop"]:
                exit_trade(symbol, price, pos, state, candle_time)

# ================= MAIN =================

def run():

    state = {
        s:{"position":None,"last_candle":None}
        for s in SYMBOLS
    }

    log("BOT STARTED")

    while True:
        try:
            for symbol in SYMBOLS:
                df = fetch_candles(symbol)
                if len(df) < 50:
                    continue
                process_symbol(symbol, df, state[symbol])

            time.sleep(20)

        except Exception:
            log(traceback.format_exc())
            time.sleep(20)

if __name__=="__main__":
    run()