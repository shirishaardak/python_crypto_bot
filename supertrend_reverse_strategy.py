import os
import time
import requests
import pandas as pd
import numpy as np
from datetime import datetime
import pandas_ta as ta
from dotenv import load_dotenv
import traceback

load_dotenv()

# ================= TELEGRAM =================

TELEGRAM_TOKEN = os.getenv("BOT_TOK")
TELEGRAM_CHAT_ID = os.getenv("CHAT_")

_last_tg = {}

def send_telegram(msg,key=None,cooldown=30):

    try:
        if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
            return

        now=time.time()

        if key:
            if key in _last_tg and now-_last_tg[key] < cooldown:
                return
            _last_tg[key]=now

        url=f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

        requests.post(
            url,
            json={
                "chat_id":TELEGRAM_CHAT_ID,
                "text":msg,
                "parse_mode":"Markdown"
            },
            timeout=5
        )

    except:
        print("Telegram error")

# ================= SETTINGS =================

SYMBOLS=["BTCUSD","ETHUSD"]

DEFAULT_CONTRACTS={"BTCUSD":100,"ETHUSD":100}
CONTRACT_SIZE={"BTCUSD":0.001,"ETHUSD":0.01}

TAKER_FEE=0.0005
TIMEFRAME="3m"

BASE_DIR=os.getcwd()
SAVE_DIR=os.path.join(BASE_DIR,"data","poc_magnet_bot")
os.makedirs(SAVE_DIR,exist_ok=True)

TRADE_CSV=os.path.join(SAVE_DIR,"live_trades.csv")

# ================= UTIL =================

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

def commission(price,qty,symbol):
    return price * CONTRACT_SIZE[symbol] * qty * TAKER_FEE

# ================= DATA =================

def fetch_candles(symbol):

    r=requests.get(
        "https://api.india.delta.exchange/v2/history/candles",
        params={
            "resolution":TIMEFRAME,
            "symbol":symbol,
            "limit":300
        },
        timeout=10
    )

    data=r.json()["result"]

    df=pd.DataFrame(
        data,
        columns=["time","open","high","low","close","volume"]
    )

    df["time"]=pd.to_datetime(df["time"],unit="s")
    df.set_index("time",inplace=True)
    df.sort_index(inplace=True)

    return df.astype(float)

# ================= HEIKIN ASHI =================

def heikin_ashi(df):

    ha=pd.DataFrame(index=df.index)

    ha["HA_close"]=(df.open+df.high+df.low+df.close)/4

    ha_open=[(df.open.iloc[0]+df.close.iloc[0])/2]

    for i in range(1,len(df)):
        ha_open.append((ha_open[i-1]+ha["HA_close"].iloc[i-1])/2)

    ha["HA_open"]=ha_open

    ha["HA_high"]=ha[["HA_open","HA_close"]].join(df.high).max(axis=1)
    ha["HA_low"]=ha[["HA_open","HA_close"]].join(df.low).min(axis=1)

    return ha

# ================= DAILY VALUE AREA =================

def value_area(df):

    session=df[df.index.date==df.index[-1].date()]

    price_bins=np.round(session["close"],1)

    vol_profile=session.groupby(price_bins)["volume"].sum()

    poc=vol_profile.idxmax()

    total_vol=vol_profile.sum()

    sorted_vol=vol_profile.sort_values(ascending=False)

    cum_vol=0
    prices=[]

    for p,v in sorted_vol.items():

        cum_vol+=v
        prices.append(p)

        if cum_vol>=total_vol*0.7:
            break

    VAH=max(prices)
    VAL=min(prices)

    return VAH,VAL,poc

# ================= INDICATORS =================

def indicators(df):

    df["EMA200"]=ta.ema(df.close,200)

    vwap=(df.close*df.volume).cumsum()/df.volume.cumsum()
    df["VWAP"]=vwap

    df["ATR"]=ta.atr(df.high,df.low,df.close,14)

    st=ta.supertrend(df.high,df.low,df.close,10,3)
    df["ST"]=st["SUPERT_10_3.0"]

    return df

# ================= STRATEGY =================

def process_symbol(symbol,df,state):

    df=indicators(df)

    ha=heikin_ashi(df)

    if len(df)<210:
        return

    last=ha.iloc[-2]
    price=df.close.iloc[-2]

    VAH,VAL,POC=value_area(df)

    ema=df["EMA200"].iloc[-2]
    vwap=df["VWAP"].iloc[-2]
    atr=df["ATR"].iloc[-2]

    pos=state["position"]

    bullish=last.HA_close>last.HA_open
    bearish=last.HA_close<last.HA_open

    # VOLATILITY FILTER
    if atr/price > 0.02:
        return

    # ================= ENTRY =================

    if pos is None:

        # LONG
        if price < VAL and bullish and price > ema and price < vwap:

            state["position"]={
                "side":"long",
                "entry":price,
                "target":POC,
                "stop":VAL,
                "qty":DEFAULT_CONTRACTS[symbol],
                "entry_time":datetime.now()
            }

            log(f"{symbol} LONG {price}")

            send_telegram(
                f"🟢 {symbol} LONG\nEntry {price}\nTarget {POC}\nStop {VAL}"
            )

        # SHORT
        elif price > VAH and bearish and price < ema and price > vwap:

            state["position"]={
                "side":"short",
                "entry":price,
                "target":POC,
                "stop":VAH,
                "qty":DEFAULT_CONTRACTS[symbol],
                "entry_time":datetime.now()
            }

            log(f"{symbol} SHORT {price}")

            send_telegram(
                f"🔴 {symbol} SHORT\nEntry {price}\nTarget {POC}\nStop {VAH}"
            )

    # ================= EXIT =================

    if pos:

        if pos["side"]=="long":

            if price >= pos["target"] or price < pos["stop"]:
                exit_trade(symbol,price,pos,state)

        else:

            if price <= pos["target"] or price > pos["stop"]:
                exit_trade(symbol,price,pos,state)

# ================= EXIT =================

def exit_trade(symbol,price,pos,state):

    pnl=(
        (price-pos["entry"]) if pos["side"]=="long"
        else (pos["entry"]-price)
    ) * CONTRACT_SIZE[symbol] * pos["qty"]

    net=pnl - commission(price,pos["qty"],symbol)

    trade={
        "entry_time":pos["entry_time"],
        "exit_time":datetime.now(),
        "symbol":symbol,
        "side":pos["side"],
        "entry_price":pos["entry"],
        "exit_price":price,
        "qty":pos["qty"],
        "net_pnl":round(net,6)
    }

    pd.DataFrame([trade]).to_csv(
        TRADE_CSV,
        mode="a",
        header=not os.path.exists(TRADE_CSV),
        index=False
    )

    log(f"{symbol} EXIT {net}")

    send_telegram(
        f"✅ {symbol} {pos['side']} EXIT\nPnL {round(net,6)}"
    )

    state["position"]=None

# ================= MAIN LOOP =================

def run():

    state={s:{"position":None} for s in SYMBOLS}

    log("BOT STARTED")

    send_telegram("🚀 Improved POC Magnet Bot Started")

    while True:

        try:

            for symbol in SYMBOLS:

                df=fetch_candles(symbol)

                if len(df)<200:
                    continue

                process_symbol(symbol,df,state[symbol])

            time.sleep(20)

        except Exception:

            log("ERROR")

            log(traceback.format_exc())

            send_telegram("⚠️ BOT ERROR")

            time.sleep(20)

if __name__=="__main__":
    run()