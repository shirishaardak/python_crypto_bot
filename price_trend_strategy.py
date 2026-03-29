import os
import time
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import pandas_ta as ta
from dotenv import load_dotenv
from delta_rest_client import DeltaRestClient, OrderType

load_dotenv()

# ================= TELEGRAM =================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

_last_tg = {}

def send_telegram(msg, key=None, cooldown=30):
    try:
        now = time.time()
        if key and key in _last_tg and now - _last_tg[key] < cooldown:
            return
        if key:
            _last_tg[key] = now

        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg}, timeout=5)
    except:
        pass

def log(msg, tg=False, key=None):
    text = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(text)
    if tg:
        send_telegram(text, key)

# ================= DELTA API =================
DELTA_API_KEY = os.getenv("DELTA_API_KEY")
DELTA_API_SECRET = os.getenv("DELTA_API_SECRET")

client = DeltaRestClient(
    base_url="https://api.india.delta.exchange",
    api_key=DELTA_API_KEY,
    api_secret=DELTA_API_SECRET
)

# ================= SETTINGS =================
SYMBOLS = ["BTCUSD"]

# ⚠️ START SMALL
DEFAULT_CONTRACTS = {"BTCUSD": 10}

CONTRACT_SIZE = {"BTCUSD": 0.001}
TAKER_FEE = 0.0005

TIMEFRAME = "1h"
DAYS = 15

TAKE_PROFIT = {"BTCUSD": 300}
STOP_LOSS   = {"BTCUSD": 200}

BASE_DIR = os.getcwd()
SAVE_DIR = os.path.join(BASE_DIR, "data", "price_trend_strategy")
os.makedirs(SAVE_DIR, exist_ok=True)

TRADE_CSV = os.path.join(SAVE_DIR, "live_trades.csv")

# ================= PRODUCT IDS =================
def get_product_ids(symbols):
    try:
        r = requests.get("https://api.india.delta.exchange/v2/products", timeout=10)
        data = r.json()["result"]

        product_map = {}
        for p in data:
            if p["symbol"] in symbols and p["contract_type"] == "perpetual_futures":
                product_map[p["symbol"]] = p["id"]

        return product_map
    except Exception as e:
        log(f"Product ID fetch error: {e}", tg=True)
        return {}

PRODUCT_IDS = get_product_ids(SYMBOLS)

if not PRODUCT_IDS:
    raise Exception("❌ Product IDs not loaded")

# ================= ORDER =================
def place_market_order(symbol, side, qty):
    try:
        order = client.place_order(
            product_id=PRODUCT_IDS[symbol],
            order_type=OrderType.MARKET,
            side=side,
            size=qty
        )

        # 🔥 STRICT CHECK
        if not order or ("success" in order and not order["success"]):
            log(f"❌ {symbol} ORDER FAILED: {order}", tg=True, key=f"{symbol}_order_fail")
            return None

        log(f"✅ {symbol} ORDER SUCCESS: {order}", tg=True, key=f"{symbol}_order_success")
        return order

    except Exception as e:
        log(f"❌ {symbol} Order Exception: {e}", tg=True, key=f"{symbol}_order_error")
        return None

# ================= UTILITIES =================
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
        log(f"{symbol} PRICE error: {e}", tg=True)
        return None

def fetch_candles(symbol):
    start = int((datetime.now() - timedelta(days=DAYS)).timestamp())

    params = {
        "resolution": TIMEFRAME,
        "symbol": symbol,
        "start": str(start),
        "end": str(int(time.time()))
    }

    try:
        r = requests.get("https://api.india.delta.exchange/v2/history/candles", params=params, timeout=10)
        df = pd.DataFrame(r.json()["result"], columns=["time","open","high","low","close","volume"])

        df.rename(columns=str.title, inplace=True)
        df["Time"] = pd.to_datetime(df["Time"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata")
        df.set_index("Time", inplace=True)
        df = df.astype(float)
        return df.dropna()
    except Exception as e:
        log(f"{symbol} fetch error: {e}")
        return None

# ================= TRENDLINE =================
def calculate_trendline(df):
    ha = ta.ha(df["Open"], df["High"], df["Low"], df["Close"]).reset_index(drop=True)

    order = 21
    ha["UPPER"] = ha["HA_high"].rolling(order).max()
    ha["LOWER"] = ha["HA_low"].rolling(order).min()

    ha["Trendline"] = np.nan
    trend = ha.loc[0, "HA_close"]
    ha.loc[0, "Trendline"] = trend

    for i in range(1, len(ha)):
        if ha.loc[i, "HA_high"] == ha.loc[i, "UPPER"]:
            trend = ha.loc[i, "HA_low"]
        elif ha.loc[i, "HA_low"] == ha.loc[i, "LOWER"]:
            trend = ha.loc[i, "HA_high"]
        ha.loc[i, "Trendline"] = trend

    return ha

# ================= STRATEGY =================
def process_symbol(symbol, df, price, state):
    ha = calculate_trendline(df)

    last = ha.iloc[-2]
    prev = ha.iloc[-3]
    pos  = state["position"]
    now = datetime.now()

    # ===== ENTRY =====
    if pos is None:

        if last.HA_close > last.Trendline and last.HA_close > prev.HA_close and last.HA_close > prev.HA_open:
            order = place_market_order(symbol, "buy", DEFAULT_CONTRACTS[symbol])
            if order:
                state["position"] = {
                    "side": "long",
                    "entry": price,
                    "qty": DEFAULT_CONTRACTS[symbol],
                    "entry_time": now
                }
                log(f"🟢 {symbol} LONG ORDER @ {price}", tg=True)
            return

        if last.HA_close < last.Trendline and last.HA_close < prev.HA_close and last.HA_close < prev.HA_open:
            order = place_market_order(symbol, "sell", DEFAULT_CONTRACTS[symbol])
            if order:
                state["position"] = {
                    "side": "short",
                    "entry": price,
                    "qty": DEFAULT_CONTRACTS[symbol],
                    "entry_time": now
                }
                log(f"🔴 {symbol} SHORT ORDER @ {price}", tg=True)
            return

    # ===== EXIT =====
    if pos:
        exit_trade = False
        pnl = 0

        if pos["side"] == "long" and price < last.Trendline:
            order = place_market_order(symbol, "sell", pos["qty"])
            if order:
                pnl = (price - pos["entry"]) * CONTRACT_SIZE[symbol] * pos["qty"]
                exit_trade = True

        if pos["side"] == "short" and price > last.Trendline:
            order = place_market_order(symbol, "buy", pos["qty"])
            if order:
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
            log(f"{emoji} {symbol} EXIT @ {price} | PNL: {round(net,6)}", tg=True)

            state["position"] = None

# ================= MAIN =================
def run():
    state = {s: {"position": None, "last_candle_time": None} for s in SYMBOLS}

    log("🚀 LIVE BOT STARTED", tg=True)

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
            log(f"🚨 Runtime error: {e}", tg=True)
            time.sleep(5)

if __name__ == "__main__":
    run()