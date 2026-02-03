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

TREND_TF = "1h"
ENTRY_TF = "15m"
DAYS = 5

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
    trade_copy = trade.copy()
    for t in ["entry_time", "exit_time"]:
        trade_copy[t] = trade_copy[t].strftime("%Y-%m-%d %H:%M:%S")

    cols = [
        "entry_time","exit_time","symbol","side",
        "entry_price","exit_price","qty","net_pnl"
    ]

    pd.DataFrame([trade_copy])[cols].to_csv(
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

def fetch_candles(symbol, resolution, days=DAYS, tz="Asia/Kolkata"):
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

# ================= TRENDLINE (UNCHANGED) =================
def calculate_trendline(df):
    ha = ta.ha(df["open"], df["high"], df["low"], df["close"]).reset_index(drop=True)
    data = df.copy().reset_index(drop=True)

    data["HA_open"]  = ha["HA_open"]
    data["HA_high"]  = ha["HA_high"]
    data["HA_low"]   = ha["HA_low"]
    data["HA_close"] = ha["HA_close"]

    high_vals = data["HA_high"].values
    low_vals  = data["HA_low"].values

    max_idx = argrelextrema(high_vals, np.greater_equal, order=21)[0]
    min_idx = argrelextrema(low_vals,  np.less_equal,    order=21)[0]

    data["smoothed_high"] = np.nan
    data["smoothed_low"]  = np.nan

    data.iloc[max_idx, data.columns.get_loc("smoothed_high")] = data["HA_high"].iloc[max_idx]
    data.iloc[min_idx, data.columns.get_loc("smoothed_low")]  = data["HA_low"].iloc[min_idx]

    data[["smoothed_high","smoothed_low"]] = data[["smoothed_high","smoothed_low"]].ffill()

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
def process_symbol(symbol, state):
    now = datetime.now()
    df_1h = fetch_candles(symbol, TREND_TF)
    df_5m = fetch_candles(symbol, ENTRY_TF)

    if len(df_1h) < 50 or len(df_5m) < 10:
        return

    trend_1h = calculate_trendline(df_1h)
    trend_5m = calculate_trendline(df_5m)
    one_hour_trendline = trend_1h["trendline"].iloc[-2]

    last_5m_close = trend_5m["HA_close"].iloc[-2]
    last_5m_prv_close = trend_5m["HA_close"].iloc[-3]
    last_5m_open = trend_5m["HA_open"].iloc[-3]
    candle_time = trend_5m.index[-2]

    price = fetch_price(symbol)
    pos = state["position"]

    # ===== ENTRY =====
    if pos is None and now.minute % 15 == 0:

        if last_5m_close > one_hour_trendline and last_5m_close > last_5m_prv_close and last_5m_close > last_5m_open:
            state["position"] = {
                "side": "long",
                "entry": price,
                "qty": DEFAULT_CONTRACTS[symbol],
                "entry_time": datetime.now()
            }
            state["last_candle"] = candle_time
            send_telegram(f"📈 <b>{symbol} LONG ENTRY</b>\n{price}")
            return

        if last_5m_close < one_hour_trendline and last_5m_close < last_5m_prv_close and last_5m_close < last_5m_open:
            state["position"] = {
                "side": "short",
                "entry": price,
                "qty": DEFAULT_CONTRACTS[symbol],
                "entry_time": datetime.now()
            }
            state["last_candle"] = candle_time
            send_telegram(f"📉 <b>{symbol} SHORT ENTRY</b>\n{price}")
            return

    # ===== EXIT =====
    if pos:
        exit_trade = False

        if pos["side"] == "long" and last_5m_close < one_hour_trendline:
            exit_trade = True

        if pos["side"] == "short" and last_5m_close > one_hour_trendline:
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
            "entry_time","exit_time","symbol","side",
            "entry_price","exit_price","qty","net_pnl"
        ]).to_csv(TRADE_CSV, index=False)

    state = {s: {"position": None, "last_candle": None} for s in SYMBOLS}

    send_telegram("🚀 <b>MTF Bot Started (1H Trend / 5M Entry)</b>")

    while True:
        try:
            for symbol in SYMBOLS:
                process_symbol(symbol, state[symbol])
            time.sleep(20)
        except Exception as e:
            log(e)
            time.sleep(5)

if __name__ == "__main__":
    run()
