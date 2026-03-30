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
from datetime import datetime, time, timedelta, timezone, date
from dotenv import load_dotenv
import requests

sys.path.append(os.getcwd())
load_dotenv()

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

ADX_PERIOD=14
ADX_THRESHOLD=20

folder="data/Trend_Following"
os.makedirs(folder,exist_ok=True)
TRADES_FILE=f"{folder}/live_trades.csv"
TOKEN_FILE="auth/api_key/access_token.txt"

# ================= TELEGRAM =================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TEL_CHAT_ID")

def send_telegram(msg):
    try:
        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
            print("❌ Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")
            return

        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": msg
        }

        response = requests.post(url, json=payload, timeout=5)

        # 🔥 DEBUG OUTPUT
        print("Status:", response.status_code)
        print("Response:", response.text)

    except Exception as e:
        print("❌ Telegram Error:", e)
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
last_exit_time=None
stop_loss=None
trail_level=None

NSE_HOLIDAYS=set()
holiday_load_date=None

# ================= SAFE PRICE =================
def get_last_price(symbol):
    try:
        data = fyers.quotes({"symbols": symbol})

        if not data or "d" not in data or not data["d"]:
            return None

        v = data["d"][0].get("v", {})
        return v.get("lp")

    except:
        return None

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

# ================= HOLIDAY =================
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

# ================= EXPIRY =================
def get_last_thursday(year,month):
    last_day=date(year,month,1)+timedelta(days=32)
    last_day=last_day.replace(day=1)-timedelta(days=1)
    while last_day.weekday()!=3:
        last_day-=timedelta(days=1)
    return last_day

def get_current_monthly_expiry():
    today=ist_today()
    now=ist_time()
    expiry=get_last_thursday(today.year,today.month)

    if today>expiry or (today==expiry and now>=time(15,30)):
        next_month=today.month+1
        next_year=today.year
        if next_month==13:
            next_month=1
            next_year+=1
        expiry=get_last_thursday(next_year,next_month)

    return expiry

# ================= ATM OPTION =================
def get_atm_option(option_type):

    if not is_market_open():
        return None

    spot=get_last_price(SPOT_SYMBOL)
    if spot is None:
        return None

    strike=int(round(spot/100)*100)

    expiry=get_current_monthly_expiry()
    expiry_str=expiry.strftime("%y%b").upper()

    return f"NSE:BANKNIFTY{expiry_str}{strike}{option_type}"

# ================= DATA =================
def get_stock_historical_data(data):
    try:
        final_data = fyers.history(data=data)

        if not isinstance(final_data, dict):
            return pd.DataFrame()

        candles = final_data.get("candles")

        if not candles:
            return pd.DataFrame()

        df=pd.DataFrame(candles)

        if df.shape[1] < 6:
            return pd.DataFrame()

        df=df.iloc[:, :6]
        df.columns=['Date','Open','High','Low','Close','Volume']

        df['Date']=pd.to_datetime(df['Date'],unit='s',errors='coerce')

        df.dropna(subset=['Date','Open','High','Low','Close'], inplace=True)

        if df.empty:
            return pd.DataFrame()

        df.set_index("Date",inplace=True)
        return df

    except Exception as e:
        send_telegram(f"⚠ Data Fetch Error {e}")
        return pd.DataFrame()

# ================= TRENDLINE =================
def calculate_trendline(df):

    ha=ta.ha(df["Open"],df["High"],df["Low"],df["Close"]).reset_index(drop=True)
    data=df.copy().reset_index(drop=True)

    data["HA_Close"]=ha["HA_close"]
    data["HA_Open"]=ha["HA_open"]
    data["HA_High"]=ha["HA_high"]
    data["HA_Low"]=ha["HA_low"]

    st = ta.supertrend(
        high=ha["HA_high"],
        low=ha["HA_low"],
        close=ha["HA_close"],
        length=21,
        multiplier=2.5
    )

    data["ST"] = st.iloc[:,0]
    data["ST"] = data["ST"].ffill()

    adx=ta.adx(df["High"],df["Low"],df["Close"],length=ADX_PERIOD)
    data["ADX"]=adx["ADX_14"]

    data.index=df.index
    return data

