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
TELEGRAM_TOKEN = os.getenv("BOT_TO")
TELEGRAM_CHAT_ID = os.getenv("CHAT")
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
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"},
            timeout=5
        )
    except Exception:
        print("Telegram Error:", traceback.format_exc())


# ================= SETTINGS =================
SYMBOLS = ["BTCUSD", "ETHUSD"]

DEFAULT_CONTRACTS = {"BTCUSD": 100, "ETHUSD": 100}
CONTRACT_SIZE = {"BTCUSD": 0.001, "ETHUSD": 0.01}

TAKER_FEE = 0.0005

TIMEFRAME = "1m"
DAYS = 15

STOP_LOSS = {"BTCUSD": 100, "ETHUSD": 10}
TRAIL_STEP = {"BTCUSD": 50, "ETHUSD": 5}

ADX_LENGTH = 21
ADX_THRESHOLD = 30

BASE_DIR = os.getcwd()
SAVE_DIR = os.path.join(BASE_DIR, "data", "supertrend_reverse_strategy")
os.makedirs(SAVE_DIR, exist_ok=True)

TRADE_CSV = os.path.join(SAVE_DIR, "live_trades.csv")


# ================= UTIL =================
def log(msg):
    print(f"[{datetime.now()}] {msg}")

def commission(price, qty, symbol):
    return price * CONTRACT_SIZE[symbol] * qty * TAKER_FEE


def save_trade(trade):
    try:
        trade_copy = trade.copy()

        for t in ["entry_time", "exit_time"]:
            if isinstance(trade_copy.get(t), datetime):
                trade_copy[t] = trade_copy[t].strftime("%Y-%m-%d %H:%M:%S")

        cols = ["entry_time","exit_time","symbol","side",
                "entry_price","exit_price","qty","net_pnl"]

        df = pd.DataFrame([trade_copy])[cols]

        df.to_csv(
            TRADE_CSV,
            mode="a",
            header=not os.path.exists(TRADE_CSV),
            index=False
        )

    except Exception:
        log(traceback.format_exc())


# ================= DATA =================
def fetch_price(symbol):
    r = requests.get(
        f"https://api.india.delta.exchange/v2/tickers/{symbol}",
        timeout=5
    )
    return float(r.json()["result"]["mark_price"])


def fetch_candles(symbol, resolution=TIMEFRAME, days=DAYS):
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
        columns=["time","open","high","low","close","volume"]
    )

    df["time"] = pd.to_datetime(df["time"], unit="s")
    df.set_index("time", inplace=True)

    return df.astype(float).sort_index()


# ================= TRENDLINE =================
def calculate_trendline(df, order=21):

    data = df.copy().reset_index(drop=True)

    data["HA_close"] = (data.open + data.high + data.low + data.close)/4

    ha_open = np.zeros(len(data))
    ha_open[0] = (data.open.iloc[0] + data.close.iloc[0]) / 2

    for i in range(1,len(data)):
        ha_open[i] = (ha_open[i-1] + data.HA_close.iloc[i-1]) / 2

    data["HA_open"] = ha_open

    data["HA_high"] = np.maximum.reduce(
        [data.HA_open,data.HA_close,data.high]
    )

    data["HA_low"] = np.minimum.reduce(
        [data.HA_open,data.HA_close,data.low]
    )

    data["UPPER"] = data.HA_high.rolling(order).max()
    data["LOWER"] = data.HA_low.rolling(order).min()

    data["UPPER_SL"] = data.HA_high.rolling(5).max()
    data["LOWER_SL"] = data.HA_low.rolling(5).min()

    trend = data.HA_close.iloc[0]
    data["trendline"] = trend

    for i in range(1,len(data)):
        if data.HA_high.iloc[i] == data.UPPER.iloc[i]:
            trend = data.LOWER_SL.iloc[i]
        elif data.HA_low.iloc[i] == data.LOWER.iloc[i]:
            trend = data.UPPER_SL.iloc[i]

        data.loc[i,"trendline"] = trend

    return data


