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

def send_telegram(msg, key=None, cooldown=30):
    try:
        if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
            return

        now = time.time()

        if key:
            if key in _last_tg and now - _last_tg[key] < cooldown:
                return
            _last_tg[key] = now

        session.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID,"text": msg},
            timeout=5
        )
    except:
        print("Telegram Error")

# ================= SETTINGS =================

SYMBOLS = ["BTCUSD","ETHUSD"]

DEFAULT_CONTRACTS = {"BTCUSD":100,"ETHUSD":100}
CONTRACT_SIZE = {"BTCUSD":0.001,"ETHUSD":0.01}

stop = {"BTCUSD":70,"ETHUSD":7}

TAKER_FEE = 0.0005

TIMEFRAME = "1m"
DAYS = 1

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

    data = safe_get(
        f"https://api.india.delta.exchange/v2/tickers/{symbol}"
    )

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

# ================= HEIKIN ASHI =================

def calculate_heikin_ashi(df):

    ha = pd.DataFrame(index=df.index)

    ha["HA_close"] = (df.open+df.high+df.low+df.close)/4

    ha_open = [(df.open.iloc[0]+df.close.iloc[0])/2]

    for i in range(1,len(df)):
        ha_open.append(
            (ha_open[i-1]+ha["HA_close"].iloc[i-1])/2
        )

    ha["HA_open"] = ha_open

    ha["HA_high"] = ha[["HA_open","HA_close"]].join(df.high).max(axis=1)

    ha["HA_low"] = ha[["HA_open","HA_close"]].join(df.low).min(axis=1)

    return ha

# ================= INDICATORS =================

def build_indicators(df):

    ha = calculate_heikin_ashi(df)

    st = ta.supertrend(
        high=ha["HA_high"],
        low=ha["HA_low"],
        close=ha["HA_close"],
        length=7,
        multiplier=2.5
    )

    ha["SUPERTREND"] = st["SUPERT_7_2.5"]

    return ha

# ================= SAVE TRADE =================

def save_trade(trade):

    trade_copy = trade.copy()

    for t in ["entry_time","exit_time"]:
        trade_copy[t] = trade_copy[t].strftime("%Y-%m-%d %H:%M:%S")

    cols = [
        "entry_time",
        "exit_time",
        "symbol",
        "side",
        "entry_price",
        "exit_price",
        "qty",
        "net_pnl"
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

    ha = build_indicators(df)

    last = ha.iloc[-2]
    prev = ha.iloc[-3]

    price = fetch_price(symbol)

    if price is None:
        return

    pos = state["position"]

    cross_up = last.HA_close > last.SUPERTREND and prev.HA_close <= prev.SUPERTREND
    cross_down = last.HA_close < last.SUPERTREND and prev.HA_close >= prev.SUPERTREND

    if abs(last.HA_close-last.SUPERTREND) < (0.0006*price):
        return

    body = abs(last.HA_close-last.HA_open)
    rng = last.HA_high-last.HA_low

    strong = body > (0.6*rng)

    candle_time = ha.index[-2]

    if pos is None and state["last_candle"] != candle_time:

        if cross_up and strong:

            state["position"]={
                "symbol":symbol,
                "side":"long",
                "entry":price,
                "tsl":price-stop[symbol],
                "best_price":price,
                "qty":DEFAULT_CONTRACTS[symbol],
                "entry_time":datetime.now()
            }

            state["last_candle"]=candle_time

            log(f"{symbol} LONG {price}")

            send_telegram(f"🟢 {symbol} LONG {price}")

        elif cross_down and strong:

            state["position"]={
                "symbol":symbol,
                "side":"short",
                "entry":price,
                "tsl":price+stop[symbol],
                "best_price":price,
                "qty":DEFAULT_CONTRACTS[symbol],
                "entry_time":datetime.now()
            }

            state["last_candle"]=candle_time

            log(f"{symbol} SHORT {price}")

            send_telegram(f"🔴 {symbol} SHORT {price}")

    if pos:

        move_step = stop[symbol]*0.5

        if pos["side"]=="long":

            if price>pos["best_price"]:
                pos["best_price"]=price

            profit_move = pos["best_price"]-pos["entry"]

            if profit_move>stop[symbol]:

                pos["tsl"]=max(
                    pos["tsl"],
                    pos["entry"]+(profit_move-move_step)
                )

            if price<=pos["tsl"] or last.HA_close<last.SUPERTREND:
                exit_trade(symbol,price,pos,state)

        else:

            if price<pos["best_price"]:
                pos["best_price"]=price

            profit_move = pos["entry"]-pos["best_price"]

            if profit_move>stop[symbol]:

                pos["tsl"]=min(
                    pos["tsl"],
                    pos["entry"]-(profit_move-move_step)
                )

            if price>=pos["tsl"] or last.HA_close>last.SUPERTREND:
                exit_trade(symbol,price,pos,state)

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

            time.sleep(2)

        except Exception:

            log(traceback.format_exc())

            send_telegram("⚠️ BOT ERROR")

            time.sleep(5)

if __name__=="__main__":
    run()