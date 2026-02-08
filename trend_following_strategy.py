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
from fyers_apiv3 import fyersModel
from datetime import datetime, time, timedelta, timezone
from dotenv import load_dotenv

# ================= PATH FIX =================
mydir = os.getcwd()
sys.path.append(mydir)

# ================= LOAD ENV =================
load_dotenv()

# ================= IST =================
IST = timezone(timedelta(hours=5, minutes=30))

def ist_now():
    return datetime.now(timezone.utc).astimezone(IST)

def ist_today():
    return ist_now().date()

def ist_time():
    return ist_now().time()

# ================= TELEGRAM =================
TELEGRAM_BOT_TOKEN = os.getenv("TEL_BOTfg_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("fg")

def send_telegram(msg):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg},
            timeout=5
        )
    except Exception as e:
        print("Telegram error:", e)

# ================= CONFIG =================
CLIENT_ID = "98E1TAKD4T-100"
QTY = 30

SL_POINTS = 50
TARGET_POINTS = 250
TRAIL_POINTS = 1
COMMISSION_RATE = 0.0004

TOKEN_FILE = "auth/api_key/access_token.txt"
folder = "data/Trend_Following"
os.makedirs(folder, exist_ok=True)
TRADES_FILE = f"{folder}/live_trades.csv"

# ================= STATE =================
fyers = None
trade_active = False
CURR_position = 0
CURR_enter = 0
CURR_SL = 0
CURR_TSL = 0
CURR_enter_time = None
TRADE_SYMBOL = None

# ================= UTILS =================
def commission(price, qty):
    return round(price * qty * COMMISSION_RATE, 6)

def calculate_pnl(entry, exit, qty):
    return round((exit - entry) * qty, 6)

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

# ================= MARKET DATA =================
def get_stock_historical_data(data, fyers):
    final_data = fyers.history(data=data)
    df = pd.DataFrame(final_data['candles'])
    df = df.rename(columns={0:'Date',1:'Open',2:'High',3:'Low',4:'Close',5:'V'})
    df['Date'] = pd.to_datetime(df['Date'], unit='s')
    df.set_index("Date", inplace=True)
    return df

# ================= OPTION SYMBOL =================
def get_atm_option_symbol(spot_price, option_type):
    strike = round(spot_price / 100) * 100
    expiry = ist_today().strftime("%y%b").upper()
    return f"NSE:BANKNIFTY{expiry}{strike}{option_type}"

# ================= STRATEGY =================
def run_strategy():
    global trade_active, CURR_position, CURR_enter
    global CURR_SL, CURR_TSL, CURR_enter_time, TRADE_SYMBOL

    SPOT_SYMBOL = "NSE:NIFTYBANK-INDEX"

    quotes = fyers.quotes({"symbols": SPOT_SYMBOL})["d"]
    spot_price = quotes[0]["v"]["lp"]

    hist_data = {
        "symbol": SPOT_SYMBOL,
        "resolution": "1",
        "date_format": "1",
        "range_from": (ist_today()-timedelta(days=5)).strftime("%Y-%m-%d"),
        "range_to": ist_today().strftime("%Y-%m-%d"),
        "cont_flag": "1"
    }

    df = get_stock_historical_data(hist_data, fyers)

    if len(df) < 50:
        return

    st = ta.supertrend(
        high=df["High"],
        low=df["Low"],
        close=df["Close"],
        length=21,
        multiplier=1.5
    )

    df["SUPERTREND"] = st["SUPERT_21_1.5"]

    last = df.iloc[-2]

    # ===== ENTRY =====
    if not trade_active and ist_time() >= time(9,30):

        if last.Close > last.SUPERTREND:
            CURR_position = 1
            option_type = "CE"

        elif last.Close < last.SUPERTREND:
            CURR_position = -1
            option_type = "PE"

        else:
            return

        TRADE_SYMBOL = get_atm_option_symbol(spot_price, option_type)

        ltp = fyers.quotes({"symbols": TRADE_SYMBOL})["d"][0]["v"]["lp"]

        CURR_enter = ltp
        CURR_SL = ltp - SL_POINTS
        CURR_TSL = CURR_SL
        CURR_enter_time = ist_now()
        trade_active = True

        send_telegram(f"⚡ BUY {TRADE_SYMBOL} @ {ltp}")

    # ===== TRAILING =====
    if trade_active:

        ltp = fyers.quotes({"symbols": TRADE_SYMBOL})["d"][0]["v"]["lp"]

        profit = ltp - CURR_enter
        if profit > TRAIL_POINTS:
            CURR_TSL = max(CURR_TSL, ltp - TRAIL_POINTS)

        exit_reason = None

        if profit >= TARGET_POINTS:
            exit_reason = "TARGET"

        if ltp <= CURR_TSL:
            exit_reason = "TSL"

        if ist_time() >= time(15,15):
            exit_reason = "TIME"

        if exit_reason:

            net = calculate_pnl(CURR_enter, ltp, QTY) - commission(ltp,QTY)

            save_trade({
                "entry_time": CURR_enter_time.isoformat(),
                "exit_time": ist_now().isoformat(),
                "symbol": TRADE_SYMBOL,
                "entry_price": CURR_enter,
                "exit_price": ltp,
                "net_pnl": net
            })

            send_telegram(f"🔁 EXIT {TRADE_SYMBOL} | ₹{round(net,2)} | {exit_reason}")

            trade_active=False
            CURR_position=0

# ================= MAIN LOOP =================
send_telegram("🚀 Option Buying Algo Started")

token_loaded=False
model_loaded=False

while True:
    try:
        market_open=time(9,15)
        market_close=time(15,30)

        if market_open<=ist_time()<market_close:
            if not token_loaded:
                token_loaded=load_token()

            if not model_loaded:
                fyers=load_model()
                model_loaded=True

        if time(9,30)<=ist_time()<=market_close and model_loaded:
            run_strategy()

        t.sleep(2)

    except Exception as e:
        send_telegram(f"❌ Algo error: {e}")
        t.sleep(5)
