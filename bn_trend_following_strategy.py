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
tsl=0
entry_time=None
symbol=None
last_signal_candle=None
trade_taken_today=False
last_reset_date=None
token_load_date=None
model_load_date=None

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
    global position_type, entry_price, tsl
    global entry_time, symbol, last_signal_candle
    global trade_taken_today

    position_type=None
    entry_price=0
    tsl=0
    entry_time=None
    symbol=None
    last_signal_candle=None
    trade_taken_today=False

    send_telegram("🔄 Daily Reset Completed")

# ================= AUTH =================
def load_token():
    send_telegram("🔑 Loading API Token...")
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

# ================= DATA =================
def get_stock_historical_data(data):
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

# ================= EXPIRY =================
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
def build_option_symbol(spot,opt_type):
    strike=int(round(spot/100)*100)
    expiry=monthly_expiry_code()
    sym=f"NSE:BANKNIFTY{expiry}{strike}{opt_type}"
    try:
        test=fyers.quotes({"symbols":sym})
        if test.get("d"):
            return sym
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

    global position_type, entry_price, tsl
    global entry_time, symbol, last_signal_candle
    global trade_taken_today

    spot=fyers.quotes({"symbols":SPOT_SYMBOL})["d"][0]["v"]["lp"]

    hist={
        "symbol":SPOT_SYMBOL,
        "resolution":"5",
        "date_format":"1",
        "range_from":(ist_today()-timedelta(days=5)).strftime("%Y-%m-%d"),
        "range_to":ist_today().strftime("%Y-%m-%d"),
        "cont_flag":"1"
    }

    df=get_stock_historical_data(hist)
    if len(df)<50:
        return

    df=calculate_trendline(df)

    last=df.iloc[-2]
    prev=df.iloc[-3]
    candle_time=df.index[-2]

    if last_signal_candle==candle_time:
        return

    buy=last.HA_Close>last.trendline and last.HA_Close>prev.HA_Close
    sell=last.HA_Close<last.trendline and last.HA_Close<prev.HA_Close

    if position_type is None and not trade_taken_today and ist_time()>=time(9,30):

        if buy:
            symbol=build_option_symbol(spot,"CE")
            position_type="CE"
        elif sell:
            symbol=build_option_symbol(spot,"PE")
            position_type="PE"
        else:
            return

        if not symbol:
            position_type=None
            return

        price=fyers.quotes({"symbols":symbol})["d"][0]["v"]["lp"]

        entry_price=price
        tsl=price-25
        entry_time=ist_now()
        trade_taken_today=True
        last_signal_candle=candle_time

        send_telegram(f"⚡ BUY {position_type} {symbol} @ {price}")

    if position_type:

        price=fyers.quotes({"symbols":symbol})["d"][0]["v"]["lp"]
        tsl=update_trailing(entry_price,price,tsl)

        if price<=tsl or ist_time()>=time(15,15):

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

            send_telegram(f"🔁 EXIT {symbol} PnL ₹{round(net,2)}")

            position_type=None

# ================= MAIN LOOP =================
send_telegram("🚀 BankNifty Monthly Algo Started (EC2 Tokyo)")

while True:
    try:
        now=ist_time()
        today=ist_today()

        # 3:30 AM RESET
        if now>=time(3,30) and last_reset_date!=today:
            daily_reset()
            last_reset_date=today

        # 9:00 TOKEN LOAD
        if time(9,0)<=now<time(15,30):
            if token_load_date!=today:
                load_token()
                token_load_date=today

        # 9:15 MODEL LOAD
        if time(9,15)<=now<time(15,30):
            if model_load_date!=today:
                fyers=load_model()
                model_load_date=today

        # 9:30 STRATEGY START
        if time(9,30)<=now<=time(15,30) and model_load_date==today:
            run_strategy()

        t.sleep(2)

    except Exception as e:
        send_telegram(f"❌ Algo Error: {e}")
        t.sleep(5)
