# ================= HARD FIX FOR FYERS TIMEZONE BUG =================
import zoneinfo
_original_zoneinfo = zoneinfo.ZoneInfo

class SafeZoneInfo(zoneinfo.ZoneInfo):
    def __new__(cls, key):
        if not key:
            key = "UTC"
        if isinstance(key, str) and key.lower() == "asia/kolkata":
            key = "Asia/Kolkata"
        return _original_zoneinfo(key)

zoneinfo.ZoneInfo = SafeZoneInfo

# ================= IMPORTS =================
import os
import sys
import time as t
import requests
import pandas as pd
import numpy as np
import pandas_ta as ta
from scipy.signal import argrelextrema
from fyers_apiv3 import fyersModel
from datetime import datetime, time, timedelta, timezone
from dotenv import load_dotenv

# ================= PATH FIX =================
sys.path.append(os.getcwd())

# ================= LOAD ENV =================
load_dotenv()

# ================= FORCE IST =================
IST = timezone(timedelta(hours=5, minutes=30))

def ist_now():
    return datetime.now(timezone.utc).astimezone(IST)

def ist_today():
    return ist_now().date()

def ist_time():
    return ist_now().time()

# ================= TELEGRAM =================
TELEGRAM_BOT_TOKEN = os.getenv("TEL_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TEL_CHAT_ID")

def send_telegram(msg):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg},
            timeout=5
        )
    except:
        pass

# ================= CONFIG =================
CLIENT_ID = "98E1TAKD4T-100"
QTY = 30
SL_POINTS = 50
TARGET_POINTS = 200
COMMISSION_RATE = 0.0004

TOKEN_FILE = "auth/api_key/access_token.txt"
TOKEN_CSV = "data/Trend_Following/instrument_token.csv"
DATA_FOLDER = "data/Trend_Following"
TRADES_FILE = f"{DATA_FOLDER}/live_trades.csv"
os.makedirs(DATA_FOLDER, exist_ok=True)

# ================= STATE =================
fyers = None
token_df = None

CURR_position = 0   # 1 = BUY, -1 = SELL
CURR_enter = 0
CURR_enter_time = None
last_candle_time = None

token_loaded = model_loaded = symbols_loaded = False

# ================= UTILS =================
def commission(price, qty):
    return price * qty * COMMISSION_RATE

def calculate_pnl(entry, exit, qty):
    return (exit - entry) * qty

def save_trade(trade):
    df = pd.DataFrame([trade])
    if not os.path.exists(TRADES_FILE):
        df.to_csv(TRADES_FILE, index=False)
    else:
        df.to_csv(TRADES_FILE, mode="a", header=False, index=False)

# ================= FYERS AUTH =================
def load_token():    
    send_telegram("🔐 Loading token")
    os.system("python auth/fyers_auth.py")
    return True

def load_model():
    send_telegram("🔌 Loading Fyers model")
    token = open(TOKEN_FILE).read().strip()
    return fyersModel.FyersModel(
        client_id=CLIENT_ID,
        token=token,
        is_async=False,
        log_path=""
    )

# ================= SYMBOL LOAD =================
def load_symbols(fyers):
    send_telegram("📄 Loading symbols")
    if os.path.exists(TOKEN_CSV):
        return pd.read_csv(TOKEN_CSV)

    df = pd.read_csv("https://api.kite.trade/instruments")
    df = df[(df["name"] == "BANKNIFTY") & (df["segment"] == "NFO-FUT")]
    df = df.sort_values("expiry").iloc[0]

    out = pd.DataFrame([{
        "tradingsymbol": df["tradingsymbol"]
    }])
    out.to_csv(TOKEN_CSV, index=False)
    return out

# ================= MARKET DATA =================
def get_stock_historical_data(data, fyers):
    res = fyers.history(data=data)
    df = pd.DataFrame(res["candles"])
    df.columns = ["Date", "Open", "High", "Low", "Close", "V"]
    df["Date"] = pd.to_datetime(df["Date"], unit="s")
    df.set_index("Date", inplace=True)
    return df

