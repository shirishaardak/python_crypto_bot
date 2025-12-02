import os
import time
import requests
import pandas as pd
from datetime import datetime, timedelta
import pandas_ta as ta
from scipy.signal import argrelextrema
import numpy as np
from delta_rest_client import DeltaRestClient, OrderType
from dotenv import load_dotenv
import traceback

load_dotenv()

# ================= SETTINGS ===============================
SYMBOLS = ["BTCUSD", "ETHUSD"]

ENTRY_MOVE = {"BTCUSD": 100, "ETHUSD": 10}
STOP_LOSS = {"BTCUSD": 300, "ETHUSD": 20}

PRODUCT_IDS = {"BTCUSD": 27, "ETHUSD": 3136}
DEFAULT_CONTRACTS = {"BTCUSD": 30, "ETHUSD": 30}
CONTRACT_SIZE = {"BTCUSD": 0.001, "ETHUSD": 0.01}
TAKER_FEE = 0.0005

# ================= TELEGRAM ===============================
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')


def send_telegram_message(msg: str) -> None:
    try:
        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
            print("⚠️ Telegram not configured.")
            return
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg}, timeout=5)
    except:
        pass


def send_short_error(prefix: str, details: str):
    send_telegram_message(f"❌ {prefix}\n{details}")


# ================= API CLIENT =============================
client = DeltaRestClient(
    base_url='https://api.india.delta.exchange',
    api_key=os.getenv('DELTA_API_KEY'),
    api_secret=os.getenv('DELTA_API_SECRET')
)

# ================= UTILITIES ==============================
def commission(price, contracts, symbol):
    return price * CONTRACT_SIZE[symbol] * contracts * TAKER_FEE


def fetch_ticker_price(symbol):
    try:
        url = f"https://api.india.delta.exchange/v2/tickers/{symbol}"
        r = requests.get(url, timeout=5).json()
        price = float(r["result"]["mark_price"])
        return price
    except Exception as e:
        send_short_error("TICKER ERROR", f"{symbol}\n{e}")
        return None


def fetch_candles(symbol, resolution="5m", days=1, tz='Asia/Kolkata'):
    headers = {'Accept': 'application/json'}
    start = int((datetime.now() - timedelta(days=days)).timestamp())
    params = {
        'resolution': resolution,
        'symbol': symbol,
        'start': str(start),
        'end': str(int(time.time())),
    }
    url = 'https://api.india.delta.exchange/v2/history/candles'
    try:
        r = requests.get(url, params=params, headers=headers, timeout=10)
        r.raise_for_status()
        data = r.json().get("result", [])
        if not data:
            # log(f"No data for {symbol}")
            return None

        df = pd.DataFrame(data, columns=["time", "open", "high", "low", "close", "volume"])
        df.rename(columns=str.title, inplace=True)

        first_time = df["Time"].iloc[0]
        if first_time > 1e12:
            df["Time"] = pd.to_datetime(df["Time"], unit="ms", utc=True)
        else:
            df["Time"] = pd.to_datetime(df["Time"], unit="s", utc=True)

        df["Time"] = df["Time"].dt.tz_convert(tz)
        df["time"] = df["Time"]
        df.set_index("Time", inplace=True)
        df.sort_index(inplace=True)
        df = df[~df.index.duplicated(keep='last')]

        for col in ["Open", "High", "Low", "Close", "Volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")

        return df

    except Exception as e:
        # log(f"⚠️ Error fetching {symbol}: {e}")
        return None


# ================= ORDER WRAPPERS ==========================
def place_order(product_id, side, size):
    try:
        resp = client.place_order(
            product_id=product_id,
            order_type=OrderType.MARKET,
            side=side,
            size=size
        )
        if not resp:
            send_short_error("ORDER FAILED", f"{side}\nProduct: {product_id}\nSize: {size}")
            return None
        return resp
    except Exception as e:
        send_short_error("ORDER ERROR", f"{side}\n{product_id}\n{e}")
        return None


