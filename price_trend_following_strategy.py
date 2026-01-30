import os
import time
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import pandas_ta as ta
from scipy.signal import argrelextrema
from dotenv import load_dotenv
load_dotenv()

# ================= SETTINGS =================
SYMBOLS = ["BTCUSD", "ETHUSD"]

DEFAULT_CONTRACTS = {"BTCUSD": 100, "ETHUSD": 100}
CONTRACT_SIZE = {"BTCUSD": 0.001, "ETHUSD": 0.01}
TAKER_FEE = 0.0005

TIMEFRAME = "15m"
DAYS = 5

TAKE_PROFIT = {"BTCUSD": 300, "ETHUSD": 30}
STOP_LOSS   = {"BTCUSD": 200, "ETHUSD": 20}
TRAIL_STEP  = {"BTCUSD": 100, "ETHUSD": 10}

# ================= TELEGRAM =================
TELEGRAM_BOT_TOKEN = os.getenv("BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("CHAT_ID")

BASE_DIR = os.getcwd()
SAVE_DIR = os.path.join(BASE_DIR, "data", "price_trend_following_strategy")
os.makedirs(SAVE_DIR, exist_ok=True)

TRADE_CSV = os.path.join(SAVE_DIR, "live_trades.csv")

def save_processed_data(ha, symbol):
    path = os.path.join(SAVE_DIR, f"{symbol}_processed.csv")
    out = pd.DataFrame({
        "time": ha.index,
        "HA_open": ha["HA_open"],
        "HA_high": ha["HA_high"],
        "HA_low": ha["HA_low"],
        "HA_close": ha["HA_close"],
        "trendline": ha["trendline"],
        "ATR": ha["ATR_HA"],
        "ATR_MA": ha["ATR_MA_HA"]
    })
    out.to_csv(path, index=False)

# ================= UTILITIES =================
def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}")

def send_telegram(msg):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(url, data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": msg,
            "parse_mode": "HTML"
        }, timeout=5)
    except Exception as e:
        log(f"Telegram error: {e}")

def commission(price, qty, symbol):
    return price * CONTRACT_SIZE[symbol] * qty * TAKER_FEE

def save_trade(trade):
    trade = trade.copy()
    for c in ["entry_time", "exit_time"]:
        trade[c] = trade[c].strftime("%Y-%m-%d %H:%M:%S")

    pd.DataFrame([trade]).to_csv(
        TRADE_CSV,
        mode="a",
        header=not os.path.exists(TRADE_CSV),
        index=False
    )

# ================= DATA =================
def fetch_price(symbol):
    r = requests.get(
        f"https://api.india.delta.exchange/v2/tickers/{symbol}",
        timeout=5
    )
    return float(r.json()["result"]["mark_price"])

def fetch_candles(symbol, resolution=TIMEFRAME, days=DAYS, tz="Asia/Kolkata"):
    start = int((datetime.now() - timedelta(days=days)).timestamp())

    r = requests.get(
        "https://api.india.delta.exchange/v2/history/candles",
        params={
            "resolution": resolution,
            "symbol": symbol,
            "start": str(start),
            "end": str(int(time.time()))
        },
        timeout=10
    )

    df = pd.DataFrame(
        r.json()["result"],
        columns=["time", "open", "high", "low", "close", "volume"]
    )

    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True).dt.tz_convert(tz)
    df.set_index("time", inplace=True)
    df = df.astype(float)
    return df.sort_index()

# ================= HEIKIN ASHI =================
def heikin_ashi(df):
    ha = pd.DataFrame(index=df.index)
    ha["HA_close"] = (df["open"] + df["high"] + df["low"] + df["close"]) / 4
    ha["HA_open"] = ha["HA_close"].shift(1)
    ha.iloc[0, ha.columns.get_loc("HA_open")] = (
        df["open"].iloc[0] + df["close"].iloc[0]
    ) / 2
    ha["HA_high"] = ha[["HA_open", "HA_close"]].join(df["high"]).max(axis=1)
    ha["HA_low"] = ha[["HA_open", "HA_close"]].join(df["low"]).min(axis=1)
    return ha

