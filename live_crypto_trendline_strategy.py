"""
Delta Exchange Trendline Strategy (Telegram + Clean Structure)
---------------------------------------------------------------
- Heikin-Ashi + Trendline logic
- HA close > Trendline → Long
- HA close < Trendline → Short
- Auto flips positions
- Telegram Alerts integrated
- Simplified and readable structure (like first script)
"""

import os
import time
import json
import hmac
import hashlib
import requests
import pandas as pd
import numpy as np
import pandas_ta as ta
from datetime import datetime, timedelta
from scipy.signal import argrelextrema
from dotenv import load_dotenv

# =========================================================
# ---------------- CONFIGURATION ---------------------------
# =========================================================
load_dotenv()

BASE_URL = "https://api.india.delta.exchange"
SYMBOLS = ["ETHUSD"]
ORDER_QTY = {"ETHUSD": 50}
TREND_INTERVAL = "15m"
TREND_DAYS = 3
DRY_RUN = False  # ⚠️ Set False for live trading

# API Keys
API_KEY = os.getenv('DELTA_API_KEY')
API_SECRET = os.getenv('DELTA_API_SECRET')

# Telegram Config
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

# =========================================================
# ---------------- TELEGRAM UTILS --------------------------
# =========================================================
def send_telegram_message(msg: str):
    """Send alert message via Telegram."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ Telegram not configured.")
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": msg}
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        print(f"⚠️ Telegram Error: {e}")

def log(msg, alert=False):
    """Print and optionally send Telegram alert."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {msg}")
    if alert:
        send_telegram_message(f"📣 {msg}")

# =========================================================
# ---------------- DELTA API HELPERS -----------------------
# =========================================================
def generate_signature(secret, message):
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()

def api_request(method, path, payload=None, auth=True):
    """Generic API request for Delta Exchange."""
    url = BASE_URL + path
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    body = json.dumps(payload) if payload else ""

    if auth:
        timestamp = str(int(time.time()))
        message = method.upper() + timestamp + path + body
        signature = generate_signature(API_SECRET, message)
        headers.update({
            "api-key": API_KEY,
            "timestamp": timestamp,
            "signature": signature
        })

    r = requests.request(method, url, headers=headers, data=body, timeout=10)
    if r.status_code != 200:
        print("❌ API Error:", r.status_code, r.text)
        r.raise_for_status()
    return r.json()

def get_product_map():
    """Return {symbol: product_id} for futures."""
    try:
        r = requests.get(BASE_URL + "/v2/products", timeout=10)
        r.raise_for_status()
        products = r.json().get("result", [])
        return {p["symbol"]: p["id"] for p in products if p.get("contract_type") == "perpetual_futures"}
    except Exception as e:
        log(f"⚠️ Product fetch failed: {e}")
        return {}

PRODUCT_MAP = get_product_map()

# =========================================================
# ---------------- ORDER MANAGEMENT ------------------------
# =========================================================
def place_market_order(symbol, side, size, reduce_only=False):
    """Place market order or simulate if DRY_RUN."""
    product_id = PRODUCT_MAP.get(symbol)
    if not product_id:
        log(f"❌ Unknown symbol: {symbol}")
        return None

    payload = {
        "product_id": product_id,
        "order_type": "market_order",
        "side": side,
        "size": size,
        "reduce_only": reduce_only
    }

    msg = f"{side.upper()} {symbol} x{size} (reduce_only={reduce_only}, DRY_RUN={DRY_RUN})"
    log(msg, alert=True)

    if DRY_RUN:
        return {"mock": True, "side": side, "symbol": symbol}

    try:
        res = api_request("POST", "/v2/orders", payload=payload, auth=True)
        # log(f"✅ Order placed: {res}", alert=True)
        return res
    except Exception as e:
        log(f"❌ Order failed for {symbol}: {e}", alert=True)
        return None

# =========================================================
# ---------------- DATA & TREND ----------------------------
# =========================================================
def fetch_candles(symbol, resolution="15m", days=3, tz='Asia/Kolkata'):
    """Fetch OHLC candles from Delta."""
    start = int((datetime.now() - timedelta(days=days)).timestamp())
    url = BASE_URL + "/v2/history/candles"
    params = {
        "symbol": symbol,
        "resolution": resolution,
        "start": str(start),
        "end": str(int(time.time()))
    }

    try:
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json().get("result", [])
        if not data:
            log(f"No candle data for {symbol}")
            return None

        df = pd.DataFrame(data, columns=["time", "open", "high", "low", "close", "volume"])
        t = df["time"].iloc[0]
        df["time"] = pd.to_datetime(df["time"], unit="ms" if t > 1e12 else "s", utc=True).dt.tz_convert(tz)
        df.set_index("time", inplace=True)
        df = df.astype(float)
        df = df.sort_index()
        return df

    except Exception as e:
        log(f"⚠️ Candle fetch failed for {symbol}: {e}")
        return None

