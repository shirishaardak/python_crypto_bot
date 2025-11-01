import os
import time as t
import requests
import pandas as pd
import numpy as np
import pandas_ta as ta
from datetime import datetime, timedelta
from delta_rest_client import DeltaRestClient, OrderType
from scipy.signal import argrelextrema
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# ---------------------------------------
# SETTINGS
# ---------------------------------------
symbols = ["BTCUSD", "ETHUSD"]   # trading pairs you want
ORDER_QTY = 30

# Use environment variables for security
api_key = os.getenv('DELTA_API_KEY')
api_secret = os.getenv('DELTA_API_SECRET')

client = DeltaRestClient(
    base_url='https://api.india.delta.exchange',
    api_key=api_key,
    api_secret=api_secret
)

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
    full_msg = f"[{timestamp}] {msg}"
    print(full_msg)
    if alert:
        send_telegram_message(f"📣 {full_msg}")

# ---------------------------------------
# Get product IDs (futures only)
# ---------------------------------------
def get_futures_product_ids(symbols):
    url = "https://api.india.delta.exchange/v2/products"
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()

        product_map = {}
        if "result" in data:
            for p in data["result"]:
                if p["symbol"] in symbols and p.get("contract_type") == "perpetual_futures":
                    product_map[p["symbol"]] = p["id"]
        return product_map
    except Exception as e:
        log(f"Error fetching product IDs: {e}", alert=True)
        return {}

symbols_map = get_futures_product_ids(symbols)
if not symbols_map:
    raise ValueError("Could not fetch futures product IDs from Delta API")
print("Loaded futures product IDs:", symbols_map)

# Status dictionary - FIXED: Added stop_order_id tracking
renko_param = {
    symbol: {
        'Date': '',
        'close': '',
        'option': 0,  # 0 = no position, 1 = long, 2 = short
        'single': 0,
        'Trendline': None,
        'up_band': None,
        'down_band': None,
        'stop_order_id': None,
        'main_order_id': None
    }
    for symbol in symbols_map
}

# ---------------------------------------
# Candle Fetch
# ---------------------------------------
def fetch_and_save_delta_candles(symbol, resolution='15m', days=7, save_dir='.', tz='Asia/Kolkata'):
    headers = {'Accept': 'application/json'}
    start = int((datetime.now() - timedelta(days=days)).timestamp())
    params = {
        'resolution': resolution,
        'symbol': symbol,
        'start': str(start),
        'end': str(int(t.time())),
    }
    url = 'https://api.india.delta.exchange/v2/history/candles'
    
    try:
        r = requests.get(url, params=params, headers=headers, timeout=10)
        r.raise_for_status()
        data = r.json()

        if 'result' in data and data['result']:
            df = pd.DataFrame(data['result'], columns=['time', 'open', 'high', 'low', 'close', 'volume'])
            df['time'] = pd.to_datetime(df['time'], unit='s', utc=True).dt.tz_convert(tz)
            df.set_index("time", inplace=True)
            df['symbol'] = symbol
            return df
        else:
            log(f"No data for {symbol}", alert=True)
            return None
    except Exception as e:
        log(f"Error fetching candles for {symbol}: {e}", alert=True)
        return None

# ---------------------------------------
# Process symbol
# ---------------------------------------
def process_symbol(symbol, renko_param, ha_save_dir="./data/live_crypto_supertrend_strategy"):
    df = fetch_and_save_delta_candles(symbol, resolution='15m', days=7, save_dir=ha_save_dir)
    if df is None or df.empty:
        return renko_param

    df = df.sort_index()
    order = 63  # local extrema order

    # Heikin-Ashi
    df = ta.ha(open_=df['open'], high=df['high'], close=df['close'], low=df['low'])

    max_idx = argrelextrema(df["HA_high"].values, np.greater_equal, order=order)[0]
    min_idx = argrelextrema(df["HA_low"].values, np.less_equal, order=order)[0]

    if len(max_idx):
        df.loc[df.index[max_idx], "Trendline"] = df["HA_low"].iloc[max_idx].values
    if len(min_idx):
        df.loc[df.index[min_idx], "Trendline"] = df["HA_high"].iloc[min_idx].values

    df["Trendline"] = df["Trendline"].ffill()

    # --- Symbol-specific band widths ---
    if symbol == "BTCUSD":
        up_buffer = 150
        down_buffer = 150
    elif symbol == "ETHUSD":
        up_buffer = 15
        down_buffer = 15
    else:
        up_buffer = 100
        down_buffer = 100

    # --- Compute upper/lower bands ---
    df["up_band"] = df["Trendline"] + up_buffer
    df["down_band"] = df["Trendline"] - down_buffer

    # --- Generate signals ---
    df['single'] = 0
    df.loc[
        (df['HA_close'] > df['Trendline']) & 
        (df['HA_close'] > df['HA_close'].shift(1)) & 
        (df['HA_close'] > df['HA_open']),
        'single'
    ] = 1

    df.loc[
        (df['HA_close'] < df['Trendline']) & 
        (df['HA_close'] < df['HA_close'].shift(1)) & 
        (df['HA_close'] < df['HA_open']),
        'single'
    ] = -1

    if len(df) < 2:
        log(f"Not enough data for {symbol}", alert=True)
        return renko_param

    last_row = df.iloc[-2]

    renko_param[symbol].update({
        'Date': last_row.name,
        'close': last_row['HA_close'],
        'single': last_row['single'],
        'Trendline': last_row['Trendline'],
        'up_band': last_row['up_band'],
        'down_band': last_row['down_band'],
    })

    return renko_param

