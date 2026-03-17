import os
import time
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import pandas_ta as ta
from dotenv import load_dotenv

load_dotenv()

# ================= TELEGRAM =================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_T")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT")

_last_tg = {}

def send_telegram(msg,key=None,cooldown=30):

    try:

        now=time.time()

        if key and key in _last_tg and now-_last_tg[key]<cooldown:
            return

        if key:
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

SYMBOLS=["BTCUSD","ETHUSD"]

DEFAULT_CONTRACTS={
"BTCUSD":100,
"ETHUSD":100
}

CONTRACT_SIZE={
"BTCUSD":0.001,
"ETHUSD":0.01
}

TRAIL_STEP={
"BTCUSD":2,
"ETHUSD":1
}

TIMEFRAME="5m"
DAYS=3
TAKER_FEE=0.0005


# ================= PATH =================

BASE_DIR=os.getcwd()
SAVE_DIR=os.path.join(BASE_DIR,"data","poc_vwap_bot")

os.makedirs(SAVE_DIR,exist_ok=True)

TRADE_CSV=os.path.join(SAVE_DIR,"live_trades.csv")


# ================= LOG =================

def log(msg,tg=False,key=None):

    text=f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"

    print(text)

    if tg:
        send_telegram(text,key)


# ================= COMMISSION =================

def commission(price,qty,symbol):

    notional=price*CONTRACT_SIZE[symbol]*qty
    return notional*TAKER_FEE


# ================= SAVE TRADE =================

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


# ================= FETCH PRICE =================

def fetch_price(symbol):

    try:

        r=requests.get(
        f"https://api.india.delta.exchange/v2/tickers/{symbol}",
        timeout=5)

        return float(r.json()["result"]["mark_price"])

    except Exception as e:

        log(f"{symbol} price error {e}",tg=True)

        return None


# ================= FETCH CANDLES =================

def fetch_candles(symbol):

    start=int((datetime.now()-timedelta(days=DAYS)).timestamp())

    try:

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

        df=pd.DataFrame(
        r.json()["result"],
        columns=["time","open","high","low","close","volume"]
        )

        df.rename(columns=str.title,inplace=True)

        df["Time"]=pd.to_datetime(df["Time"],unit="s",utc=True).dt.tz_convert("Asia/Kolkata")

        df.set_index("Time",inplace=True)

        df.sort_index(inplace=True)

        df=df.astype(float)

        return df

    except Exception as e:

        log(f"{symbol} candle error {e}")

        return None


# ================= ORDERBOOK =================

def fetch_orderbook(symbol,depth=20):

    try:

        r=requests.get(
        f"https://api.india.delta.exchange/v2/l2orderbook/{symbol}",
        params={"depth":depth},
        timeout=5)

        data=r.json()["result"]

        return data["buy"],data["sell"]

    except:

        return None,None


# ================= IMBALANCE =================

def orderbook_imbalance(bids,asks):

    try:

        bid=sum(float(b[1]) for b in bids)
        ask=sum(float(a[1]) for a in asks)

        if bid+ask==0:
            return 0.5

        return bid/(bid+ask)

    except:
        return 0.5


# ================= LIQUIDITY WALL =================

def detect_liquidity_wall(bids,asks):

    try:

        bid_sizes=[float(b[1]) for b in bids]
        ask_sizes=[float(a[1]) for a in asks]

        avg_bid=np.mean(bid_sizes)
        avg_ask=np.mean(ask_sizes)

        bid_wall=max(bid_sizes)>avg_bid*2
        ask_wall=max(ask_sizes)>avg_ask*2

        return bid_wall,ask_wall

    except:
        return False,False


# ================= POC =================

def calculate_poc(df,bins=40):

    try:

        if len(df)<20:
            return None

        price=df["Close"]
        volume=df["Volume"]

        hist,edges=np.histogram(price,bins=bins,weights=volume)

        poc_index=np.argmax(hist)

        poc=(edges[poc_index]+edges[poc_index+1])/2

        return float(poc)

    except:
        return None


# ================= LIQUIDITY SWEEP =================

