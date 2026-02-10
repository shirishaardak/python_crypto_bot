import os
import time
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import pandas_ta as ta
from dotenv import load_dotenv
from scipy.signal import argrelextrema

load_dotenv()

# ================= TELEGRAM =================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

_last_tg = {}

def send_telegram(msg):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url,json={"chat_id":TELEGRAM_CHAT_ID,"text":msg},timeout=5)
    except:
        pass

def log(msg,tg=False):
    text=f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(text)
    if tg:
        send_telegram(text)

# ================= SETTINGS =================
SYMBOLS=["BTCUSD","ETHUSD"]

DEFAULT_CONTRACTS={"BTCUSD":100,"ETHUSD":100}
CONTRACT_SIZE={"BTCUSD":0.001,"ETHUSD":0.01}
TAKER_FEE=0.0005

TIMEFRAME="5m"
DAYS=5

COOLDOWN_SECONDS=120

MIN_DISTANCE={"BTCUSD":50,"ETHUSD":5}

BASE_DIR=os.getcwd()
SAVE_DIR=os.path.join(BASE_DIR,"data","trendline_reverse_strategy")
os.makedirs(SAVE_DIR,exist_ok=True)

TRADE_CSV=os.path.join(SAVE_DIR,"live_trades.csv")

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
# ================= UTIL =================
def commission(price,qty,symbol):
    notional=price*CONTRACT_SIZE[symbol]*qty
    return notional*TAKER_FEE

def save_trade(trade):
    trade_copy=trade.copy()
    for t in ["entry_time","exit_time"]:
        trade_copy[t]=trade_copy[t].strftime("%Y-%m-%d %H:%M:%S")

    pd.DataFrame([trade_copy]).to_csv(
        TRADE_CSV,
        mode="a",
        header=not os.path.exists(TRADE_CSV),
        index=False
    )

# ================= DATA =================
def fetch_price(symbol):
    try:
        r=requests.get(f"https://api.india.delta.exchange/v2/tickers/{symbol}",timeout=5)
        return float(r.json()["result"]["mark_price"])
    except:
        return None

def fetch_candles(symbol):
    start=int((datetime.now()-timedelta(days=DAYS)).timestamp())

    params={
        "resolution":TIMEFRAME,
        "symbol":symbol,
        "start":str(start),
        "end":str(int(time.time()))
    }

    try:
        r=requests.get(
            "https://api.india.delta.exchange/v2/history/candles",
            params=params,
            timeout=10
        )

        df=pd.DataFrame(
            r.json()["result"],
            columns=["time","open","high","low","close","volume"]
        )

        df.rename(columns=str.title,inplace=True)

        df["Time"]=(
            pd.to_datetime(df["Time"],unit="s",utc=True)
            .dt.tz_convert("Asia/Kolkata")
        )

        df.set_index("Time",inplace=True)
        df.sort_index(inplace=True)

        return df.astype(float)

    except:
        return None

# ================= HEIKIN ASHI =================
def calculate_heikin_ashi(df):
    ha=df.copy()

    ha["HA_close"]=(ha["Open"]+ha["High"]+ha["Low"]+ha["Close"])/4

    ha_open=[]
    for i in range(len(ha)):
        if i==0:
            ha_open.append((ha["Open"].iloc[0]+ha["Close"].iloc[0])/2)
        else:
            ha_open.append((ha_open[i-1]+ha["HA_close"].iloc[i-1])/2)

    ha["HA_open"]=ha_open
    ha["HA_high"]=ha[["High","HA_open","HA_close"]].max(axis=1)
    ha["HA_low"]=ha[["Low","HA_open","HA_close"]].min(axis=1)

    return ha

