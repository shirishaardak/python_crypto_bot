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

        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg},
            timeout=5
        )

    except:
        print("Telegram Error")

# ================= SETTINGS =================

SYMBOLS = ["BTCUSD","ETHUSD"]

DEFAULT_CONTRACTS = {"BTCUSD":100,"ETHUSD":100}
CONTRACT_SIZE = {"BTCUSD":0.001,"ETHUSD":0.01}

TAKER_FEE = 0.0005
TIMEFRAME = "5m"   # ✅ CHANGED
DAYS = 5

ATR_MULTIPLIER = 1.5  # ✅ NEW (Dynamic Stop)

BASE_DIR = os.getcwd()
SAVE_DIR = os.path.join(BASE_DIR,"data","pro_bot")
os.makedirs(SAVE_DIR,exist_ok=True)

TRADE_CSV = os.path.join(SAVE_DIR,"live_trades.csv")

# ================= UTIL =================

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

def commission(price,qty,symbol):
    return price * CONTRACT_SIZE[symbol] * qty * TAKER_FEE

# ================= PRICE =================

def fetch_price(symbol):
    try:
        r = requests.get(f"https://api.india.delta.exchange/v2/tickers/{symbol}", timeout=5)
        return float(r.json()["result"]["mark_price"])
    except:
        return None

# ================= ORDER FLOW =================

def fetch_orderbook(symbol):
    try:
        symbol_map = {"BTCUSD": "BTCUSDT", "ETHUSD": "ETHUSDT"}
        s = symbol_map[symbol]

        r = requests.get(
            f"https://api.binance.com/api/v3/depth?symbol={s}&limit=20",
            timeout=5
        )

        data = r.json()
        bid_vol = sum(float(b[1]) for b in data["bids"])
        ask_vol = sum(float(a[1]) for a in data["asks"])

        if bid_vol + ask_vol == 0:
            return 0.5

        return bid_vol / (bid_vol + ask_vol)

    except:
        return 0.5


def fetch_trade_flow(symbol):
    try:
        symbol_map = {"BTCUSD": "BTCUSDT", "ETHUSD": "ETHUSDT"}
        s = symbol_map[symbol]

        r = requests.get(
            f"https://api.binance.com/api/v3/trades?symbol={s}&limit=100",
            timeout=5
        )

        trades = r.json()

        buy, sell = 0, 0

        for t in trades:
            qty = float(t["qty"])
            if t["isBuyerMaker"]:
                sell += qty
            else:
                buy += qty

        return buy - sell

    except:
        return 0

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

    df = pd.DataFrame(
        r.json()["result"],
        columns=["time","open","high","low","close","volume"]
    )

    df["time"] = pd.to_datetime(df["time"],unit="s")
    df.set_index("time",inplace=True)

    return df.astype(float).sort_index()

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

    ha["ATR"] = ta.atr(ha["HA_high"], ha["HA_low"], ha["HA_close"], length=14)
    ha["ATR_MA"] = ha["ATR"].rolling(21).mean()

    ha["UPPER"] = ha["HA_high"].rolling(96).max()
    ha["LOWER"] = ha["HA_low"].rolling(96).min()
    ha["UP"] = ha["HA_high"].rolling(7).max()
    ha["LOW"] = ha["HA_low"].rolling(7).min()

    trendline = np.zeros(len(ha))
    trend = ha["HA_close"].iloc[0]

    for i in range(len(ha)):
        if i == 0:
            trendline[i] = trend
            continue

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

    imbalance = fetch_orderbook(symbol)
    delta = fetch_trade_flow(symbol)

    log(f"{symbol} P:{price} IMB:{round(imbalance,2)} DELTA:{round(delta,2)}")

    pos = state["position"]

    cross_up = (
        last.HA_close > last.Trendline and
        last.HA_close > prev.HA_close and
        last.HA_close > prev.HA_open and
        last.ATR > last.ATR_MA and
        imbalance > 0.6 and
        delta > 0
    )

    cross_down = (
        last.HA_close < last.Trendline and
        last.HA_close < prev.HA_close and
        last.HA_close < prev.HA_open and
        last.ATR > last.ATR_MA and
        imbalance < 0.4 and
        delta < 0
    )

    candle_time = ha.index[-2]

    # ================= ENTRY =================
    if pos is None and state["last_candle"] != candle_time:

        if cross_up:
            state["position"] = {
                "side":"long",
                "entry":price,
                "qty":DEFAULT_CONTRACTS[symbol],
                "entry_time":time.time(),
                "highest":price,
                "lowest":price,
                "stop": price - (last.ATR * ATR_MULTIPLIER)  # ✅ ATR STOP
            }

            state["last_candle"] = candle_time
            log(f"{symbol} LONG {price}")
            send_telegram(f"🟢 {symbol} LONG {price}")

        elif cross_down:
            state["position"] = {
                "side":"short",
                "entry":price,
                "qty":DEFAULT_CONTRACTS[symbol],
                "entry_time":time.time(),
                "highest":price,
                "lowest":price,
                "stop": price + (last.ATR * ATR_MULTIPLIER)  # ✅ ATR STOP
            }

            state["last_candle"] = candle_time
            log(f"{symbol} SHORT {price}")
            send_telegram(f"🔴 {symbol} SHORT {price}")

    # ================= EXIT =================
    if pos:

        if pos["side"] == "long":

            pos["highest"] = max(pos["highest"], price)

            # ✅ ATR TRAILING
            new_stop = pos["highest"] - (last.ATR * ATR_MULTIPLIER)
            pos["stop"] = max(pos["stop"], new_stop)

            if price <= pos["stop"]:
                log(f"{symbol} EXIT LONG")
                exit_trade(symbol, price, pos, state)

        else:

            pos["lowest"] = min(pos["lowest"], price)

            # ✅ ATR TRAILING
            new_stop = pos["lowest"] + (last.ATR * ATR_MULTIPLIER)
            pos["stop"] = min(pos["stop"], new_stop)

            if price >= pos["stop"]:
                log(f"{symbol} EXIT SHORT")
                exit_trade(symbol, price, pos, state)

# ================= EXIT =================

def exit_trade(symbol, price, pos, state):

    pnl = (
        (price-pos["entry"]) if pos["side"]=="long"
        else (pos["entry"]-price)
    ) * CONTRACT_SIZE[symbol] * pos["qty"]

    net = pnl - commission(price,pos["qty"],symbol)

    trade = {
        "entry_time":datetime.fromtimestamp(pos["entry_time"]),
        "exit_time":datetime.now(),
        "symbol":symbol,
        "side":pos["side"],
        "entry_price":pos["entry"],
        "exit_price":price,
        "qty":pos["qty"],
        "net_pnl":round(net,6)
    }

    pd.DataFrame([trade]).to_csv(
        TRADE_CSV,
        mode="a",
        header=not os.path.exists(TRADE_CSV),
        index=False
    )

    log(f"{symbol} EXIT PnL {net}")
    send_telegram(f"✅ {symbol} EXIT PnL {round(net,6)}")

    state["position"] = None

# ================= MAIN =================

def run():

    state = {
        s:{"position":None,"last_candle":None}
        for s in SYMBOLS
    }

    log("BOT STARTED")
    send_telegram("🚀 BOT STARTED")

    while True:
        try:
            for symbol in SYMBOLS:

                df = fetch_candles(symbol)

                if len(df) < 50:
                    continue

                process_symbol(symbol, df, state[symbol])

            time.sleep(1)

        except Exception:
            log(traceback.format_exc())
            send_telegram("⚠️ ERROR")
            time.sleep(2)

if __name__ == "__main__":
    run()