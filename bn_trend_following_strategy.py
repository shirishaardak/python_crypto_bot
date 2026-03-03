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
import os, sys
import time as t
import pandas as pd
import numpy as np
import pandas_ta as ta
from fyers_apiv3 import fyersModel
from datetime import datetime, time, timedelta, timezone
from dotenv import load_dotenv
import requests

sys.path.append(os.getcwd())
load_dotenv()

requests.adapters.DEFAULT_RETRIES = 5

# ================= IST TIME =================
IST = timezone(timedelta(hours=5, minutes=30))

def ist_now():
    return datetime.now(timezone.utc).astimezone(IST)

def ist_today():
    return ist_now().date()

def ist_time():
    return ist_now().time()

# ================= CONFIG =================
CLIENT_ID="98E1TAKD4T-100"
QTY=30
COMMISSION_RATE=0.0004
SPOT_SYMBOL="NSE:NIFTYBANK-INDEX"

folder="data/Trend_Following"
os.makedirs(folder,exist_ok=True)
TRADES_FILE=f"{folder}/live_trades.csv"
TOKEN_FILE="auth/api_key/access_token.txt"

# ================= TELEGRAM =================
TELEGRAM_BOT_TOKEN = os.getenv("TEL_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TEL_CHAT_ID")

def send_telegram(msg):
    try:
        url=f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload={"chat_id":TELEGRAM_CHAT_ID,"text":msg}
        requests.post(url,json=payload,timeout=5)
    except:
        pass

# ================= GLOBAL STATE =================
fyers=None
position_type=None
entry_price=0
entry_time=None
symbol=None
last_reset_date=None
token_load_date=None
model_load_date=None
holiday_load_date=None
last_exit_time=None
stop_loss=None
trail_level=None
NSE_HOLIDAYS=set()

# ================= UTILS =================
def commission(price,qty):
    return round(price*qty*COMMISSION_RATE,6)

def calculate_pnl(entry,exit,qty):
    return round((exit-entry)*qty,6)

def save_trade(data):
    df=pd.DataFrame([data])
    if not os.path.exists(TRADES_FILE):
        df.to_csv(TRADES_FILE,index=False)
    else:
        df.to_csv(TRADES_FILE,mode="a",header=False,index=False)

# ================= DAILY RESET =================
def daily_reset():
    global position_type, entry_price, entry_time
    global symbol, last_exit_time, stop_loss, trail_level

    position_type=None
    entry_price=0
    entry_time=None
    symbol=None
    last_exit_time=None
    stop_loss=None
    trail_level=None

    send_telegram("🔄 Daily Reset Completed")

# ================= AUTH =================
def load_token():
    os.system("python auth/fyers_auth.py")
    return True

def load_model():
    token=open(TOKEN_FILE).read().strip()
    model=fyersModel.FyersModel(
        client_id=CLIENT_ID,
        token=token,
        is_async=False,
        log_path=""
    )
    send_telegram("📡 Fyers Model Connected")
    return model

# ================= NSE HOLIDAY ENGINE =================
def fetch_nse_holidays():
    global NSE_HOLIDAYS
    try:
        url="https://www.nseindia.com/api/holiday-master?type=trading"
        headers={"User-Agent":"Mozilla/5.0","Accept":"application/json"}
        session=requests.Session()
        session.get("https://www.nseindia.com",headers=headers,timeout=5)
        response=session.get(url,headers=headers,timeout=5)
        data=response.json()

        NSE_HOLIDAYS.clear()
        for item in data["CM"]:
            h_date=datetime.strptime(item["tradingDate"],"%d-%b-%Y").date()
            NSE_HOLIDAYS.add(h_date)

        send_telegram("✅ NSE Holidays Loaded")

    except Exception as e:
        send_telegram(f"⚠ Holiday Fetch Failed {e}")

def is_market_open():
    today=ist_today()
    if today.weekday()>=5:
        return False
    if today in NSE_HOLIDAYS:
        return False
    return True

# ================= STRATEGY =================
def run_strategy():
    global position_type, entry_price, entry_time
    global stop_loss, trail_level, last_exit_time

    data = {
        "symbol": SPOT_SYMBOL,
        "resolution": "5",
        "date_format": "1",
        "range_from": (datetime.now()-timedelta(days=2)).strftime("%Y-%m-%d"),
        "range_to": datetime.now().strftime("%Y-%m-%d"),
        "cont_flag": "1"
    }

    candles = fyers.history(data)
    if "candles" not in candles:
        return

    df = pd.DataFrame(candles["candles"],
                      columns=["time","open","high","low","close","volume"])

    df["close"]=df["close"].astype(float)
    df["high"]=df["high"].astype(float)
    df["low"]=df["low"].astype(float)

    df["supertrend"]=ta.supertrend(df["high"],df["low"],df["close"],10,3).iloc[:,0]

    price=df["close"].iloc[-1]
    st=df["supertrend"].iloc[-1]

    # ENTRY
    if position_type is None:
        if price>st:
            position_type="LONG"
            entry_price=price
            entry_time=ist_now()
            stop_loss=st
            trail_level=st
            send_telegram(f"🟢 LONG Entry {price}")

        elif price<st:
            position_type="SHORT"
            entry_price=price
            entry_time=ist_now()
            stop_loss=st
            trail_level=st
            send_telegram(f"🔴 SHORT Entry {price}")

    # MANAGEMENT
    else:
        if position_type=="LONG":
            if price<=stop_loss:
                exit_trade(price)

            if price-entry_price>50:
                stop_loss=entry_price

        if position_type=="SHORT":
            if price>=stop_loss:
                exit_trade(price)

            if entry_price-price>50:
                stop_loss=entry_price

def exit_trade(price):
    global position_type, entry_price, entry_time

    pnl=calculate_pnl(entry_price,price,QTY)
    net=pnl-commission(price,QTY)-commission(entry_price,QTY)

    send_telegram(f"🏁 Exit {price} | Net PnL {net}")

    save_trade({
        "entry_time":entry_time,
        "exit_time":ist_now(),
        "side":position_type,
        "entry_price":entry_price,
        "exit_price":price,
        "qty":QTY,
        "net_pnl":net
    })

    position_type=None

# ================= START =================
send_telegram("🚀 BankNifty Dynamic ATM Trend Algo Started")

fetch_nse_holidays()
holiday_load_date = ist_today()

# ================= MAIN LOOP =================
while True:
    try:
        now=ist_time()
        today=ist_today()

        if time(3,30) <= now < time(3,31) and last_reset_date != today:
            daily_reset()
            last_reset_date=today

        if time(8,55) <= now < time(9,0) and holiday_load_date != today:
            fetch_nse_holidays()
            holiday_load_date=today

        if time(9,0)<=now<time(15,30):
            if token_load_date!=today:
                load_token()
                token_load_date=today

        if time(9,0)<=now<time(9,30):
            if model_load_date!=today:
                fyers=load_model()
                model_load_date=today

        if time(9,30)<=now<=time(15,30) and model_load_date==today:
            if is_market_open():
                run_strategy()

        t.sleep(3)

    except Exception as e:
        send_telegram(f"❌ Algo Error: {e}")
        t.sleep(5)