# ================= TREND CALC ============================
def build_trend(df, symbol):
    sl = STOP_LOSS[symbol]

    ha = ta.ha(df["Open"], df["High"], df["Low"], df["Close"])
    ha["EMA"] = df["Close"].ewm(span=10).mean()

    adx = ta.adx(ha["HA_high"], ha["HA_low"], ha["HA_close"])
    ha["ADX"] = adx["ADX_14"]
    ha["ADX_avg"] = ha["ADX"].rolling(14).mean()

    highs = argrelextrema(ha["HA_high"].values, np.greater_equal, order=21)[0]
    lows = argrelextrema(ha["HA_low"].values, np.less_equal, order=21)[0]

    ha["max_high"] = np.nan
    ha["max_low"] = np.nan

    if len(highs): ha.loc[highs, "max_high"] = ha.loc[highs, "HA_high"]
    if len(lows):  ha.loc[lows, "max_low"] = ha.loc[lows, "HA_low"]

    ha["max_high"].ffill(inplace=True)
    ha["max_low"].ffill(inplace=True)

    trend = ha.copy()
    trend["Trendline"] = np.nan

    t = trend["HA_close"].iloc[0] - sl
    trend.loc[0, "Trendline"] = t

    for i in range(1, len(trend)):
        if trend.loc[i, "HA_high"] == trend.loc[i, "max_high"]:
            t = trend.loc[i, "HA_close"] - sl
        elif trend.loc[i, "HA_low"] == trend.loc[i, "max_low"]:
            t = trend.loc[i, "HA_close"] + sl
        trend.loc[i, "Trendline"] = t

    return trend


# ================= TRADING LOGIC ==========================
def trade(symbol, price, df, state):
    trend = df["Trendline"].iloc[-1]
    last_close = df["HA_close"].iloc[-1]
    adx = df["ADX"].iloc[-1]
    adx_avg = df["ADX_avg"].iloc[-1]

    entry_move = ENTRY_MOVE[symbol]
    sl = STOP_LOSS[symbol]
    size = DEFAULT_CONTRACTS[symbol]
    pid = PRODUCT_IDS[symbol]

    pos = state["pos"]
    upper = state["upper"]
    lower = state["lower"]

    # ---------- NEW LIMIT LEVELS ----------
    if pos is None:
        if upper is None and last_close > trend:
            state["upper"] = price + entry_move

        if lower is None and last_close < trend:
            state["lower"] = price - entry_move

    # ---------- LONG ENTRY ----------
    if pos is None and upper and price >= upper and price >= trend and adx > adx_avg:
        resp = place_order(pid, "buy", size)
        if resp:
            send_telegram_message(f"🟢 LONG ENTRY {symbol}\nPrice: {price}")
            state.update({"pos": "long", "entry": price, "stop": price - sl})
        state["upper"] = None
        return

    # ---------- SHORT ENTRY ----------
    if pos is None and lower and price <= lower and price <= trend and adx > adx_avg:
        resp = place_order(pid, "sell", size)
        if resp:
            send_telegram_message(f"🔻 SHORT ENTRY {symbol}\nPrice: {price}")
            state.update({"pos": "short", "entry": price, "stop": price + sl})
        state["lower"] = None
        return

    # ========== POSITION MANAGEMENT ==========
    if pos == "long" and last_close < trend:
        pnl = (price - state["entry"]) * CONTRACT_SIZE[symbol] * size
        pnl -= commission(price, size, symbol)
        place_order(pid, "sell", size)
        send_telegram_message(f"📤 LONG EXIT {symbol}\nPnL: {pnl:.3f}")
        state.update({"pos": None, "upper": None, "lower": None})
        return

    if pos == "short" and last_close > trend:
        pnl = (state["entry"] - price) * CONTRACT_SIZE[symbol] * size
        pnl -= commission(price, size, symbol)
        place_order(pid, "buy", size)
        send_telegram_message(f"📤 SHORT EXIT {symbol}\nPnL: {pnl:.3f}")
        state.update({"pos": None, "upper": None, "lower": None})
        return


# ================= MAIN LOOP =============================
def run_live():
    state = {s: {"pos": None, "entry": None, "upper": None, "lower": None} for s in SYMBOLS}
    send_telegram_message("🚀 BOT STARTED")

    while True:
        try:
            for symbol in SYMBOLS:

                df = fetch_candles(symbol)
                if df is None or len(df) < 40:
                    continue

                trend_df = build_trend(df, symbol)

                price = fetch_ticker_price(symbol)
                if price is None:
                    continue

                trade(symbol, price, trend_df, state[symbol])

            time.sleep(10)

        except Exception as e:
            send_short_error("MAIN LOOP ERROR", str(e))
            time.sleep(5)


# ================= EXECUTE ================================
if __name__ == "__main__":
    run_live()
