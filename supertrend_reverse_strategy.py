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

TELEGRAM_TOKEN=os.getenv("BOT_TOK")
TELEGRAM_CHAT_ID=os.getenv("CHAT_")

_last_tg={}

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
                "text":msg
            },
            timeout=5
        )

    except:
        pass


# ================= SETTINGS =================

SYMBOLS=["BTCUSD","ETHUSD"]

CONTRACTS={
"BTCUSD":100,
"ETHUSD":100
}

CONTRACT_SIZE={
"BTCUSD":0.001,
"ETHUSD":0.01
}

TAKER_FEE=0.0005

TIMEFRAME="3"

BASE_DIR=os.getcwd()

SAVE_DIR=os.path.join(BASE_DIR,"data","poc_pro_bot")

os.makedirs(SAVE_DIR,exist_ok=True)

TRADE_FILE=os.path.join(SAVE_DIR,"trades.csv")


# ================= UTIL =================

def log(msg):

    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


def commission(price,qty,symbol):

    return price*qty*CONTRACT_SIZE[symbol]*TAKER_FEE


# ================= FETCH DATA =================

def fetch_candles(symbol):

    try:

        r=requests.get(
            "https://api.india.delta.exchange/v2/history/candles",
            params={
                "symbol":symbol,
                "resolution":TIMEFRAME,
                "limit":300
            },
            timeout=10
        )

        js=r.json()

        if "result" not in js:
            log(f"{symbol} API issue")
            return None

        data=js["result"]

        df=pd.DataFrame(
            data,
            columns=["time","open","high","low","close","volume"]
        )

        df["time"]=pd.to_datetime(df["time"],unit="s")

        df.set_index("time",inplace=True)

        df.sort_index(inplace=True)

        return df.astype(float)

    except Exception as e:

        log(f"{symbol} fetch error {e}")

        return None


# ================= VALUE AREA =================

def value_area(df):

    today=df[df.index.date==df.index[-1].date()]

    bins=np.round(today["close"],1)

    vp=today.groupby(bins)["volume"].sum()

    poc=vp.idxmax()

    total=vp.sum()

    sorted_vol=vp.sort_values(ascending=False)

    cum=0
    prices=[]

    for p,v in sorted_vol.items():

        cum+=v
        prices.append(p)

        if cum>=total*0.7:
            break

    VAH=max(prices)
    VAL=min(prices)

    return VAH,VAL,poc


# ================= INDICATORS =================

def indicators(df):

    df["EMA200"]=ta.ema(df.close,200)

    df["ATR"]=ta.atr(df.high,df.low,df.close,14)

    df["VWAP"]=(df.close*df.volume).cumsum()/df.volume.cumsum()

    st=ta.supertrend(df.high,df.low,df.close,10,3)

    df["ST"]=st["SUPERT_10_3.0"]

    df["ST_DIR"]=st["SUPERTd_10_3.0"]

    return df


# ================= STRATEGY =================

def process(symbol,df,state):

    df=indicators(df)

    if len(df)<210:
        return

    price=df.close.iloc[-2]

    ema=df.EMA200.iloc[-2]

    atr=df.ATR.iloc[-2]

    vwap=df.VWAP.iloc[-2]

    st_dir=df.ST_DIR.iloc[-2]

    VAH,VAL,POC=value_area(df)

    pos=state["position"]

    if atr/price > 0.025:
        return

    # ================= ENTRY =================

    if pos is None:

        # LONG SETUP
        if price < VAL and st_dir==1 and price>ema:

            state["position"]={
            "side":"long",
            "entry":price,
            "target":POC,
            "stop":VAL,
            "qty":CONTRACTS[symbol],
            "entry_time":datetime.now()
            }

            log(f"{symbol} LONG {price}")

            send_telegram(f"{symbol} LONG {price}")

        # SHORT SETUP
        elif price > VAH and st_dir==-1 and price<ema:

            state["position"]={
            "side":"short",
            "entry":price,
            "target":POC,
            "stop":VAH,
            "qty":CONTRACTS[symbol],
            "entry_time":datetime.now()
            }

            log(f"{symbol} SHORT {price}")

            send_telegram(f"{symbol} SHORT {price}")


    # ================= EXIT =================

    if pos:

        if pos["side"]=="long":

            if price>=pos["target"] or price<pos["stop"]:
                exit_trade(symbol,price,pos,state)

        else:

            if price<=pos["target"] or price>pos["stop"]:
                exit_trade(symbol,price,pos,state)


# ================= EXIT =================

def exit_trade(symbol,price,pos,state):

    pnl=(

        (price-pos["entry"])
        if pos["side"]=="long"
        else (pos["entry"]-price)

    )*CONTRACT_SIZE[symbol]*pos["qty"]

    net=pnl-commission(price,pos["qty"],symbol)

    trade={

    "entry_time":pos["entry_time"],
    "exit_time":datetime.now(),
    "symbol":symbol,
    "side":pos["side"],
    "entry":pos["entry"],
    "exit":price,
    "qty":pos["qty"],
    "pnl":round(net,6)

    }

    pd.DataFrame([trade]).to_csv(
        TRADE_FILE,
        mode="a",
        header=not os.path.exists(TRADE_FILE),
        index=False
    )

    log(f"{symbol} EXIT {net}")

    send_telegram(f"{symbol} EXIT PnL {round(net,6)}")

    state["position"]=None


# ================= MAIN =================

def run():

    log("PRO BOT STARTED")

    send_telegram("POC PRO BOT STARTED")

    state={s:{"position":None} for s in SYMBOLS}

    last_candle=None

    while True:

        try:

            for symbol in SYMBOLS:

                df=fetch_candles(symbol)

                if df is None:
                    continue

                candle=df.index[-1]

                if last_candle==candle:
                    continue

                last_candle=candle

                process(symbol,df,state[symbol])

            time.sleep(30)

        except Exception:

            log("ERROR")

            log(traceback.format_exc())

            send_telegram("BOT ERROR")

            time.sleep(30)


if __name__=="__main__":

    run()