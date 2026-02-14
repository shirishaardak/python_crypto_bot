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
from scipy.signal import argrelextrema
import requests

# ================= PATH =================
sys.path.append(os.getcwd())
load_dotenv()

# ================= IST =================
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
TELEGRAM_BOT_TOKEN="TEL_BOT_TOKEN"
TELEGRAM_CHAT_ID="TEL_CHAT_ID"

def send_telegram(msg):
    try:
        url=f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload={"chat_id":TELEGRAM_CHAT_ID,"text":msg}
        requests.post(url,json=payload,timeout=5)
    except Exception as e:
        print(f"Telegram error: {e}")

# ================= STATE =================
fyers=None
CE_active=False
PE_active=False
CE_entry=0
PE_entry=0
CE_TSL=0
PE_TSL=0
CE_symbol=None
PE_symbol=None
CE_enter_time=None
PE_enter_time=None
last_signal_candle=None

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

# ================= AUTH =================
def load_token():
    send_telegram("🔑 API Token Loading Started")
    os.system("python auth/fyers_auth.py")
    send_telegram("✅ API Token Loaded")
    return True

def load_model():
    try:
        send_telegram("📡 Fyers API Model Loading...")
        token=open(TOKEN_FILE).read().strip()
        model=fyersModel.FyersModel(client_id=CLIENT_ID,token=token,is_async=False,log_path="")
        send_telegram("✅ Fyers API Connected Successfully")
        return model
    except Exception as e:
        send_telegram(f"❌ API Load Error: {e}")
        raise

# ================= DATA =================
def get_stock_historical_data(data,fyers):
    final_data=fyers.history(data=data)
    df=pd.DataFrame(final_data['candles'])
    df=df.rename(columns={0:'Date',1:'Open',2:'High',3:'Low',4:'Close',5:'V'})
    df['Date']=pd.to_datetime(df['Date'],unit='s')
    df.set_index("Date",inplace=True)
    return df

# ================= TRENDLINE =================
def calculate_trendline(df):

    ha=ta.ha(df["Open"],df["High"],df["Low"],df["Close"]).reset_index(drop=True)
    data=df.copy().reset_index(drop=True)

    data["HA_Close"]=ha["HA_close"]
    data["HA_Open"]=ha["HA_open"]
    data["HA_High"]=ha["HA_high"]
    data["HA_Low"]=ha["HA_low"]

    high_vals=data["HA_High"].values
    low_vals=data["HA_Low"].values

    max_idx=argrelextrema(high_vals,np.greater_equal,order=21)[0]
    min_idx=argrelextrema(low_vals,np.less_equal,order=21)[0]

    data["smoothed_high"]=np.nan
    data["smoothed_low"]=np.nan

    data.iloc[max_idx,data.columns.get_loc("smoothed_high")]=data["HA_High"].iloc[max_idx]
    data.iloc[min_idx,data.columns.get_loc("smoothed_low")]=data["HA_Low"].iloc[min_idx]

    data[["smoothed_high","smoothed_low"]]=data[["smoothed_high","smoothed_low"]].ffill()

    trendline=data["HA_Close"].iloc[0]
    data["trendline"]=trendline

    for i in range(1,len(data)):
        if data["HA_High"].iloc[i]==data["smoothed_high"].iloc[i]:
            trendline=data["HA_High"].iloc[i]
        elif data["HA_Low"].iloc[i]==data["smoothed_low"].iloc[i]:
            trendline=data["HA_Low"].iloc[i]
        data.loc[i,"trendline"]=trendline

    data.index=df.index
    return data

# ================= MONTHLY EXPIRY =================
def last_thursday(year,month):
    last_day=date(year,month,28)+timedelta(days=4)
    last_day=last_day-timedelta(days=last_day.day)
    offset=(last_day.weekday()-3)%7
    return last_day-timedelta(days=offset)

def monthly_expiry_code():
    today=ist_today()
    exp=last_thursday(today.year,today.month)

    if today>exp:
        if today.month==12:
            exp=last_thursday(today.year+1,1)
        else:
            exp=last_thursday(today.year,today.month+1)

    return exp.strftime("%y%b").upper()

# ================= OPTION BUILDER =================
def build_option_symbol(spot_price,opt_type):

    strike=int(round(spot_price/100)*100)
    expiry=monthly_expiry_code()

    symbol=f"NSE:BANKNIFTY{expiry}{strike}{opt_type}"

    try:
        test=fyers.quotes({"symbols":symbol})
        if "d" in test:
            return symbol
    except:
        return None

