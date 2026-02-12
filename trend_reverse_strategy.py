import os
import time
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from dotenv import load_dotenv
from scipy.signal import argrelextrema
import pandas_ta as ta

load_dotenv()

# ================= TELEGRAM =================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
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

STOP_LOSS = {"BTCUSD": 50, "ETHUSD": 5}

TAKER_FEE = 0.0005
TIMEFRAME = "5m"
DAYS = 5

BASE_DIR=os.getcwd()
SAVE_DIR=os.path.join(BASE_DIR,"data","trend_reverse_strategy")
os.makedirs(SAVE_DIR,exist_ok=True)

TRADE_CSV=os.path.join(SAVE_DIR,"live_trades.csv")

LAST_CANDLE_TIME = {}

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

def save_processed_data(df, symbol):

    path = os.path.join(SAVE_DIR, f"{symbol}_processed.csv")

    out = pd.DataFrame({
        "time": df.index,
        "HA_open": df["HA_open"],
        "HA_high": df["HA_high"],
        "HA_low": df["HA_low"],
        "HA_close": df["HA_close"],
        "trendline": df["trendline"]
    })

    out.to_csv(path, index=False)

# ================= DATA =================
def fetch_price(symbol):
    try:
        r = requests.get(f"https://api.india.delta.exchange/v2/tickers/{symbol}", timeout=5)
        return float(r.json()["result"]["mark_price"])
    except Exception as e:
        log(f"{symbol} PRICE fetch error: {e}")
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

# ================= HEIKIN ASHI =================
def calculate_heikin_ashi(df):
    ha_df = df.copy()

    ha_df["HA_close"] = (ha_df["Open"] + ha_df["High"] + ha_df["Low"] + ha_df["Close"]) / 4

    ha_open = []
    for i in range(len(ha_df)):
        if i == 0:
            ha_open.append((ha_df["Open"].iloc[0] + ha_df["Close"].iloc[0]) / 2)
        else:
            ha_open.append((ha_open[i-1] + ha_df["HA_close"].iloc[i-1]) / 2)

    ha_df["HA_open"] = ha_open
    ha_df["HA_high"] = ha_df[["High", "HA_open", "HA_close"]].max(axis=1)
    ha_df["HA_low"] = ha_df[["Low", "HA_open", "HA_close"]].min(axis=1)

    return ha_df

# ================= TRENDLINE =================
def calculate_trendline(df):

    ha = df.copy()

    ha["ATR"]=ta.atr(ha["HA_high"],ha["HA_low"],ha["HA_close"],length=14)
    ha["ATR_MA"]=ha["ATR"].rolling(14).min() 

    order = 21

    ha["swing_high"] = np.nan
    ha["swing_low"] = np.nan

    high_idx = argrelextrema(
        ha["HA_high"].values,
        np.greater_equal,
        order=order
    )[0]

    low_idx = argrelextrema(
        ha["HA_low"].values,
        np.less_equal,
        order=order
    )[0]

    ha.iloc[high_idx, ha.columns.get_loc("swing_high")] = ha.iloc[high_idx]["HA_high"].values
    ha.iloc[low_idx, ha.columns.get_loc("swing_low")] = ha.iloc[low_idx]["HA_low"].values

    ha["trendline"] = np.nan
    trend = ha["HA_close"].iloc[0]
    ha.iloc[0, ha.columns.get_loc("trendline")] = trend

    for i in range(1, len(ha)):
        if not np.isnan(ha.iloc[i]["swing_high"]):
            trend = ha.iloc[i]["HA_high"]
        elif not np.isnan(ha.iloc[i]["swing_low"]):
            trend = ha.iloc[i]["HA_low"]

        ha.iloc[i, ha.columns.get_loc("trendline")] = trend

    return ha

# ================= CANDLE CHECK =================
def is_new_candle(symbol, df):

    last_closed_time = df.index[-2]

    if symbol not in LAST_CANDLE_TIME:
        LAST_CANDLE_TIME[symbol] = last_closed_time
        return True

    if last_closed_time != LAST_CANDLE_TIME[symbol]:
        LAST_CANDLE_TIME[symbol] = last_closed_time
        return True

    return False