def compute_trend(df):
    """Compute Heikin-Ashi and Trendline."""
    if df is None or df.empty:
        return df

    ha = ta.ha(df['open'], df['high'], df['low'], df['close'])
    df = pd.concat([df, ha], axis=1)

    df["Trendline"] = np.nan
    order = 42
    try:
        max_idx = argrelextrema(df["HA_high"].values, np.greater_equal, order=order)[0]
        min_idx = argrelextrema(df["HA_low"].values, np.less_equal, order=order)[0]
        if len(max_idx):
            df.loc[df.index[max_idx], "Trendline"] = df["HA_low"].iloc[max_idx].values
        if len(min_idx):
            df.loc[df.index[min_idx], "Trendline"] = df["HA_high"].iloc[min_idx].values
    except Exception:
        pass

    df["Trendline"] = df["Trendline"].ffill()
    return df

# =========================================================
# ---------------- STRATEGY LOGIC --------------------------
# =========================================================
def process_symbol(symbol, positions):
    """Process one symbol for trendline-based trade signals."""
    df = fetch_candles(symbol, TREND_INTERVAL, TREND_DAYS)
    df = compute_trend(df)
    if df is None or df.empty:
        return positions

    last = df.iloc[-2]
    prev = df.iloc[-3]

    price = last["HA_close"]
    trend = last["Trendline"]
    log(f"{symbol}: | trend @ {trend:.2f} | price @ {price:.2f}")
    if pd.isna(trend):
        return positions

    pos = positions[symbol]
    qty = ORDER_QTY[symbol]

    # Entry logic
    if pos is None:
        if price > trend and last["HA_close"] > prev["HA_close"] and last["HA_close"] > last["HA_open"]:
            place_market_order(symbol, "buy", qty)
            positions[symbol] = "long"
            log(f"{symbol}: 🔵 Enter LONG @ {price:.2f}", alert=True)
        elif price < trend and last["HA_close"] < prev["HA_close"] and last["HA_close"] < last["HA_open"]:
            place_market_order(symbol, "sell", qty)
            positions[symbol] = "short"
            log(f"{symbol}: 🔴 Enter SHORT @ {price:.2f}", alert=True)

    # Flip logic
    elif pos == "long" and price < trend:
        place_market_order(symbol, "sell", qty, reduce_only=True)
        place_market_order(symbol, "sell", qty)
        positions[symbol] = "short"
        log(f"{symbol}: 🔁 Flip to SHORT @ {price:.2f}", alert=True)

    elif pos == "short" and price > trend:
        place_market_order(symbol, "buy", qty, reduce_only=True)
        place_market_order(symbol, "buy", qty)
        positions[symbol] = "long"
        log(f"{symbol}: 🔁 Flip to LONG @ {price:.2f}", alert=True)

    return positions

# =========================================================
# ---------------- MAIN LOOP -------------------------------
# =========================================================
def run_strategy():
    """Main loop running every 15 minutes."""
    positions = {s: None for s in SYMBOLS}
    log("🚀 Starting Delta Trendline Strategy (Telegram Enabled)", alert=True)

    while True:
        now = datetime.now()
        if now.minute % 5 == 0 and now.second in range(5, 20):
            log(f"\n[{now.strftime('%Y-%m-%d %H:%M:%S')}] Running cycle...")
            for symbol in SYMBOLS:
                try:
                    positions = process_symbol(symbol, positions)
                except Exception as e:
                    log(f"⚠️ Error in {symbol}: {e}", alert=True)
            log("✅ Cycle completed. Waiting for next 15-min interval.\n")
            time.sleep(60)
        else:
            time.sleep(5)

# =========================================================
# ---------------- ENTRY POINT -----------------------------
# =========================================================
if __name__ == "__main__":
    if not DRY_RUN and (not API_KEY or not API_SECRET):
        raise RuntimeError("❌ Missing API keys for live trading.")
    run_strategy()
