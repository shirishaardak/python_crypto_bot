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
import pandas_ta as ta
from fyers_apiv3 import fyersModel
from datetime import datetime, time, timedelta, timezone
from dotenv import load_dotenv

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

# ================= TELEGRAM =================
BOT = os.getenv("TEL_BOT_TOKEN")
CHAT = os.getenv("TEL_CHAT_ID")

def send_telegram(msg):
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT}/sendMessage",
            json={"chat_id": CHAT, "text": msg},
            timeout=5
        )
    except:
        pass

# ================= CONFIG =================
CLIENT_ID = "98E1TAKD4T-100"
QTY = 30
TARGET_POINTS = 250
ATR_MULTIPLIER = 1.2
COMMISSION_RATE = 0.0004

TOKEN_FILE = "auth/api_key/access_token.txt"
TOKEN_CSV = "data/Trend_Following/instrument_token.csv"

folder = "data/Trend_Following"
os.makedirs(folder, exist_ok=True)
TRADES_FILE = f"{folder}/live_trades.csv"

# ================= STATE =================
fyers = None
token_df = None
CURR_position = 0
CURR_enter = 0
CURR_TSL = 0
CURR_enter_time = None
trade_active = False
entry_meta = {}

# ================= HEIKIN ASHI =================
def add_heiken_ashi(df):
    ha = df.copy()
    ha["HA_Close"] = (df["Open"]+df["High"]+df["Low"]+df["Close"])/4
    ha_open=[(df["Open"].iloc[0]+df["Close"].iloc[0])/2]
    for i in range(1,len(df)):
        ha_open.append((ha_open[i-1]+ha["HA_Close"].iloc[i-1])/2)
    ha["HA_Open"]=ha_open
    ha["HA_High"]=ha[["HA_Open","HA_Close","High"]].max(axis=1)
    ha["HA_Low"]=ha[["HA_Open","HA_Close","Low"]].min(axis=1)
    return ha

# ================= UTILS =================
def commission(price,qty):
    return round(price*qty*COMMISSION_RATE,6)

def calculate_pnl(entry,exit,qty,side):
    return round((exit-entry)*qty,6) if side=="BUY" else round((entry-exit)*qty,6)

def save_trade(data):
    df = pd.DataFrame([data])
    if not os.path.exists(TRADES_FILE):
        df.to_csv(TRADES_FILE, index=False)
    else:
        df.to_csv(TRADES_FILE, mode="a", header=False, index=False)

# ================= AUTH =================
def load_token():
    os.system("python auth/fyers_auth.py")
    return True

def load_model():
    token=open(TOKEN_FILE).read().strip()
    return fyersModel.FyersModel(
        client_id=CLIENT_ID,
        token=token,
        is_async=False,
        log_path=""
    )

# ================= DATA =================
def get_stock_historical_data(data,fyers):
    final_data=fyers.history(data=data)
    df=pd.DataFrame(final_data['candles'])
    df=df.rename(columns={0:'Date',1:'Open',2:'High',3:'Low',4:'Close',5:'V'})
    df['Date']=pd.to_datetime(df['Date'],unit='s')
    df.set_index("Date",inplace=True)
    return df