# ================= STRATEGY =================
def process_symbol(symbol, df, price, state, allow_entry):

    df = calculate_heikin_ashi(df)
    df = calculate_trendline(df)

    save_processed_data(df, symbol)

    last = df.iloc[-2]
    prev = df.iloc[-3]

    pos  = state["position"]
    now = datetime.now()

    # ===== ENTRY =====
    if allow_entry and pos is None and last.ATR > last.ATR_MA:

        if (
            last.HA_close > last.trendline and
            last.HA_close > prev.HA_open and
            last.HA_close > prev.HA_close
        ):
            risk = abs(price - last.trendline)

            state["position"] = {
                "side": "long",
                "entry": price,
                "stop": last.trendline,
                "risk": risk,
                "qty": DEFAULT_CONTRACTS[symbol],
                "entry_time": now
            }
            log(f"🟢 {symbol} LONG ENTRY @ {price}")
            return

        if (
            last.HA_close < last.trendline and
            last.HA_close < prev.HA_open and
            last.HA_close < prev.HA_close
        ):
            risk = abs(price - last.trendline)

            state["position"] = {
                "side": "short",
                "entry": price,
                "stop": last.trendline,
                "risk": risk,
                "qty": DEFAULT_CONTRACTS[symbol],
                "entry_time": now
            }
            log(f"🔴 {symbol} SHORT ENTRY @ {price}")
            return

    # ===== POSITION MANAGEMENT =====
    if pos:

        # ===== TRAILING STOP =====
        if pos["side"] == "long":
            new_stop = price - STOP_LOSS[symbol]
            if new_stop > pos["stop"]:
                pos["stop"] = new_stop
                log(f"🔄 {symbol} LONG TRAIL SL → {round(new_stop,2)}")

        elif pos["side"] == "short":
            new_stop = price + STOP_LOSS[symbol]
            if new_stop < pos["stop"]:
                pos["stop"] = new_stop
                log(f"🔄 {symbol} SHORT TRAIL SL → {round(new_stop,2)}")

        exit_trade = False
        pnl = 0

        if pos["side"] == "long":
            if last.HA_close < pos["stop"]:
                pnl = (price - pos["entry"]) * CONTRACT_SIZE[symbol] * pos["qty"]
                exit_trade = True

        if pos["side"] == "short":
            if last.HA_close > pos["stop"]:
                pnl = (pos["entry"] - price) * CONTRACT_SIZE[symbol] * pos["qty"]
                exit_trade = True

        if exit_trade:
            fee = commission(price, pos["qty"], symbol)
            net = pnl - fee

            save_trade({
                "symbol": symbol,
                "side": pos["side"],
                "entry_price": pos["entry"],
                "exit_price": price,
                "qty": pos["qty"],
                "net_pnl": round(net,6),
                "entry_time": pos["entry_time"],
                "exit_time": now
            })

            emoji = "🟢" if net > 0 else "🔴"
            log(f"{emoji} {symbol} EXIT @ {price}  PNL: {round(net,6)}")

            state["position"] = None

# ================= MAIN =================
def run():

    state = {s: {"position": None} for s in SYMBOLS}

    log("🚀 Strategy LIVE (Trendline Trailing Stop Enabled)")

    while True:
        try:
            for symbol in SYMBOLS:
                df = fetch_candles(symbol)

                if df is None or len(df) < 100:
                    continue

                price = fetch_price(symbol)

                if price is None:
                    continue

                new_candle = is_new_candle(symbol, df)

                if state[symbol]["position"]:
                    process_symbol(symbol, df, price, state[symbol], allow_entry=False)

                elif new_candle:
                    process_symbol(symbol, df, price, state[symbol], allow_entry=True)

            time.sleep(5)

        except Exception as e:
            log(f"🚨 Runtime error: {e}")
            time.sleep(5)

if __name__ == "__main__":
    run()
