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

# ===== RISK SETTINGS =====
TAKE_PROFIT = {"BTCUSD": 300, "ETHUSD": 30}
STOP_LOSS   = {"BTCUSD": 100, "ETHUSD": 10}
TRAIL_STEP  = {"BTCUSD": 100, "ETHUSD": 10}

# ================= TELEGRAM =================
TELEGRAM_BOT_TOKEN = os.getenv("BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("CHAT_ID")

BASE_DIR = os.getcwd()
SAVE_DIR = os.path.join(BASE_DIR, "data", "price_trend_following_strategy")
os.makedirs(SAVE_DIR, exist_ok=True)

TRADE_CSV = os.path.join(SAVE_DIR, "live_trades.csv")

# ================= UTILITIES =================
def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}")


def send_telegram(msg):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": msg,
            "parse_mode": "HTML"
        }
        requests.post(url, data=payload, timeout=5)
    except Exception as e:
        log(f"Telegram error: {e}")


def commission(price, qty, symbol):
    notional = price * CONTRACT_SIZE[symbol] * qty
    return notional * TAKER_FEE


def save_trade(trade):
    trade = trade.copy()

    for col in ["entry_time", "exit_time"]:
        if isinstance(trade.get(col), datetime):
            trade[col] = trade[col].strftime("%Y-%m-%d %H:%M:%S")

    cols = [
        "entry_time", "exit_time", "symbol", "side",
        "entry_price", "exit_price", "qty", "net_pnl"
    ]

    pd.DataFrame([trade])[cols].to_csv(
        TRADE_CSV,
        mode="a",
        header=not os.path.exists(TRADE_CSV),
        index=False
    )


def save_processed_data(df, data, symbol):
    path = os.path.join(SAVE_DIR, f"{symbol}_processed.csv")

    out = pd.DataFrame({
        "time": df.index,
        "HA_open": data["HA_open"],
        "HA_high": data["HA_high"],
        "HA_low": data["HA_low"],
        "HA_close": data["HA_close"],
        "smoothed_high": data["smoothed_high"],
        "smoothed_low": data["smoothed_low"],
        "trendline": data["Trendline"],
        "ATR_HA": data["ATR_HA"],
        "ATR_MA_HA": data["ATR_MA_HA"],
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
    except Exception as e:
        send_telegram(f"⚠️ <b>{symbol} PRICE FETCH ERROR</b>\n{e}")
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
            columns=["time", "open", "high", "low", "close", "volume"]
        )

        df.rename(columns=str.title, inplace=True)
        df["Time"] = pd.to_datetime(df["Time"], unit="s", utc=True).dt.tz_convert(tz)
        df.set_index("Time", inplace=True)
        df.sort_index(inplace=True)

        for col in ["Open", "High", "Low", "Close", "Volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")

        return df.dropna()

    except Exception as e:
        send_telegram(f"⚠️ <b>{symbol} CANDLE FETCH ERROR</b>\n{e}")
        return None


# ================= HEIKIN ASHI =================
def heikin_ashi(df):
    ha = pd.DataFrame(index=df.index)
    ha["HA_close"] = (df["Open"] + df["High"] + df["Low"] + df["Close"]) / 4
    ha["HA_open"] = (df["Open"].iloc[0] + df["Close"].iloc[0]) / 2

    for i in range(1, len(df)):
        ha.iloc[i, ha.columns.get_loc("HA_open")] = (
            ha["HA_open"].iloc[i - 1] + ha["HA_close"].iloc[i - 1]
        ) / 2

    ha["HA_high"] = pd.concat([df["High"], ha["HA_open"], ha["HA_close"]], axis=1).max(axis=1)
    ha["HA_low"] = pd.concat([df["Low"], ha["HA_open"], ha["HA_close"]], axis=1).min(axis=1)

    return ha.reset_index(drop=True)


# ================= MAIN =================
def run():
    log("FULL HEIKIN ASHI ENGINE STARTED")
    send_telegram("🚀 <b>Heikin Ashi Trading Bot Started</b>")

    while True:
        try:
            time.sleep(20)
        except Exception as e:
            send_telegram(f"🚨 <b>BOT ERROR</b>\n{e}")
            time.sleep(5)


if __name__ == "__main__":
    run()
