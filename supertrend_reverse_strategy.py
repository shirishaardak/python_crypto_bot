import os
import time
import requests
import pandas as pd
from datetime import datetime, timedelta
from dotenv import load_dotenv
import pandas_ta as ta
import numpy as np

load_dotenv()

# ================= TELEGRAM =================
TELEGRAM_TOKEN = os.getenv("BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("CHAT_ID")

_last_tg = {}

def send_telegram(msg, key=None, cooldown=30):
    try:
        now = time.time()
        if key and key in _last_tg and now - _last_tg[key] < cooldown:
            return
        if key:
            _last_tg[key] = now

        if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
            requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg}, timeout=5)
    except Exception as e:
        print("Telegram error:", e)


# ================= SETTINGS =================
SYMBOLS = ["BTCUSD", "ETHUSD"]

DEFAULT_CONTRACTS = {"BTCUSD": 100, "ETHUSD": 100}
CONTRACT_SIZE = {"BTCUSD": 0.001, "ETHUSD": 0.01}

TARGET_POINTS = {"BTCUSD": 200, "ETHUSD": 10}

TAKER_FEE = 0.0005
EXECUTION_TF = "5m"
DAYS = 5

BASE_DIR = os.getcwd()
SAVE_DIR = os.path.join(BASE_DIR, "data", "supertrend_reverse_strategy")
os.makedirs(SAVE_DIR, exist_ok=True)

TRADE_CSV = os.path.join(SAVE_DIR, "live_trades.csv")

LAST_CANDLE_TIME = {}

print("Saving files to:", SAVE_DIR)


# ================= UTIL =================
def log(msg, tg=False):
    text = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(text)
    if tg:
        send_telegram(text)


def commission(price, qty, symbol):
    notional = price * CONTRACT_SIZE[symbol] * qty
    return notional * TAKER_FEE


def save_trade(trade):
    trade_copy = trade.copy()
    trade_copy["entry_time"] = trade_copy["entry_time"].strftime("%Y-%m-%d %H:%M:%S")
    trade_copy["exit_time"] = trade_copy["exit_time"].strftime("%Y-%m-%d %H:%M:%S")

    cols = [
        "entry_time", "exit_time", "symbol", "side",
        "entry_price", "exit_price", "qty", "net_pnl"
    ]

    pd.DataFrame([trade_copy])[cols].to_csv(
        TRADE_CSV,
        mode="a",
        header=not os.path.exists(TRADE_CSV),
        index=False
    )


# ================= DATA =================
def fetch_price(symbol):
    try:
        r = requests.get(
            f"https://api.india.delta.exchange/v2/tickers/{symbol}",
            timeout=5
        )
        return float(r.json()["result"]["mark_price"])
    except Exception as e:
        print("Price fetch error:", e)
        return None


def fetch_candles(symbol, resolution):
    start = int((datetime.now() - timedelta(days=DAYS)).timestamp())

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

        df = pd.DataFrame(
            r.json()["result"],
            columns=["time","open","high","low","close","volume"]
        )

        df.rename(columns=str.title, inplace=True)

        df["Time"] = pd.to_datetime(
            df["Time"], unit="s", utc=True
        ).dt.tz_convert("Asia/Kolkata")

        df.set_index("Time", inplace=True)
        df.sort_index(inplace=True)

        return df.astype(float).dropna()

    except Exception as e:
        print("Candle fetch error:", e)
        return None


# ================= SAVE PROCESSED DATA =================
def save_processed_data(ha, symbol):
    try:
        path = os.path.join(SAVE_DIR, f"{symbol}_processed.csv")

        required_cols = [
            "HA_open", "HA_high", "HA_low", "HA_close",
            "SUPERTREND", "trendline", "UPPER", "LOWER", "ADX"
        ]

        missing = [c for c in required_cols if c not in ha.columns]
        if missing:
            print(f"[{symbol}] Missing columns:", missing)
            print("Available:", list(ha.columns))
            return

        out = ha[required_cols].copy()
        out["time"] = ha.index
        out = out.tail(500)

        out.to_csv(path, index=False)


    except Exception as e:
        print(f"Error saving processed data for {symbol}:", e)


# ================= HEIKIN ASHI =================
def calculate_heikin_ashi(df):
    ha = df.copy()

    ha["HA_close"] = (ha["Open"] + ha["High"] + ha["Low"] + ha["Close"]) / 4
    ha["HA_open"] = 0.0

    ha.iloc[0, ha.columns.get_loc("HA_open")] = (
        ha["Open"].iloc[0] + ha["Close"].iloc[0]
    ) / 2

    for i in range(1, len(ha)):
        ha.iloc[i, ha.columns.get_loc("HA_open")] = (
            ha["HA_open"].iloc[i-1] + ha["HA_close"].iloc[i-1]
        ) / 2

    ha["HA_high"] = ha[["High","HA_open","HA_close"]].max(axis=1)
    ha["HA_low"] = ha[["Low","HA_open","HA_close"]].min(axis=1)

    return ha