# ================= STRATEGY =================
def process_symbol(symbol, df, price, state):

    data = calculate_trendline(df)

    if len(data) < 50:
        return

    # ADD ADX
    adx = ta.adx(df["high"], df["low"], df["close"], length=ADX_LENGTH)
    df["ADX"] = adx[f"ADX_{ADX_LENGTH}"]

    trend_strength = df["ADX"].iloc[-2]

    # TREND FILTER
    if trend_strength < ADX_THRESHOLD:
        return

    last = data.iloc[-2]
    prev = data.iloc[-3]
    candle_time = df.index[-2]

    pos = state["position"]

    cross_up = last.HA_close > last.trendline and last.HA_close > prev.HA_open
    cross_down = last.HA_close < last.trendline and last.HA_close < prev.HA_open


    # ENTRY (REVERSED)
    if pos is None and state["last_candle"] != candle_time:

        trend_sl = last.trendline

        if cross_up:
            sl = max(price + STOP_LOSS[symbol], trend_sl)

            state["position"] = {
                "side":"long",
                "entry":price,
                "stop":sl,
                "qty":DEFAULT_CONTRACTS[symbol],
                "entry_time":datetime.now(),
                "last_trail_price":price
            }

            state["last_candle"] = candle_time
            log(f"{symbol} SHORT ENTRY {price}")

            send_telegram(f"🔴 {symbol} SHORT\nPrice:{price}\nADX:{round(trend_strength,2)}")
            return


        if cross_down:
            sl = min(price - STOP_LOSS[symbol], trend_sl)

            state["position"] = {
                "side":"short",
                "entry":price,
                "stop":sl,
                "qty":DEFAULT_CONTRACTS[symbol],
                "entry_time":datetime.now(),
                "last_trail_price":price
            }

            state["last_candle"] = candle_time
            log(f"{symbol} LONG ENTRY {price}")

            send_telegram(f"🟢 {symbol} LONG\nPrice:{price}\nADX:{round(trend_strength,2)}")
            return


    # EXIT
    if pos:

        step = TRAIL_STEP[symbol]

        if pos["side"]=="long":

            moved = price - pos["last_trail_price"]

            if moved >= step:
                pos["stop"] += step
                pos["last_trail_price"] = price

            if price < pos["stop"]:
                exit_trade(symbol,price,pos,state)


        if pos["side"]=="short":

            moved = pos["last_trail_price"] - price

            if moved >= step:
                pos["stop"] -= step
                pos["last_trail_price"] = price

            if price > pos["stop"]:
                exit_trade(symbol,price,pos,state)


def exit_trade(symbol,price,pos,state):

    pnl = (
        (price-pos["entry"]) if pos["side"]=="long"
        else (pos["entry"]-price)
    ) * CONTRACT_SIZE[symbol] * pos["qty"]

    net = pnl - commission(price,pos["qty"],symbol)

    save_trade({
        "symbol":symbol,
        "side":pos["side"],
        "entry_price":pos["entry"],
        "exit_price":price,
        "qty":pos["qty"],
        "net_pnl":round(net,6),
        "entry_time":pos["entry_time"],
        "exit_time":datetime.now()
    })

    log(f"{symbol} EXIT PnL {round(net,2)}")

    send_telegram(
        f"✅ {symbol} EXIT\nPnL:{round(net,2)}"
    )

    state["position"] = None


# ================= MAIN =================
def run():

    if not os.path.exists(TRADE_CSV):
        pd.DataFrame(
            columns=["entry_time","exit_time","symbol","side",
                     "entry_price","exit_price","qty","net_pnl"]
        ).to_csv(TRADE_CSV,index=False)

    state = {s:{"position":None,"last_candle":None} for s in SYMBOLS}

    log("BOT STARTED")
    send_telegram("🚀 Bot Started")

    while True:

        try:

            for symbol in SYMBOLS:

                df = fetch_candles(symbol)

                if len(df) < 100:
                    continue

                price = fetch_price(symbol)

                process_symbol(symbol,df,price,state[symbol])

            time.sleep(5)

        except Exception:

            log(traceback.format_exc())
            send_telegram("⚠️ BOT ERROR")

            time.sleep(5)


if __name__ == "__main__":
    run()