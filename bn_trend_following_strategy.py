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
entry_time=None
symbol=None
last_signal_candle=None
last_reset_date=None
token_load_date=None
model_load_date=None
last_exit_time=None

daily_ce_symbol=None
daily_pe_symbol=None
daily_strike=None

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
    global symbol, last_signal_candle, last_exit_time
    global daily_ce_symbol, daily_pe_symbol, daily_strike

    position_type=None
    entry_price=0
    entry_time=None
    symbol=None
    last_signal_candle=None
    last_exit_time=None

    daily_ce_symbol=None
    daily_pe_symbol=None
    daily_strike=None

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

# ================= LOCK STRIKE =================
def lock_atm_strike():
    global daily_ce_symbol, daily_pe_symbol, daily_strike

    spot=fyers.quotes({"symbols":SPOT_SYMBOL})["d"][0]["v"]["lp"]
    daily_strike=int(round(spot/100)*100)
    expiry=date.today().strftime("%y%b").upper()

    daily_ce_symbol=f"NSE:BANKNIFTY{expiry}{daily_strike}CE"
    daily_pe_symbol=f"NSE:BANKNIFTY{expiry}{daily_strike}PE"

    send_telegram(
        f"🔒 ATM Locked @ 9:20\n"
        f"Strike: {daily_strike}\n"
        f"CE: {daily_ce_symbol}\n"
        f"PE: {daily_pe_symbol}"
    )

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
            trendline=data["HA_Low"].iloc[i]
        elif data["HA_Low"].iloc[i]==data["smoothed_low"].iloc[i]:
            trendline=data["HA_High"].iloc[i]
        data.loc[i,"trendline"]=trendline

    data.index=df.index
    return data

# ================= STRATEGY =================
def run_strategy():

    global position_type, entry_price
    global entry_time, symbol, last_signal_candle, last_exit_time

    if last_exit_time and (ist_now()-last_exit_time).seconds<300:
        return

    ce_symbol=daily_ce_symbol
    pe_symbol=daily_pe_symbol

    if not ce_symbol or not pe_symbol:
        return

    hist_template={
        "resolution":"5",
        "date_format":"1",
        "range_from":(ist_today()-timedelta(days=5)).strftime("%Y-%m-%d"),
        "range_to":ist_today().strftime("%Y-%m-%d"),
        "cont_flag":"1"
    }

    # ===== CE DATA =====
    hist_ce=hist_template.copy()
    hist_ce["symbol"]=ce_symbol
    df_ce=get_stock_historical_data(hist_ce)

    # ===== PE DATA =====
    hist_pe=hist_template.copy()
    hist_pe["symbol"]=pe_symbol
    df_pe=get_stock_historical_data(hist_pe)

    if len(df_ce)<50 or len(df_pe)<50:
        return

    df_ce=calculate_trendline(df_ce)
    df_pe=calculate_trendline(df_pe)

    last_ce=df_ce.iloc[-2]
    prev_ce=df_ce.iloc[-3]
    last_pe=df_pe.iloc[-2]
    prev_pe=df_pe.iloc[-3]

    candle_time_ce=df_ce.index[-2]
    candle_time_pe=df_pe.index[-2]

    if last_signal_candle in [candle_time_ce,candle_time_pe]:
        return

    buy_call=last_ce.HA_Close>last_ce.trendline and last_ce.HA_Close>prev_ce.HA_Close
    buy_put=last_pe.HA_Close>last_pe.trendline and last_pe.HA_Close>prev_pe.HA_Close

    # ================= ENTRY =================
    if position_type is None and ist_time()>=time(9,30):

        if buy_call:
            symbol=ce_symbol
            position_type="CE"
            last_signal_candle=candle_time_ce

        elif buy_put:
            symbol=pe_symbol
            position_type="PE"
            last_signal_candle=candle_time_pe
        else:
            return

        price=fyers.quotes({"symbols":symbol})["d"][0]["v"]["lp"]
        entry_price=price
        entry_time=ist_now()

        send_telegram(f"⚡ BUY {symbol} @ {price}")

    # ================= EXIT =================
    if position_type:

        hist_exit=hist_template.copy()
        hist_exit["symbol"]=symbol
        df_exit=get_stock_historical_data(hist_exit)

        if len(df_exit)<50:
            return

        df_exit=calculate_trendline(df_exit)
        last=df_exit.iloc[-2]

        price=fyers.quotes({"symbols":symbol})["d"][0]["v"]["lp"]

        exit_signal=False

        if last.HA_Close<last.trendline:
            exit_signal=True

        if ist_time()>=time(15,15):
            exit_signal=True

        if exit_signal:

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

            send_telegram(f"🔁 EXIT {symbol} (Trendline Break) PnL ₹{round(net,2)}")

            position_type=None
            last_exit_time=ist_now()

# ================= MAIN LOOP =================
send_telegram("🚀 BankNifty Trendline Option Algo Started")

while True:
    try:
        now=ist_time()
        today=ist_today()

        if now>=time(3,30) and last_reset_date!=today:
            daily_reset()
            last_reset_date=today

        if time(9,0)<=now<time(15,30):
            if token_load_date!=today:
                load_token()
                token_load_date=today

        if time(9,20)<=now<time(9,25):
            if model_load_date!=today:
                fyers=load_model()
                model_load_date=today
                lock_atm_strike()

        if time(9,30)<=now<=time(15,30) and model_load_date==today:
            run_strategy()

        t.sleep(2)

    except Exception as e:
        send_telegram(f"❌ Algo Error: {e}")
        t.sleep(5)