# ================= STRATEGY =================
def run_strategy():
    global CURR_position,CURR_enter,CURR_TSL
    global trade_active,CURR_enter_time,entry_meta

    CURR_SYMBOL="NSE:"+token_df.loc[0,"tradingsymbol"]
    CURR_price=fyers.quotes({"symbols":CURR_SYMBOL})["d"][0]["v"]["lp"]

    hist={
        "symbol":CURR_SYMBOL,
        "resolution":"5",
        "date_format":"1",
        "range_from":(ist_today()-timedelta(days=5)).strftime("%Y-%m-%d"),
        "range_to":ist_today().strftime("%Y-%m-%d"),
        "cont_flag":"1"
    }

    df=get_stock_historical_data(hist,fyers)
    if len(df)<50:
        return

    df=add_heiken_ashi(df)

    st=ta.supertrend(
        high=df["HA_High"],
        low=df["HA_Low"],
        close=df["HA_Close"],
        length=21,
        multiplier=2.5
    )
    df["SUPERTREND"]=st["SUPERT_21_2.5"]

    df["ATR"]=ta.atr(df["HA_High"],df["HA_Low"],df["HA_Close"],length=14)
    df["ATR_AM"]=ta.ema(df["ATR"],length=21)

    last=df.iloc[-2]
    prev=df.iloc[-3]

    atr_filter=(last.ATR>last.ATR_AM)

    # ===== ENTRY =====
    if not trade_active and ist_time()>=time(9,30):

        if not atr_filter:
            return

        if last.HA_Close > last.SUPERTREND and last.HA_Close > prev.HA_Close and last.HA_Close > prev.HA_Open:
            CURR_position=1
            side="BUY"
        elif last.HA_Close < last.SUPERTREND and last.HA_Close < prev.HA_Close and last.HA_Close < prev.HA_Open:
            CURR_position=-1
            side="SELL"
        else:
            return

        CURR_enter=CURR_price
        CURR_TSL=CURR_price-(last.ATR*ATR_MULTIPLIER) if CURR_position==1 else CURR_price+(last.ATR*ATR_MULTIPLIER)

        trade_active=True
        CURR_enter_time=ist_now()

        entry_meta={
            "entry_atr":last.ATR,
            "entry_atr_am":last.ATR_AM,
            "entry_supertrend":last.SUPERTREND
        }

        send_telegram(f"⚡ {side} @ {CURR_price}")

    # ===== TRAILING =====
    if trade_active:

        if CURR_position==1:
            CURR_TSL=max(CURR_TSL,CURR_price-(last.ATR*ATR_MULTIPLIER))
        else:
            CURR_TSL=min(CURR_TSL,CURR_price+(last.ATR*ATR_MULTIPLIER))

    # ===== EXIT =====
    exit_reason=None

    if trade_active:

        if CURR_position==1 and CURR_price<=CURR_TSL:
            exit_reason="ATR_TRAIL_EXIT"
        if CURR_position==-1 and CURR_price>=CURR_TSL:
            exit_reason="ATR_TRAIL_EXIT"

        if CURR_position==1 and (CURR_price-CURR_enter)>=TARGET_POINTS:
            exit_reason="TARGET_EXIT"
        if CURR_position==-1 and (CURR_enter-CURR_price)>=TARGET_POINTS:
            exit_reason="TARGET_EXIT"

        if ist_time()>=time(15,15):
            exit_reason="TIME_EXIT"

    if trade_active and exit_reason:

        side="BUY" if CURR_position==1 else "SELL"

        net=calculate_pnl(CURR_enter,CURR_price,QTY,side)-commission(CURR_price,QTY)

        save_trade({
            "entry_time": CURR_enter_time.isoformat(),
            "exit_time": ist_now().isoformat(),
            "symbol": CURR_SYMBOL,
            "side": side,
            "entry_price": CURR_enter,
            "exit_price": CURR_price,
            "qty": QTY,
            "net_pnl": net
        })

        send_telegram(f"🔁 EXIT | ₹{round(net,2)} | {exit_reason}")

        CURR_position=0
        trade_active=False

# ================= MAIN LOOP =================
send_telegram("🚀 ATR Managed HA Supertrend Algo Started")

token_loaded=model_loaded=symbols_loaded=False

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
            if not symbols_loaded:
                token_df=pd.read_csv(TOKEN_CSV)
                symbols_loaded=True

        if time(9,30)<=ist_time()<=market_close and symbols_loaded:
            run_strategy()

        t.sleep(2)

    except Exception as e:
        send_telegram(f"❌ Algo error {e}")
        t.sleep(5)
