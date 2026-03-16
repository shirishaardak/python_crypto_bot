import os
import time
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import pandas_ta as ta
from dotenv import load_dotenv
import traceback

load_dotenv()

# ================= TELEGRAM =================

TELEGRAM_TOKEN = os.getenv("TEL_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TEL_CHAT_ID")

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
            json={"chat_id":TELEGRAM_CHAT_ID,"text":msg},
            timeout=5
        )

    except:
        pass


# ================= SETTINGS =================

SYMBOLS = ["BTCUSD","ETHUSD"]

CONTRACTS={"BTCUSD":100,"ETHUSD":100}

CONTRACT_SIZE={"BTCUSD":0.001,"ETHUSD":0.01}

TAKER_FEE=0.0005

TIMEFRAME="3m"

DAYS=5

BASE_DIR=os.getcwd()
SAVE_DIR=os.path.join(BASE_DIR,"data","pro_liq_bot")
os.makedirs(SAVE_DIR,exist_ok=True)

TRADE_FILE=os.path.join(SAVE_DIR,"trades.csv")


# ================= UTIL =================

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


def commission(price,qty,symbol):
    return price*qty*CONTRACT_SIZE[symbol]*TAKER_FEE


# ================= FETCH CANDLES =================

def fetch_candles(symbol):

    try:

        start=int((datetime.now()-timedelta(days=DAYS)).timestamp())

        r=requests.get(
            "https://api.india.delta.exchange/v2/history/candles",
            params={
                "resolution":TIMEFRAME,
                "symbol":symbol,
                "start":str(start),
                "end":str(int(time.time()))
            },
            timeout=10
        )

        js=r.json()

        if "result" not in js:
            log(f"{symbol} API ERROR")
            return None

        df=pd.DataFrame(
            js["result"],
            columns=["time","open","high","low","close","volume"]
        )

        df["time"]=pd.to_datetime(df["time"],unit="s")

        df.set_index("time",inplace=True)

        df.sort_index(inplace=True)

        return df.astype(float)

    except Exception as e:

        log(f"{symbol} FETCH ERROR {e}")

        return None


# ================= LIVE PRICE =================

def fetch_price(symbol):

    try:

        r=requests.get(
            f"https://api.india.delta.exchange/v2/tickers/{symbol}",
            timeout=5
        )

        return float(r.json()["result"]["mark_price"])

    except:
        return None


# ================= ORDER BOOK =================

def order_book(symbol):

    try:

        r=requests.get(
            f"https://api.india.delta.exchange/v2/l2orderbook/{symbol}",
            timeout=5
        )

        data=r.json()["result"]

        bids=data["buy"][:10]
        asks=data["sell"][:10]

        bid_vol=sum(float(b[1]) for b in bids)
        ask_vol=sum(float(a[1]) for a in asks)

        return bid_vol,ask_vol

    except:

        return None,None


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

    dev=df.close.std()

    df["VWAP_U"]=df["VWAP"]+dev
    df["VWAP_L"]=df["VWAP"]-dev

    st=ta.supertrend(df.high,df.low,df.close,10,3)

    df["ST_DIR"]=st["SUPERTd_10_3.0"]

    return df


# ================= LIQUIDITY SWEEP =================

def liquidity_sweep(df):

    prev_high=df.high.iloc[-3]
    prev_low=df.low.iloc[-3]

    high=df.high.iloc[-2]
    low=df.low.iloc[-2]
    close=df.close.iloc[-2]

    sweep_high=high>prev_high and close<prev_high
    sweep_low=low<prev_low and close>prev_low

    return sweep_high,sweep_low


# ================= STRATEGY =================

def process(symbol,df,state):

    df=indicators(df)

    if len(df)<210:
        return

    price=fetch_price(symbol)

    if price is None:
        return

    ema=df.EMA200.iloc[-2]

    VAH,VAL,POC=value_area(df)

    vwap_u=df.VWAP_U.iloc[-2]
    vwap_l=df.VWAP_L.iloc[-2]

    st=df.ST_DIR.iloc[-2]

    sweep_high,sweep_low=liquidity_sweep(df)

    bid,ask=order_book(symbol)

    pos=state["position"]

    # ================= ENTRY =================

    if pos is None and bid and ask:

        # LONG

        if (
            sweep_low
            and price < VAL
            and price < vwap_l
            and st==1
            and bid > ask*1.2
        ):

            state["position"]={
                "side":"long",
                "entry":price,
                "target":POC,
                "stop":price*0.995,
                "qty":CONTRACTS[symbol],
                "trail":price
            }

            log(f"{symbol} LONG {price}")
            send_telegram(f"{symbol} LONG {price}")

        # SHORT

        elif (
            sweep_high
            and price > VAH
            and price > vwap_u
            and st==-1
            and ask > bid*1.2
        ):

            state["position"]={
                "side":"short",
                "entry":price,
                "target":POC,
                "stop":price*1.005,
                "qty":CONTRACTS[symbol],
                "trail":price
            }

            log(f"{symbol} SHORT {price}")
            send_telegram(f"{symbol} SHORT {price}")


    # ================= EXIT =================

    if pos:

        # trailing stop

        if pos["side"]=="long":

            if price>pos["trail"]:
                pos["trail"]=price

            stop=max(pos["stop"],pos["trail"]*0.995)

            if price>=pos["target"] or price<stop:
                exit_trade(symbol,price,pos,state)

        else:

            if price<pos["trail"]:
                pos["trail"]=price

            stop=min(pos["stop"],pos["trail"]*1.005)

            if price<=pos["target"] or price>stop:
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

        "entry_time":datetime.now(),
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

    log("LIQUIDITY PRO BOT STARTED")

    send_telegram("LIQUIDITY PRO BOT STARTED")

    state={s:{"position":None} for s in SYMBOLS}

    last_candle={s:None for s in SYMBOLS}

    while True:

        try:

            for symbol in SYMBOLS:

                df=fetch_candles(symbol)

                if df is None:
                    continue

                candle=df.index[-1]

                if last_candle[symbol]==candle:
                    continue

                last_candle[symbol]=candle

                process(symbol,df,state[symbol])

            time.sleep(10)

        except Exception:

            log("ERROR")

            log(traceback.format_exc())

            send_telegram("BOT ERROR")

            time.sleep(30)


if __name__=="__main__":

    run()