# ================= STRATEGY LOGIC =================
def high_low_trend(data, fyers):
    df = get_stock_historical_data(data, fyers)

    ha = pd.DataFrame(index=df.index)
    ha["HA_close"] = (df["Open"] + df["High"] + df["Low"] + df["Close"]) / 4
    ha["HA_open"] = ha["HA_close"].shift(1)
    ha.iloc[0, ha.columns.get_loc("HA_open")] = df.iloc[0][["Open", "Close"]].mean()

    ha["HA_high"] = ha[["HA_open", "HA_close"]].join(df["High"]).max(axis=1)
    ha["HA_low"] = ha[["HA_open", "HA_close"]].join(df["Low"]).min(axis=1)

    ha["high_smooth"] = ta.ema(ha["HA_high"], 5)
    ha["low_smooth"] = ta.ema(ha["HA_low"], 5)

    max_idx = argrelextrema(ha["high_smooth"].values, np.greater_equal, order=21)[0]
    min_idx = argrelextrema(ha["low_smooth"].values, np.less_equal, order=21)[0]

    ha["max_high"] = np.nan
    ha["max_low"] = np.nan

    # ✅ FIXED: use iloc (positional indexing)
    if len(max_idx) > 0:
        ha.iloc[max_idx, ha.columns.get_loc("max_high")] = ha.iloc[max_idx]["HA_high"].values
    if len(min_idx) > 0:
        ha.iloc[min_idx, ha.columns.get_loc("max_low")] = ha.iloc[min_idx]["HA_low"].values

    ha[["max_high", "max_low"]] = ha[["max_high", "max_low"]].ffill()

    trend = ha.iloc[0]["HA_close"]
    ha["trendline"] = trend

    for i in range(1, len(ha)):
        if ha.iloc[i]["HA_high"] == ha.iloc[i]["max_high"]:
            trend = ha.iloc[i]["HA_low"]
        elif ha.iloc[i]["HA_low"] == ha.iloc[i]["max_low"]:
            trend = ha.iloc[i]["HA_high"]
        ha.iloc[i, ha.columns.get_loc("trendline")] = trend

    ha["ATR"] = ta.atr(ha["HA_high"], ha["HA_low"], ha["HA_close"], 14)
    ha["ATR_EMA"] = ta.ema(ha["ATR"], 14)

    ha["trade_single"] = 0
    for i in range(1, len(ha)):
        if ha["ATR"].iloc[i] <= ha["ATR_EMA"].iloc[i]:
            continue
        if ha["HA_close"].iloc[i-1] < ha["trendline"].iloc[i-1] and ha["HA_close"].iloc[i] > ha["trendline"].iloc[i]:
            ha.iloc[i, ha.columns.get_loc("trade_single")] = 1
        elif ha["HA_close"].iloc[i-1] > ha["trendline"].iloc[i-1] and ha["HA_close"].iloc[i] < ha["trendline"].iloc[i]:
            ha.iloc[i, ha.columns.get_loc("trade_single")] = -1

    return ha

# ================= RUN STRATEGY =================
def run_strategy():
    global CURR_position, CURR_enter, CURR_enter_time, last_candle_time

    symbol = "NSE:" + token_df.loc[0, "tradingsymbol"]

    start = (ist_today() - timedelta(days=5)).strftime("%Y-%m-%d")
    end = ist_today().strftime("%Y-%m-%d")

    data = {
        "symbol": symbol,
        "resolution": "5",
        "date_format": "1",
        "range_from": start,
        "range_to": end,
        "cont_flag": "1"
    }

    ha = high_low_trend(data, fyers)
    candle = ha.iloc[-2]
    signal = candle["trade_single"]
    candle_time = ha.index[-2]

    if candle_time == last_candle_time:
        return
    last_candle_time = candle_time

    price = fyers.quotes({"symbols": symbol})["d"][0]["v"]["lp"]

    # NO POSITION
    if CURR_position == 0 and signal != 0:
        CURR_position = signal
        CURR_enter = price
        CURR_enter_time = ist_now()
        send_telegram(f"{'🟢 BUY' if signal == 1 else '🔴 SELL'} @ {price}")

    # BUY
    elif CURR_position == 1:
        if price <= candle["trendline"] or ist_time() >= time(15,15) or price - CURR_enter >= TARGET_POINTS:
            net = calculate_pnl(CURR_enter, price, QTY) - commission(price, QTY)
            save_trade({
                "entry_time": CURR_enter_time.isoformat(),
                "exit_time": ist_now().isoformat(),
                "symbol": symbol,
                "side": "BUY",
                "entry_price": CURR_enter,
                "exit_price": price,
                "qty": QTY,
                "net_pnl": net
            })
            CURR_position = 0
            send_telegram(f"🔴 EXIT BUY @ {price} | ₹{round(net,2)}")
            if signal == -1:
                CURR_position = -1
                CURR_enter = price
                CURR_enter_time = ist_now()
                send_telegram(f"🔴 SELL @ {price}")

    # SELL
    elif CURR_position == -1:
        if price >= candle["trendline"] or ist_time() >= time(15,15) or CURR_enter - price >= TARGET_POINTS:
            net = calculate_pnl(price, CURR_enter, QTY) - commission(price, QTY)
            save_trade({
                "entry_time": CURR_enter_time.isoformat(),
                "exit_time": ist_now().isoformat(),
                "symbol": symbol,
                "side": "SELL",
                "entry_price": CURR_enter,
                "exit_price": price,
                "qty": QTY,
                "net_pnl": net
            })
            CURR_position = 0
            send_telegram(f"🟢 EXIT SELL @ {price} | ₹{round(net,2)}")
            if signal == 1:
                CURR_position = 1
                CURR_enter = price
                CURR_enter_time = ist_now()
                send_telegram(f"🟢 BUY @ {price}")

# ================= MAIN LOOP =================
send_telegram("🚀 Trend Following Algo Started")
current_day = ist_today()

while True:
    try:
        if time(9,15) <= ist_time() <= time(15,30):
            if not token_loaded:
                token_loaded = load_token()
            if not model_loaded:
                fyers = load_model()
                model_loaded = True
            if not symbols_loaded:
                token_df = load_symbols(fyers)
                symbols_loaded = True
            run_strategy()

        if ist_time() > time(15,30) and current_day == ist_today():
            CURR_position = 0
            token_loaded = model_loaded = symbols_loaded = False
            current_day = ist_today() + timedelta(days=1)
            send_telegram("♻ EOD reset done")

        t.sleep(2)

    except Exception as e:
        send_telegram(f"❌ Algo error: {e}")
        t.sleep(5)