# ================= TRENDLINE =================
def calculate_trendline(df):

    df["ATR"] = ta.atr(df["HA_high"], df["HA_low"], df["HA_close"], length=14)

    adx = ta.adx(
        high=df["HA_high"],
        low=df["HA_low"],
        close=df["HA_close"],
        length=14
    )
    df["ADX"] = adx.filter(like="ADX").iloc[:, 0]

    st = ta.supertrend(
        high=df["HA_high"],
        low=df["HA_low"],
        close=df["HA_close"],
        length=7,
        multiplier=3
    )

    # SAFE supertrend extraction
    df["SUPERTREND"] = st.filter(like="SUPERT").iloc[:, 0]

    order = 21
    df["UPPER"] = df["HA_high"].rolling(order, min_periods=1).max()
    df["LOWER"] = df["HA_low"].rolling(order, min_periods=1).min()

    df["trendline"] = np.nan
    trend = df["HA_close"].iloc[0]
    df.iloc[0, df.columns.get_loc("trendline")] = trend

    for i in range(2, len(df)):
        curr_close = df["HA_close"].iloc[i]
        prev_close = df["HA_close"].iloc[i-1]
        prev_open  = df["HA_open"].iloc[i-1]

        if curr_close > prev_close and curr_close > prev_open and curr_close > trend:
            trend = df["HA_low"].iloc[i-2]
        elif curr_close < prev_close and curr_close < prev_open and curr_close < trend:
            trend = df["HA_high"].iloc[i-2]

        df.iloc[i, df.columns.get_loc("trendline")] = trend

    return df


# ================= STRATEGY =================
def process_symbol(symbol, df, price, state, allow_entry):

    ha = calculate_heikin_ashi(df)
    ha = calculate_trendline(ha)

    if len(ha) < 3:
        return

    save_processed_data(ha, symbol)

    last = ha.iloc[-2]
    prev = ha.iloc[-3]

    pos = state["position"]
    now = datetime.now()

    # ENTRY
    if allow_entry and pos is None:

        long_signal = (
            last.HA_close > last.SUPERTREND and
            last.HA_close > prev.UPPER
        )

        short_signal = (
            last.HA_close < last.SUPERTREND and
            last.HA_close < prev.LOWER
        )

        if long_signal or short_signal:

            side = "long" if long_signal else "short"
            qty = DEFAULT_CONTRACTS[symbol]

            state["position"] = {
                "side": side,
                "entry": price,
                "qty": qty,
                "entry_time": now
            }

            log(f"{symbol} {side.upper()} ENTRY @ {price}", tg=True)
            return

    # EXIT
    if pos:

        target_points = TARGET_POINTS[symbol]
        exit_trade = False

        if pos["side"] == "long":
            pnl = (price - pos["entry"]) * CONTRACT_SIZE[symbol] * pos["qty"]

            if price < last.SUPERTREND or price >= pos["entry"] + target_points:
                exit_trade = True

        else:
            pnl = (pos["entry"] - price) * CONTRACT_SIZE[symbol] * pos["qty"]

            if price > last.SUPERTREND or price <= pos["entry"] - target_points:
                exit_trade = True

        if exit_trade:

            fee = commission(price, pos["qty"], symbol) + \
                  commission(pos["entry"], pos["qty"], symbol)

            net = pnl - fee

            save_trade({
                "entry_time": pos["entry_time"],
                "exit_time": now,
                "symbol": symbol,
                "side": pos["side"],
                "entry_price": pos["entry"],
                "exit_price": price,
                "qty": pos["qty"],
                "net_pnl": round(net, 6)
            })

            log(f"{symbol} EXIT @ {price} | PNL: {round(net,6)}", tg=True)
            state["position"] = None


# ================= MAIN =================
def run():

    state = {s: {"position": None} for s in SYMBOLS}

    log("🚀 Strategy LIVE Started", tg=True)

    while True:
        try:
            for symbol in SYMBOLS:

                df = fetch_candles(symbol, EXECUTION_TF)
                if df is None or len(df) < 120:
                    continue

                price = fetch_price(symbol)
                if price is None:
                    continue

                new_candle = df.index[-2] != LAST_CANDLE_TIME.get(symbol)
                if new_candle:
                    LAST_CANDLE_TIME[symbol] = df.index[-2]

                if state[symbol]["position"]:
                    process_symbol(symbol, df, price, state[symbol], allow_entry=False)
                elif new_candle:
                    process_symbol(symbol, df, price, state[symbol], allow_entry=True)

            time.sleep(10)

        except Exception as e:
            log(f"Runtime error: {e}", tg=True)
            time.sleep(5)


if __name__ == "__main__":
    run()