# ================= TRENDLINE =================
def calculate_trendline(df):
    # ----- Build HA -----
    ha = ta.ha(df["open"], df["high"], df["low"], df["close"]).reset_index(drop=True)

    data = df.copy().reset_index(drop=True)

    data["HA_open"]  = ha["HA_open"]
    data["HA_high"]  = ha["HA_high"]
    data["HA_low"]   = ha["HA_low"]
    data["HA_close"] = ha["HA_close"]

    # ----- Smooth HA -----
    data["high_smooth"] = ta.ema(data["HA_high"], length=5)
    data["low_smooth"]  = ta.ema(data["HA_low"], length=5)

    high_vals = data["HA_high"].values
    low_vals  = data["HA_low"].values

    max_idx = argrelextrema(high_vals, np.greater_equal, order=42)[0]
    min_idx = argrelextrema(low_vals,  np.less_equal,    order=42)[0]

    data["smoothed_high"] = np.nan
    data["smoothed_low"]  = np.nan

    data.iloc[max_idx, data.columns.get_loc("smoothed_high")] = data["HA_high"].iloc[max_idx]
    data.iloc[min_idx, data.columns.get_loc("smoothed_low")]  = data["HA_low"].iloc[min_idx]

    data[["smoothed_high","smoothed_low"]] = data[["smoothed_high","smoothed_low"]].ffill()

    # ----- TRENDLINE FROM HA -----
    data["trendline"] = np.nan
    trendline = data["HA_close"].iloc[0]

    data.iloc[0, data.columns.get_loc("trendline")] = trendline

    for i in range(1, len(data)):

        if data["HA_high"].iloc[i] == data["smoothed_high"].iloc[i]:
            trendline = data["HA_low"].iloc[i]

        elif data["HA_low"].iloc[i] == data["smoothed_low"].iloc[i]:
            trendline = data["HA_high"].iloc[i]

        data.loc[i, "trendline"] = trendline

    data.index = df.index
    return data

# ================= STRATEGY =================
def process_symbol(symbol, df, price, state):
    data = calculate_trendline(df)
    

    data["ATR_HA"] = ta.atr(
        data["HA_high"], data["HA_low"], data["HA_close"], length=14
    )
    data["ATR_MA_HA"] = data["ATR_HA"].rolling(21).mean()
    save_processed_data(data, symbol)
    last = data.iloc[-1]
    prev = data.iloc[-2]
    candle_time = data.index[-1]

    pos = state["position"]

    # ===== ENTRY (FIX: ONE PER CANDLE) =====
    if (
        pos is None
        and state["last_candle"] != candle_time
        and last.ATR_HA > last.ATR_MA_HA
    ):
        if (
            last.HA_close > last.trendline
            and last.HA_close > prev.HA_close
            and last.HA_close > prev.HA_open
        ):
            state["position"] = {
                "side": "long",
                "entry": price,
                "stop":last.trendline - STOP_LOSS[symbol],
                "qty": DEFAULT_CONTRACTS[symbol],
                "entry_time": datetime.now()
            }
            state["last_candle"] = candle_time
            send_telegram(f"📈 <b>{symbol} LONG ENTRY</b>\n{price}")
            return

        if (
            last.HA_close < last.trendline
            and last.HA_close < prev.HA_close
            and last.HA_close < prev.HA_open
        ):
            state["position"] = {
                "side": "short",
                "entry": price,
                "stop": last.trendline + STOP_LOSS[symbol],
                "qty": DEFAULT_CONTRACTS[symbol],
                "entry_time": datetime.now()
            }
            state["last_candle"] = candle_time
            send_telegram(f"📉 <b>{symbol} SHORT ENTRY</b>\n{price}")
            return

    # ===== EXIT (FIX: STOP DOES NOT MOVE AGAINST YOU) =====
    if pos:
        exit_trade = False

        if pos["side"] == "long":
            # new_stop = last.trendline - STOP_LOSS[symbol]
            pos["stop"] = last.trendline - STOP_LOSS[symbol]
            if price < pos["stop"] or last.HA_close < last.trendline:
                exit_trade = True

        if pos["side"] == "short":
            # new_stop = 
            pos["stop"] = last.trendline + STOP_LOSS[symbol]
            if price > pos["stop"] or last.HA_close > last.trendline:
                exit_trade = True

        if exit_trade:
            pnl = (
                (price - pos["entry"]) if pos["side"] == "long"
                else (pos["entry"] - price)
            ) * CONTRACT_SIZE[symbol] * pos["qty"]

            net = pnl - commission(price, pos["qty"], symbol)

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

            send_telegram(
                f"✅ <b>{symbol} {pos['side'].upper()} EXIT</b>\n"
                f"Net PnL: {round(net, 6)}"
            )
            state["position"] = None

# ================= MAIN =================
def run():
    if not os.path.exists(TRADE_CSV):
        pd.DataFrame(columns=[
            "entry_time", "exit_time", "symbol", "side",
            "entry_price", "exit_price", "qty", "net_pnl"
        ]).to_csv(TRADE_CSV, index=False)

    state = {s: {"position": None, "last_candle": None} for s in SYMBOLS}

    send_telegram("🚀 <b>Heikin Ashi Bot Started (Execution Fixed)</b>")

    while True:
        try:
            for symbol in SYMBOLS:
                df = fetch_candles(symbol)
                if len(df) < 100:
                    continue

                price = fetch_price(symbol)
                process_symbol(symbol, df, price, state[symbol])
                

            time.sleep(20)

        except Exception as e:
            log(e)
            time.sleep(5)

if __name__ == "__main__":
    run()