# ================= TRAILING =================
def update_trailing(entry,current,sl):
    profit=current-entry
    if profit<=0:
        return sl
    steps=int(profit//1)
    new_sl=entry-25+(steps*1)
    return max(sl,new_sl)

# ================= STRATEGY =================
def run_strategy():

    global CE_active,PE_active
    global CE_entry,PE_entry
    global CE_TSL,PE_TSL
    global CE_symbol,PE_symbol
    global CE_enter_time,PE_enter_time
    global last_signal_candle

    send_telegram("⚙️ Strategy Cycle Running")

    spot_price=fyers.quotes({"symbols":SPOT_SYMBOL})["d"][0]["v"]["lp"]

    hist={
        "symbol":SPOT_SYMBOL,
        "resolution":"5",
        "date_format":"1",
        "range_from":(ist_today()-timedelta(days=5)).strftime("%Y-%m-%d"),
        "range_to":ist_today().strftime("%Y-%m-%d"),
        "cont_flag":"1"
    }

    df=get_stock_historical_data(hist,fyers)
    if len(df)<50:
        return

    df=calculate_trendline(df)

    last=df.iloc[-2]
    prev=df.iloc[-3]
    candle_time=df.index[-2]

    if last_signal_candle==candle_time:
        return

    buy_signal=last.HA_Close>last.trendline and last.HA_Close>prev.HA_Close
    sell_signal=last.HA_Close<last.trendline and last.HA_Close<prev.HA_Close

    if buy_signal and not CE_active and ist_time()>=time(9,30):

        CE_symbol=build_option_symbol(spot_price,"CE")
        if not CE_symbol:
            return

        price=fyers.quotes({"symbols":CE_symbol})["d"][0]["v"]["lp"]

        CE_entry=price
        CE_TSL=price-25
        CE_enter_time=ist_now()
        CE_active=True
        last_signal_candle=candle_time

        msg=f"⚡ BUY CE {CE_symbol} @ {price}"
        print(msg)
        send_telegram(msg)

    if sell_signal and not PE_active and ist_time()>=time(9,30):

        PE_symbol=build_option_symbol(spot_price,"PE")
        if not PE_symbol:
            return

        price=fyers.quotes({"symbols":PE_symbol})["d"][0]["v"]["lp"]

        PE_entry=price
        PE_TSL=price-25
        PE_enter_time=ist_now()
        PE_active=True
        last_signal_candle=candle_time

        msg=f"⚡ BUY PE {PE_symbol} @ {price}"
        print(msg)
        send_telegram(msg)

    if CE_active:
        price=fyers.quotes({"symbols":CE_symbol})["d"][0]["v"]["lp"]
        CE_TSL=update_trailing(CE_entry,price,CE_TSL)

        if price<=CE_TSL or ist_time()>=time(15,15):

            net=calculate_pnl(CE_entry,price,QTY)-commission(price,QTY)

            save_trade({
                "entry_time":CE_enter_time.isoformat(),
                "exit_time":ist_now().isoformat(),
                "symbol":CE_symbol,
                "side":"BUY_CE",
                "entry_price":CE_entry,
                "exit_price":price,
                "qty":QTY,
                "net_pnl":net
            })

            msg=f"🔁 EXIT CE {CE_symbol} PnL ₹{round(net,2)}"
            print(msg)
            send_telegram(msg)
            CE_active=False

    if PE_active:
        price=fyers.quotes({"symbols":PE_symbol})["d"][0]["v"]["lp"]
        PE_TSL=update_trailing(PE_entry,price,PE_TSL)

        if price<=PE_TSL or ist_time()>=time(15,15):

            net=calculate_pnl(PE_entry,price,QTY)-commission(price,QTY)

            save_trade({
                "entry_time":PE_enter_time.isoformat(),
                "exit_time":ist_now().isoformat(),
                "symbol":PE_symbol,
                "side":"BUY_PE",
                "entry_price":PE_entry,
                "exit_price":price,
                "qty":QTY,
                "net_pnl":net
            })

            msg=f"🔁 EXIT PE {PE_symbol} PnL ₹{round(net,2)}"
            print(msg)
            send_telegram(msg)
            PE_active=False

# ================= MAIN LOOP =================
msg="🚀 BankNifty Monthly Algo Started"
print(msg)
send_telegram(msg)

token_loaded=model_loaded=False

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
        print(f"❌ Algo error {e}")
        send_telegram(f"❌ Algo error {e}")
        t.sleep(5)
