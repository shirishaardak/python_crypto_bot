import os
import time
import requests
import pandas as pd
from datetime import datetime, timedelta
from dotenv import load_dotenv
import pandas_ta as ta

load_dotenv()

# ================= SETTINGS =================
SYMBOLS = ["BTCUSD", "ETHUSD"]

DEFAULT_CONTRACTS = {"BTCUSD": 100, "ETHUSD": 100}
CONTRACT_SIZE = {"BTCUSD": 0.001, "ETHUSD": 0.01}

TAKER_FEE = 0.0005
TIMEFRAME = "5m"
DAYS = 5

LAST_CANDLE_TIME = {}

BASE_DIR = os.getcwd()
SAVE_DIR = os.path.join(BASE_DIR, "data", "supertrend_reverse_strategy")
os.makedirs(SAVE_DIR, exist_ok=True)

TRADE_CSV = os.path.join(SAVE_DIR, "live_trades.csv")

# ================= UTIL =================
def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}")

def commission(price, qty, symbol):
    notional = price * CONTRACT_SIZE[symbol] * qty
    return notional * TAKER_FEE

def save_trade(trade):
    trade_copy = trade.copy()
    for t in ["entry_time", "exit_time"]:
        trade_copy[t] = trade_copy[t].strftime("%Y-%m-%d %H:%M:%S")

    pd.DataFrame([trade_copy]).to_csv(
        TRADE_CSV,
        mode="a",
        header=not os.path.exists(TRADE_CSV),
        index=False
    )

def save_processed_data(df, symbol):

    path = os.path.join(SAVE_DIR, f"{symbol}_processed.csv")

    out = pd.DataFrame({
        "time": df.index,
        "HA_open": df["HA_open"],
        "HA_high": df["HA_high"],
        "HA_low": df["HA_low"],
        "HA_close": df["HA_close"],
        "trendline": df["supertrend"]
    })

    out.to_csv(path, index=False)

# ================= DATA =================
def fetch_price(symbol):
    try:
        r = requests.get(f"https://api.india.delta.exchange/v2/tickers/{symbol}", timeout=5)
        return float(r.json()["result"]["mark_price"])
    except Exception:
        return None

def fetch_candles(symbol, resolution=TIMEFRAME, days=DAYS, tz="Asia/Kolkata"):

    start = int((datetime.now() - timedelta(days=days)).timestamp())

    params = {
        "resolution": resolution,
        "symbol": symbol,
        "start": str(start),
        "end": str(int(time.time()))
    }

    r = requests.get(
        "https://api.india.delta.exchange/v2/history/candles",
        params=params,
        timeout=10
    )

    df = pd.DataFrame(
        r.json()["result"],
        columns=["time","open","high","low","close","volume"]
    )

    df.rename(columns=str.title, inplace=True)

    df["Time"] = (
        pd.to_datetime(df["Time"], unit="s", utc=True)
        .dt.tz_convert(tz)
    )

    df.set_index("Time", inplace=True)
    df.sort_index(inplace=True)

    return df.astype(float).dropna()

# ================= HEIKIN ASHI =================
def calculate_heikin_ashi(df):

    ha = df.copy()

    ha["HA_close"] = (ha["Open"]+ha["High"]+ha["Low"]+ha["Close"]) / 4

    ha_open=[(ha["Open"].iloc[0]+ha["Close"].iloc[0])/2]

    for i in range(1,len(ha)):
        ha_open.append((ha_open[i-1]+ha["HA_close"].iloc[i-1])/2)

    ha["HA_open"]=ha_open
    ha["HA_high"]=ha[["High","HA_open","HA_close"]].max(axis=1)
    ha["HA_low"]=ha[["Low","HA_open","HA_close"]].min(axis=1)

    return ha

# ================= SUPER TREND =================
def calculate_supertrend(df, length=10, multiplier=3.0):

    st = ta.supertrend(
        high=df["HA_high"],
        low=df["HA_low"],
        close=df["HA_close"],
        length=length,
        multiplier=multiplier
    )

    df = pd.concat([df, st], axis=1)

    df["supertrend"] = df[f"SUPERT_{length}_{multiplier}"]
    df["st_dir"] = df[f"SUPERTd_{length}_{multiplier}"]

    return df

# ================= CANDLE CHECK =================
def is_new_candle(symbol, df):

    last_closed_time = df.index[-2]

    if symbol not in LAST_CANDLE_TIME:
        LAST_CANDLE_TIME[symbol]=last_closed_time
        return True

    if last_closed_time!=LAST_CANDLE_TIME[symbol]:
        LAST_CANDLE_TIME[symbol]=last_closed_time
        return True

    return False

# ================= STRATEGY =================
def process_symbol(symbol, df, price, state, allow_entry):

    df=calculate_heikin_ashi(df)
    df=calculate_supertrend(df)

    save_processed_data(df, symbol)

    last=df.iloc[-2]
    prev=df.iloc[-3]

    pos=state["position"]
    now=datetime.now()

    if allow_entry and pos is None:

        if last.st_dir==1 and prev.st_dir==-1:
            state["position"]={
                "side":"long",
                "entry":price,
                "qty":DEFAULT_CONTRACTS[symbol],
                "entry_time":now
            }
            log(f"🟢 {symbol} LONG ENTRY @ {price}")
            return

        if last.st_dir==-1 and prev.st_dir==1:
            state["position"]={
                "side":"short",
                "entry":price,
                "qty":DEFAULT_CONTRACTS[symbol],
                "entry_time":now
            }
            log(f"🔴 {symbol} SHORT ENTRY @ {price}")
            return

    if pos:

        exit_trade=False

        if pos["side"]=="long" and last.st_dir==-1:
            pnl=(price-pos["entry"])*CONTRACT_SIZE[symbol]*pos["qty"]
            exit_trade=True

        elif pos["side"]=="short" and last.st_dir==1:
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

            emoji="🟢" if net>0 else "🔴"
            log(f"{emoji} {symbol} EXIT @ {price} PNL: {round(net,6)}")

            state["position"]=None

# ================= MAIN =================
def run():

    state={s:{"position":None} for s in SYMBOLS}

    log("🚀 HA + pandas_ta Supertrend LIVE")

    while True:

        for symbol in SYMBOLS:

            df=fetch_candles(symbol)

            if df is None or len(df)<100:
                continue

            price=fetch_price(symbol)

            if price is None:
                continue

            new_candle=is_new_candle(symbol,df)

            if state[symbol]["position"]:
                process_symbol(symbol,df,price,state[symbol],False)

            elif new_candle:
                process_symbol(symbol,df,price,state[symbol],True)

        time.sleep(5)

if __name__=="__main__":
    run()