# ================= EXIT =================
def exit_trade(reason):

    global position_type,last_exit_time,stop_loss,trail_level
    global entry_price, entry_time, symbol

    price = get_last_price(symbol)
    if price is None:
        return

    net=calculate_pnl(entry_price,price,QTY)-commission(price,QTY)

    save_trade({
        "entry_time":entry_time.isoformat(),
        "exit_time":ist_now().isoformat(),
        "symbol":symbol,
        "side":position_type,
        "entry_price":entry_price,
        "exit_price":price,
        "qty":QTY,
        "net_pnl":net
    })

    send_telegram(f"🔁 EXIT {symbol} | {reason} | PnL ₹{round(net,2)}")

    position_type=None
    stop_loss=None
    trail_level=None
    last_exit_time=ist_now()

# ================= STRATEGY =================
def run_strategy():

    global position_type, entry_price, entry_time
    global symbol, stop_loss, trail_level

    if last_exit_time and (ist_now()-last_exit_time).seconds<300:
        return

    ce_symbol=get_atm_option("CE")
    pe_symbol=get_atm_option("PE")

    if ce_symbol is None or pe_symbol is None:
        return

    hist_template={
        "resolution":"5",
        "date_format":"1",
        "range_from":(ist_today()-timedelta(days=5)).strftime("%Y-%m-%d"),
        "range_to":ist_today().strftime("%Y-%m-%d"),
        "cont_flag":"1"
    }

    # INDEX
    hist_index=hist_template.copy()
    hist_index["symbol"]=SPOT_SYMBOL

    df_index_raw=get_stock_historical_data(hist_index)
    if df_index_raw.empty:
        return

    df_index=calculate_trendline(df_index_raw)
    if df_index.empty or len(df_index)<50:
        return

    index_last=df_index.iloc[-2]

    # CE
    hist_ce=hist_template.copy()
    hist_ce["symbol"]=ce_symbol
    df_ce=calculate_trendline(get_stock_historical_data(hist_ce))
    if df_ce.empty or len(df_ce)<50:
        return
    ce_last=df_ce.iloc[-2]

    # PE
    hist_pe=hist_template.copy()
    hist_pe["symbol"]=pe_symbol
    df_pe=calculate_trendline(get_stock_historical_data(hist_pe))
    if df_pe.empty or len(df_pe)<50:
        return
    pe_last=df_pe.iloc[-2]

    # ENTRY
    if position_type is None and time(9,30)<=ist_time()<=time(15,15):

        if index_last.ADX < ADX_THRESHOLD:
            return

        if index_last.HA_Close > index_last.ST and ce_last.HA_Close > ce_last.ST:
            symbol=ce_symbol
            position_type="CE"

        elif index_last.HA_Close < index_last.ST and pe_last.HA_Close > pe_last.ST:
            symbol=pe_symbol
            position_type="PE"

        else:
            return

        price=get_last_price(symbol)
        if price is None:
            return

        entry_price=price
        entry_time=ist_now()

        stop_loss=entry_price-50
        trail_level=entry_price+25

        send_telegram(f"⚡ BUY {symbol} @ {price} | SL {stop_loss}")

    # EXIT
    if position_type:

        price=get_last_price(symbol)
        if price is None:
            return

        if position_type=="CE" and price<ce_last.ST:
            exit_trade("CE Supertrend Break")
            return

        if position_type=="PE" and price<pe_last.ST:
            exit_trade("PE Supertrend Break")
            return

        if ist_time()>=time(15,15):
            exit_trade("Time Exit")
            return

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

# ================= DAILY RESET =================
def daily_reset():

    global position_type, entry_price, entry_time
    global symbol, last_exit_time
    global stop_loss, trail_level

    position_type=None
    entry_price=0
    entry_time=None
    symbol=None
    last_exit_time=None
    stop_loss=None
    trail_level=None

    send_telegram("🔄 Daily Reset Completed")

# ================= MAIN LOOP =================
send_telegram("🚀 BankNifty Option Trend Algo Started")

while True:

    try:

        now=ist_time()
        today=ist_today()

        if now >= time(16,0) and last_reset_date != today:
            daily_reset()
            last_reset_date = today

        if time(8,55)<=now<time(9,0) and holiday_load_date!=today:
            fetch_nse_holidays()
            holiday_load_date=today

        if time(9,0)<=now<time(15,30):
            if token_load_date!=today:
                load_token()
                token_load_date=today

        if time(9,0)<=now<time(15,30):
            if model_load_date!=today:
                fyers=load_model()
                model_load_date=today

        if time(9,20)<=now<=time(15,30) and model_load_date==today:

            if is_market_open():
                run_strategy()

        t.sleep(2)

    except Exception as e:
        send_telegram(f"❌ Algo Error: {e}")
        t.sleep(5)