import os
import time
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import pandas_ta as ta

# ================= SETTINGS =================
SYMBOLS = ["BTCUSD", "ETHUSD"]

DEFAULT_CONTRACTS = {"BTCUSD": 100, "ETHUSD": 100}
CONTRACT_SIZE = {"BTCUSD": 0.001, "ETHUSD": 0.01}
TAKER_FEE = 0.0005

TIMEFRAME = "5m"
DAYS = 3

# ===== RISK SETTINGS =====
TAKE_PROFIT = {"BTCUSD": 200, "ETHUSD": 10}
STOP_LOSS   = {"BTCUSD": 300, "ETHUSD": 30}

BASE_DIR = os.getcwd()
SAVE_DIR = os.path.join(BASE_DIR, "data", "momentum_based_strategy")
os.makedirs(SAVE_DIR, exist_ok=True)

TRADE_CSV = os.path.join(SAVE_DIR, "live_trades.csv")

# ================= UTILITIES =================
def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}")

def commission(price, qty, symbol):
    notional = price * CONTRACT_SIZE[symbol] * qty
    return notional * TAKER_FEE

def save_trade(trade):
    trade = trade.copy()
    for t in ["entry_time", "exit_time"]:
        trade[t] = trade[t].strftime("%Y-%m-%d %H:%M:%S")

    cols = [
        "entry_time","exit_time","symbol","side",
        "entry_price","exit_price","qty","net_pnl"
    ]

    pd.DataFrame([trade])[cols].to_csv(
        TRADE_CSV,
        mode="a",
        header=not os.path.exists(TRADE_CSV),
        index=False
    )

def save_processed_data(df, ha, symbol):
    path = os.path.join(SAVE_DIR, f"{symbol}_processed.csv")

    out = pd.DataFrame({
        "time": df.index,
        "HA_open": ha["HA_open"],
        "HA_high": ha["HA_high"],
        "HA_low": ha["HA_low"],
        "HA_close": ha["HA_close"],
        "SUPERTREND": ha["SUPERTREND"],
        "Trendline": ha["Trendline"],
        "ATR": ha["ATR"],
        "ATR_MA": ha["ATR_MA"]
    })

    out.to_csv(path, index=False)

# ================= DATA =================
def fetch_price(symbol):
    try:
        r = requests.get(
            f"https://api.india.delta.exchange/v2/tickers/{symbol}",
            timeout=5
        )
        return float(r.json()["result"]["mark_price"])
    except:
        return None

def fetch_candles(symbol, resolution=TIMEFRAME, days=DAYS, tz="Asia/Kolkata"):
    start = int((datetime.now() - timedelta(days=days)).timestamp())

    params = {
        "resolution": resolution,
        "symbol": symbol,
        "start": str(start),
        "end": str(int(time.time()))
    }

    try:
        r = requests.get(
            "https://api.india.delta.exchange/v2/history/candles",
            params=params,
            timeout=10
        )
        r.raise_for_status()

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

        df = df.astype(float)
        return df.dropna()

    except Exception as e:
        log(f"{symbol} fetch error: {e}")
        return None

# ================= TRENDLINE =================
def calculate_trendline(df):
    ha = ta.ha(
        df["Open"], df["High"], df["Low"], df["Close"]
    ).reset_index(drop=True)

    ha["ATR"] = ta.atr(
        ha["HA_high"], ha["HA_low"], ha["HA_close"], length=14
    )
    ha["ATR_MA"] = ha["ATR"].rolling(21).mean()

    ha["SUPERTREND"] = ta.supertrend( high=ha["HA_high"],low=ha["HA_low"], close=ha["HA_close"], length=21, multiplier=2.5)['SUPERT_21_2.5']
    order = 5
    ha["UPPER"] = ha["HA_high"].rolling(order).max()
    ha["LOWER"] = ha["HA_low"].rolling(order).min()

    ha["Trendline"] = np.nan
    trend = ha.loc[0, "HA_close"]
    ha.loc[0, "Trendline"] = trend

    for i in range(1, len(ha)):
        if ha.loc[i, "HA_close"] > ha.loc[i-1, "UPPER"] and ha.loc[i, "HA_close"] > trend:
            trend = ha.loc[i, "LOWER"]
        elif ha.loc[i, "HA_close"] < ha.loc[i-1, "LOWER"] and ha.loc[i, "HA_close"] < trend:
            trend = ha.loc[i, "UPPER"]

        ha.loc[i, "Trendline"] = trend

    return ha