# ---------------------------------------
# Helper functions
# ---------------------------------------
def safe_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

def place_order_with_error_handling(client, **kwargs):
    try:
        return client.place_order(**kwargs)
    except Exception as e:
        log(f"Error placing order: {e}", alert=True)
        return None

def place_stop_order_with_error_handling(client, **kwargs):
    try:
        return client.place_stop_order(**kwargs)
    except Exception as e:
        log(f"Error placing stop order: {e}", alert=True)
        return None

def cancel_order_with_error_handling(client, order_id, product_id):
    try:
        return client.cancel_order(order_id, product_id)
    except Exception as e:
        log(f"Error cancelling order {order_id}: {e}", alert=True)
        return None

def edit_stop_order_with_error_handling(client, order_id, product_id, new_stop_price):
    try:
        payload = {
            "id": order_id,
            "product_id": product_id,
            "stop_price": str(new_stop_price)
        }
        return client.request("PUT", "/v2/orders/", payload, auth=True)
    except Exception as e:
        log(f"Error editing order {order_id}: {e}", alert=True)
        return None

def get_history_orders_with_error_handling(client, product_id):
    try:
        query = {"product_id": product_id}
        response = client.order_history(query, page_size=50)  # Increased page size
        return response.get('result', []) if response else []
    except Exception as e:
        log(f"Error getting order history: {e}", alert=True)
        return []
    
def get_live_orders_with_error_handling(client):
    try:
        response = client.get_live_orders()
        return response if response else []
    except Exception as e:
        log(f"Error getting live orders: {e}", alert=True)
        return []

# ---------------------------------------
# Main Loop
# ---------------------------------------
log("🚀 Starting live strategy for BTCUSD + ETHUSD...", alert=True)


while True:
    try:
        now = datetime.now()

        if now.second == 10 and datetime.now().minute % 5 == 0:
            log(f"\n[{now.strftime('%Y-%m-%d %H:%M:%S')}] Running cycle...")

            for symbol in symbols_map:
                renko_param = process_symbol(symbol, renko_param)

            for symbol, product_id in symbols_map.items():
                price = renko_param[symbol]['close']
                trendline = renko_param[symbol]['Trendline']
                up_band = renko_param[symbol]['up_band']
                down_band = renko_param[symbol]['down_band']
                single = renko_param[symbol]['single']
                option = renko_param[symbol]['option']

                # --- BUY SIGNAL ---
                if single == 1 and option == 0:
                    log(f"🟢 BUY signal for {symbol} at {price}", alert=True)                    
                    buy_order_place = place_order_with_error_handling(
                        client,
                        product_id=product_id,
                        order_type=OrderType.MARKET,
                        side='buy',
                        size=ORDER_QTY
                    )

                    if buy_order_place and buy_order_place.get('state') == 'closed':
                        renko_param[symbol]['option'] = 1
                        renko_param[symbol]['main_order_id'] = buy_order_place.get('id')
                        log(f"✅ BUY order executed for {symbol} @ {price}", alert=True)
                # --- BUY POSITION MANAGEMENT ---
                elif option == 1 and price < down_band:
                    buy_exit_order_place = place_order_with_error_handling(
                        client,
                        product_id=product_id,
                        order_type=OrderType.MARKET,
                        side='sell',
                        size=ORDER_QTY
                    )
                    if buy_exit_order_place:
                        renko_param[symbol].update({'option': 0, 'stop_order_id': None, 'main_order_id': None})
                        log(f"🛑 Stop loss triggered for BUY position on {symbol}", alert=True)
                        stop_order_id = renko_param[symbol]['stop_order_id']
                    
                   

                # --- SELL SIGNAL ---
                if single == -1 and option == 0:
                    log(f"🔴 SELL signal for {symbol} at {price}", alert=True)                    
                    sell_order_place = place_order_with_error_handling(
                        client,
                        product_id=product_id,
                        order_type=OrderType.MARKET,
                        side='sell',
                        size=ORDER_QTY
                    )
                    
                    if sell_order_place and sell_order_place.get('state') == 'closed':
                        renko_param[symbol]['option'] = 2
                        log(f"✅ SELL order executed for {symbol} @ {price}", alert=True)                        
                        

                # --- SELL POSITION MANAGEMENT ---
                elif option == 2 and price > up_band:
                    sell_exit_order_place = place_order_with_error_handling(
                        client,
                        product_id=product_id,
                        order_type=OrderType.MARKET,
                        side='buy',
                        size=ORDER_QTY
                    )
                    if sell_exit_order_place : 
                        log(f"🛑 Stop loss triggered for SELL position on {symbol}", alert=True)
                        renko_param[symbol].update({'option': 0, 'stop_order_id': None, 'main_order_id': None})
                    

            df_status = pd.DataFrame.from_dict(renko_param, orient='index')
            print("\nCurrent Strategy Status:")
            print(df_status[['Date', 'close', 'option', 'single', 'stop_order_id']])

            log("Waiting for next cycle...\n")
            t.sleep(55)

    except KeyboardInterrupt:
        log("🧹 Manual shutdown initiated. Cancelling open stop orders...", alert=True)
        for symbol in symbols_map:
            stop_order_id = renko_param[symbol]['stop_order_id']
            if stop_order_id:
                log(f"Cancelling stop order {stop_order_id} for {symbol}")
                cancel_order_with_error_handling(client, stop_order_id, symbols_map[symbol])
        break
        
    except Exception as e:
        log(f"[ERROR] {type(e).__name__}: {e}", alert=True)
        t.sleep(5)
        continue