# ================= TRENDLINE =================
def calculate_trendline(df):

    ha=df.copy().reset_index(drop=True)

    ha["ATR"]=ta.atr(ha["HA_high"],ha["HA_low"],ha["HA_close"],length=14)

    order=21

    ha["swing_high"]=np.nan
    ha["swing_low"]=np.nan

    high_idx=argrelextrema(
        ha["HA_high"].values,
        np.greater_equal,
        order=order
    )[0]

    low_idx=argrelextrema(
        ha["HA_low"].values,
        np.less_equal,
        order=order
    )[0]

    ha.iloc[high_idx,ha.columns.get_loc("swing_high")]=ha.iloc[high_idx]["HA_high"].values
    ha.iloc[low_idx,ha.columns.get_loc("swing_low")]=ha.iloc[low_idx]["HA_low"].values

    ha["trendline"]=np.nan
    trend=ha.loc[0,"HA_close"]
    ha.loc[0,"trendline"]=trend

    for i in range(1,len(ha)):

        if not np.isnan(ha.loc[i,"swing_high"]):
            trend=ha.loc[i,"HA_low"]

        elif not np.isnan(ha.loc[i,"swing_low"]):
            trend=ha.loc[i,"HA_high"]

        ha.loc[i,"trendline"]=trend

    return ha

# ================= STRATEGY =================
def process_symbol(symbol,df,price,state):

    df=calculate_heikin_ashi(df)
    ha=calculate_trendline(df)

    save_processed_data(ha, symbol)

    ha.index=df.index

    last=ha.iloc[-2]
    prev=ha.iloc[-3]

    candle_time=ha.index[-2]

    if state["last_candle"]==candle_time:
        return

    state["last_candle"]=candle_time

    pos=state["position"]
    now=datetime.now()

    if state["last_exit_time"]:
        if (now-state["last_exit_time"]).seconds<COOLDOWN_SECONDS:
            return

    if pos is None:

        if abs(last.HA_close-last.trendline)<MIN_DISTANCE[symbol]:
            return

        if (last.HA_close > last.trendline and
            last.HA_close > prev.HA_open and
            last.HA_close > prev.HA_close
            ):

            stop=last.HA_close-(last.ATR)

            state["position"]={
                "side":"long",
                "entry":price,
                "stop":stop,
                "qty":DEFAULT_CONTRACTS[symbol],
                "entry_time":now
            }

            log(f"🟢 {symbol} LONG ENTRY {price}",tg=True)
            return

        if  (last.HA_close < last.trendline and
            last.HA_close < prev.HA_open and
            last.HA_close < prev.HA_close
            ):


            stop=last.HA_close+(last.ATR)

            state["position"]={
                "side":"short",
                "entry":price,
                "stop":stop,
                "qty":DEFAULT_CONTRACTS[symbol],
                "entry_time":now
            }

            log(f"🔴 {symbol} SHORT ENTRY {price}",tg=True)
            return

    if pos:

        exit_trade=False
        pnl=0

        if pos["side"]=="long":
            if price<=pos["stop"] or last.HA_close < last.trendline:
                pnl=(price-pos["entry"])*CONTRACT_SIZE[symbol]*pos["qty"]
                exit_trade=True

        if pos["side"]=="short":
            if price>=pos["stop"] or last.HA_close > last.trendline:
                pnl=(pos["entry"]-price)*CONTRACT_SIZE[symbol]*pos["qty"]
                exit_trade=True

        if exit_trade:

            fee=commission(price,pos["qty"],symbol)
            net=pnl-fee

            save_trade({
                "symbol":symbol,
                "side":pos["side"],
                "entry_price":pos["entry"],
                "exit_price":price,
                "qty":pos["qty"],
                "net_pnl":round(net,6),
                "entry_time":pos["entry_time"],
                "exit_time":now
            })

            log(f"{symbol} EXIT PNL {round(net,6)}",tg=True)

            state["position"]=None
            state["last_exit_time"]=now

# ================= MAIN =================
def run():

    if not os.path.exists(TRADE_CSV):
        pd.DataFrame(columns=[
            "entry_time","exit_time","symbol","side",
            "entry_price","exit_price","qty","net_pnl"
        ]).to_csv(TRADE_CSV,index=False)

    state={
        s:{
            "position":None,
            "last_candle":None,
            "last_exit_time":None
        } for s in SYMBOLS
    }

    log("🚀 LIVE STRATEGY STARTED",tg=True)

    while True:

        try:
            for symbol in SYMBOLS:

                df=fetch_candles(symbol)
                if df is None or len(df)<100:
                    continue

                price=fetch_price(symbol)
                if price is None:
                    continue

                process_symbol(symbol,df,price,state[symbol])

            time.sleep(5)

        except Exception as e:
            log(f"Runtime error {e}",tg=True)
            time.sleep(5)

if __name__=="__main__":
    run()