# ================= STRATEGY =================
def process_symbol(symbol, df, price, state):
    ha = calculate_trendline(df)
    save_processed_data(df, ha, symbol)

    last = ha.iloc[-2]
    prev = ha.iloc[-3]
    pos  = state["position"]

    now = datetime.now()

    # ===== ENTRY =====
    if now.minute % 5 == 0 and pos is None and last.ATR > last.ATR_MA:

        if (
            last.HA_close > last.Trendline and
            last.HA_close > prev.HA_close and
            last.HA_close > last.SUPERTREND
        ):
            state["position"] = {
                "side": "long",
                "entry": price,
                "stop": price - STOP_LOSS[symbol],
                "tp": price + TAKE_PROFIT[symbol],
                "qty": DEFAULT_CONTRACTS[symbol],
                "entry_time": datetime.now()
            }
            log(f"{symbol} LONG ENTRY @ {price}")
            return

        if (
            last.HA_close < last.Trendline and
            last.HA_close < prev.HA_close  and
            last.HA_close < last.SUPERTREND        
        ):
            state["position"] = {
                "side": "short",
                "entry": price,
                "stop": price + STOP_LOSS[symbol],
                "tp": price - TAKE_PROFIT[symbol],
                "qty": DEFAULT_CONTRACTS[symbol],
                "entry_time": datetime.now()
            }
            log(f"{symbol} SHORT ENTRY @ {price}")
            return

    # ===== EXIT =====
    if pos:
        exit_trade = False

        if pos["side"] == "long" and (last.HA_close < last.Trendline or price < pos['stop']):
            pnl = (price - pos["entry"]) * CONTRACT_SIZE[symbol] * pos["qty"]
            exit_trade = True

        if pos["side"] == "short" and (last.HA_close > last.Trendline or price > pos['stop']):
            pnl = (pos["entry"] - price) * CONTRACT_SIZE[symbol] * pos["qty"]
            exit_trade = True

        if exit_trade:
            fee = commission(price, pos["qty"], symbol)
            net = pnl - fee

            save_trade({
                "symbol": symbol,
                "side": pos["side"],
                "entry_price": pos["entry"],
                "exit_price": price,
                "qty": pos["qty"],
                "net_pnl": round(net, 6),
                "entry_time": pos["entry_time"],
                "exit_time": datetime.now()
            })

            log(f"{symbol} {pos['side'].upper()} EXIT @ {price} | NET {round(net,6)}")
            state["position"] = None

# ================= MAIN =================
def run():
    if not os.path.exists(TRADE_CSV):
        pd.DataFrame(columns=[
            "entry_time","exit_time","symbol","side",
            "entry_price","exit_price","qty","net_pnl"
        ]).to_csv(TRADE_CSV, index=False)

    state = {s: {"position": None} for s in SYMBOLS}
    log("🚀 HA Trendline Breakout Strategy LIVE")

    while True:
        try:
            for symbol in SYMBOLS:
                df = fetch_candles(symbol)
                if df is None or len(df) < 100:
                    continue

                price = fetch_price(symbol)
                if price is None:
                    continue

                process_symbol(symbol, df, price, state[symbol])

            time.sleep(6)

        except Exception as e:
            log(f"Runtime error: {e}")
            time.sleep(5)

if __name__ == "__main__":
    run()
