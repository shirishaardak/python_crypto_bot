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
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

_last_tg = {}

BOT_NAME = "trend_following_strategy"

def send_telegram(msg, key=None, cooldown=30):
    try:
        now = time.time()

        if key and key in _last_tg and now - _last_tg[key] < cooldown:
            return

        if key:
            _last_tg[key] = now

        # ADD BOT NAME PREFIX
        msg = f"{BOT_NAME} | {msg}"

        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

        requests.post(
            url,
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg},
            timeout=5
        )

    except Exception:
        pass


# ================= SETTINGS =================
SYMBOLS = ["BTCUSD", "ETHUSD"]

DEFAULT_CONTRACTS = {"BTCUSD": 100, "ETHUSD": 100}
CONTRACT_SIZE = {"BTCUSD": 0.001, "ETHUSD": 0.01}

TAKER_FEE = 0.0005

TIMEFRAME = "15m"
DAYS = 5

TAKE_PROFIT = {"BTCUSD": 300, "ETHUSD": 30}
STOP_LOSS   = {"BTCUSD": 100, "ETHUSD": 10}

ADX_LENGTH = 9
ADX_THRESHOLD = 25

BASE_DIR = os.getcwd()
SAVE_DIR = os.path.join(BASE_DIR, "data", "trend_following_strategy")
os.makedirs(SAVE_DIR, exist_ok=True)

TRADE_CSV = os.path.join(SAVE_DIR, "live_trades.csv")

def save_processed_data(df, ha, symbol):
    path = os.path.join(SAVE_DIR, f"{symbol}_processed.csv")
    out = pd.DataFrame({
        "time": df.index,
        "HA_open": ha["HA_open"],
        "HA_high": ha["HA_high"],
        "HA_low": ha["HA_low"],
        "HA_close": ha["HA_close"],
        "trendline": ha["trendline"],
    })
    out.to_csv(path, index=False)

# ================= UTILITIES =================
def log(msg, tg=False, key=None):
    text = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(text)
    if tg:
        send_telegram(text, key)


def commission(price, qty, symbol):
    notional = price * CONTRACT_SIZE[symbol] * qty
    return notional * TAKER_FEE


def save_trade(trade):
    trade_copy = trade.copy()

    for t in ["entry_time", "exit_time"]:
        trade_copy[t] = trade_copy[t].strftime("%Y-%m-%d %H:%M:%S")

    cols = ["entry_time","exit_time","symbol","side","entry_price","exit_price","qty","net_pnl"]

    pd.DataFrame([trade_copy])[cols].to_csv(
        TRADE_CSV,
        mode="a",
        header=not os.path.exists(TRADE_CSV),
        index=False
    )


# ================= DATA =================
def fetch_price(symbol):
    try:
        r = requests.get(f"https://api.india.delta.exchange/v2/tickers/{symbol}", timeout=5)
        return float(r.json()["result"]["mark_price"])
    except Exception as e:
        log(f"{symbol} price error: {e}")
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

    ha = ta.ha(df["Open"], df["High"], df["Low"], df["Close"]).reset_index(drop=True)

    # ===== ADX =====
    adx = ta.adx(ha["HA_high"], ha["HA_low"], ha["HA_close"], length=ADX_LENGTH)
    ha["ADX"] = adx[f"ADX_{ADX_LENGTH}"]
    # === RANGE CHANNEL ===
    ha["UPPER"] = ha["HA_high"].rolling(21).max()
    ha["LOWER"] = ha["HA_low"].rolling(21).min()
    # === TRENDLINE LOGIC ===
    trendline = np.zeros(len(ha))
    trend = ha["HA_close"].iloc[0]
    trendline[0] = trend

    for i in range(1, len(ha)):
        ha_close = ha["HA_close"].iloc[i]
        ha_high = ha["HA_high"].iloc[i]
        ha_low = ha["HA_low"].iloc[i]

        upper = ha["UPPER"].iloc[i]
        lower = ha["LOWER"].iloc[i]

        if ha_high == upper :
            trend = ha_low
        elif ha_low == lower :
            trend = ha_high

        trendline[i] = trend

    ha["trendline"] = trendline
    return ha


# ================= STRATEGY =================
def process_symbol(symbol, df, price, state):

    ha = calculate_trendline(df)
    # save_processed_data(df, ha, symbol)
    last = ha.iloc[-2]
    prev = ha.iloc[-3]

    pos = state["position"]

    now = datetime.now()

    # ===== ADX CONDITIONS =====
    adx_ok = last.ADX > ADX_THRESHOLD
    adx_rising = last.ADX > ha.iloc[-3].ADX

    # ===== ENTRY =====
    if pos is None:

        if adx_ok:

            if last.HA_close > last.trendline and last.HA_close > prev.HA_close and last.HA_close > prev.HA_open:

                state["position"] = {
                    "side": "long",
                    "entry": price,
                    "stop": last.trendline - STOP_LOSS[symbol],
                    "tp": price + TAKE_PROFIT[symbol],
                    "qty": DEFAULT_CONTRACTS[symbol],
                    "entry_time": now
                }

                log(f"🟢 {symbol} LONG ENTRY @ {price}", tg=True)
                return


            if last.HA_close < last.trendline and last.HA_close < prev.HA_close and last.HA_close < prev.HA_open:

                state["position"] = {
                    "side": "short",
                    "entry": price,
                    "stop": last.trendline + STOP_LOSS[symbol],
                    "tp": price - TAKE_PROFIT[symbol],
                    "qty": DEFAULT_CONTRACTS[symbol],
                    "entry_time": now
                }

                log(f"🔴 {symbol} SHORT ENTRY @ {price}", tg=True)
                return


    # ===== EXIT =====
    if pos:

        exit_trade = False
        pnl = 0

        if pos["side"] == "long" :
            if price <= pos["stop"] or last.HA_close <= last.trendline:
                pnl = (price - pos["entry"]) * CONTRACT_SIZE[symbol] * pos["qty"]
                exit_trade = True


        if pos["side"] == "short":
            if price >= pos["stop"] or last.HA_close >= last.trendline:
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
                "net_pnl": round(net,6),
                "entry_time": pos["entry_time"],
                "exit_time": now
            })

            emoji = "🟢" if net > 0 else "🔴"

            log(f"{emoji} {symbol} EXIT @ {price} | PNL {round(net,6)}", tg=True)

            state["position"] = None


# ================= MAIN =================
def run():

    if not os.path.exists(TRADE_CSV):

        pd.DataFrame(columns=[
            "entry_time","exit_time","symbol",
            "side","entry_price","exit_price","qty","net_pnl"
        ]).to_csv(TRADE_CSV, index=False)

    state = {s: {"position": None, "last_candle_time": None} for s in SYMBOLS}

    log("🚀 HA Trendline + ADX Strategy LIVE (15m)", tg=True)

    while True:

        try:

            for symbol in SYMBOLS:

                df = fetch_candles(symbol)

                if df is None or len(df) < 100:
                    continue

                latest_candle_time = df.index[-1]

                if state[symbol]["last_candle_time"] == latest_candle_time:
                    continue

                state[symbol]["last_candle_time"] = latest_candle_time

                price = fetch_price(symbol)

                if price is None:
                    continue

                process_symbol(symbol, df, price, state[symbol])

            time.sleep(60)

        except Exception as e:

            log(f"Runtime error: {e}", tg=True)

            time.sleep(5)


if __name__ == "__main__":
    run()