def detect_liquidity_sweep(df):

    last=df.iloc[-2]
    prev=df.iloc[-3]

    sweep_high=last.High>prev.High
    sweep_low=last.Low<prev.Low

    return sweep_high,sweep_low


# ================= INDICATORS =================

def calculate_indicators(df):

    st=ta.supertrend(df.High,df.Low,df.Close,length=21,multiplier=2.5)

    df["SUPERTREND"]=st["SUPERT_21_2.5"]

    df["VWAP"]=ta.vwap(df.High,df.Low,df.Close,df.Volume)

    return df


# ================= STRATEGY =================

def process_symbol(symbol,df,price,state):

    df=calculate_indicators(df)

    last=df.iloc[-2]

    supertrend=last.SUPERTREND
    vwap=last.VWAP

    poc=calculate_poc(df)

    if poc is None or vwap is None or supertrend is None:
        return

    sweep_high,sweep_low=detect_liquidity_sweep(df)

    bids,asks=fetch_orderbook(symbol)

    if bids is None:
        return

    imbalance=orderbook_imbalance(bids,asks)

    bid_wall,ask_wall=detect_liquidity_wall(bids,asks)

    pos=state["position"]

    now=datetime.now()

    log(f"{symbol} price:{price} vwap:{round(vwap,2)} poc:{round(poc,2)} imb:{round(imbalance,2)} sweepH:{sweep_high} sweepL:{sweep_low}")


    # ================= ENTRY =================

    if pos is None:

        if price>vwap and imbalance>0.5 and sweep_low:

            state["position"]={
            "side":"long",
            "entry":price,
            "stop":supertrend,
            "trail_price":price,
            "qty":DEFAULT_CONTRACTS[symbol],
            "entry_time":now
            }

            log(f"🟢 {symbol} LONG {price}",tg=True)

            return


        if price<vwap and imbalance<0.5 and sweep_high:

            state["position"]={
            "side":"short",
            "entry":price,
            "stop":supertrend,
            "trail_price":price,
            "qty":DEFAULT_CONTRACTS[symbol],
            "entry_time":now
            }

            log(f"🔴 {symbol} SHORT {price}",tg=True)

            return


    # ================= TRAILING =================

    if pos:

        step=TRAIL_STEP[symbol]

        if pos["side"]=="long":

            if price-pos["trail_price"]>=step:

                pos["stop"]+=step
                pos["trail_price"]=price

            if price<=pos["stop"]:

                pnl=(price-pos["entry"])*CONTRACT_SIZE[symbol]*pos["qty"]

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

                log(f"{symbol} LONG EXIT {price} PNL {net}",tg=True)

                state["position"]=None


        if pos["side"]=="short":

            if pos["trail_price"]-price>=step:

                pos["stop"]-=step
                pos["trail_price"]=price

            if price>=pos["stop"]:

                pnl=(pos["entry"]-price)*CONTRACT_SIZE[symbol]*pos["qty"]

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

                log(f"{symbol} SHORT EXIT {price} PNL {net}",tg=True)

                state["position"]=None


# ================= MAIN =================

def run():

    if not os.path.exists(TRADE_CSV):

        pd.DataFrame(columns=[
        "entry_time","exit_time","symbol","side",
        "entry_price","exit_price","qty","net_pnl"
        ]).to_csv(TRADE_CSV,index=False)


    state={s:{"position":None,"last_candle":None} for s in SYMBOLS}

    log("🚀 POC VWAP ORDERBOOK BOT STARTED",tg=True)


    while True:

        try:

            for symbol in SYMBOLS:

                df=fetch_candles(symbol)

                if df is None or len(df)<100:
                    continue

                latest=df.index[-1]

                if state[symbol]["last_candle"]==latest:
                    continue

                state[symbol]["last_candle"]=latest

                price=fetch_price(symbol)

                if price is None:
                    continue

                process_symbol(symbol,df,price,state[symbol])

            time.sleep(60)

        except Exception as e:

            log(f"runtime error {e}",tg=True)

            time.sleep(5)


if __name__=="__main__":

    run()