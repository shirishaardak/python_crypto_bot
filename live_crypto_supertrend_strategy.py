import os
import time as t
import requests
import pandas as pd
import pandas_ta as ta
from datetime import datetime, timedelta
from delta_rest_client import DeltaRestClient, OrderType
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# ---------------------------------------
# SETTINGS
# ---------------------------------------
symbols = ["BTCUSD", "ETHUSD"]   # trading pairs
ORDER_QTY = 10

TRAIL_AMOUNTS = {
    "BTCUSD": 400,
    "ETHUSD": 40
}

# ---------------------------------------
# API CONFIGURATION
# ---------------------------------------
api_key = os.getenv('DELTA_API_KEY')
api_secret = os.getenv('DELTA_API_SECRET')
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

client = DeltaRestClient(
    base_url='https://api.india.delta.exchange',
    api_key=api_key,
    api_secret=api_secret
)

# ---------------------------------------
# Telegram & Logging Utilities
# ---------------------------------------
def send_telegram_message(msg: str) -> bool:
    """Send alert message via Telegram. Returns True if sent successfully."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ Telegram not configured. Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID.")
        return False
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": msg}
        r = requests.post(url, json=payload, timeout=8)
        r.raise_for_status()
        return True
    except Exception as e:
        print(f"⚠️ Telegram Error: {e} | message was: {msg}")
        return False

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
log(f"Loaded futures product IDs: {symbols_map}", alert=True)

# ---------------------------------------
# Initialize parameters
# ---------------------------------------
renko_param = {
    symbol: {
        'Date': '',
        'close': '',
        'option': 0,  # 0 = no position, 1 = long, 2 = short
        'single': 0,
        'EMA_21': None,
        'EMA_21_UP': None,
        'EMA_21_DN': None,
        'stop_order_id': None,
        'main_order_id': None
    }
    for symbol in symbols_map
}

# ---------------------------------------
# Candle Fetch
# ---------------------------------------
def fetch_and_save_delta_candles(symbol, resolution='1h', days=7, save_dir='.', tz='Asia/Kolkata'):
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
            log(f"No candle data for {symbol}")
            return None
    except Exception as e:
        log(f"Error fetching candles for {symbol}: {e}", alert=True)
        return None

# ---------------------------------------
# Process symbol
# ---------------------------------------
def process_symbol(symbol, renko_param, ha_save_dir="./data/crypto"):
    df = fetch_and_save_delta_candles(symbol, resolution='15m', days=7, save_dir=ha_save_dir)
    if df is None or df.empty:
        return renko_param

    df = df.sort_index()

    # Heikin-Ashi
    df = ta.ha(open_=df['open'], high=df['high'], close=df['close'], low=df['low'])

    # EMA(21)
    df['EMA_21'] = ta.ema(df['HA_close'], length=10)
    offset = 250 if symbol == "BTCUSD" else 20
    df['EMA_21_UP'] = df['EMA_21'] + offset
    df['EMA_21_DN'] = df['EMA_21'] - offset

    df['single'] = 0
    df.loc[(df['HA_close'] > df['EMA_21_UP']) & (df['HA_close'] > df['HA_open'].shift(1)), 'single'] = 1
    df.loc[(df['HA_close'] < df['EMA_21_DN']) & (df['HA_close'] < df['HA_open'].shift(1)), 'single'] = -1

    os.makedirs(ha_save_dir, exist_ok=True)
    df.to_csv(f"{ha_save_dir}/supertrend_live_{symbol}.csv")

    last_row = df.iloc[-1]

    renko_param[symbol].update({
        'Date': last_row.name,
        'close': last_row['HA_close'],
        'single': last_row['single'],
        'EMA_21_UP': last_row['EMA_21_UP'],
        'EMA_21_DN': last_row['EMA_21_DN'],
        'EMA_21': last_row['EMA_21'],
    })

    return renko_param

# ---------------------------------------
# Helper functions for order management
# ---------------------------------------
def place_order_with_error_handling(client, **kwargs):
    try:
        response = client.place_order(**kwargs)
        return response
    except Exception as e:
        log(f"❌ Error placing order: {e}", alert=True)
        return None

def place_stop_order_with_error_handling(client, **kwargs):
    try:
        response = client.place_stop_order(**kwargs)
        return response
    except Exception as e:
        log(f"❌ Error placing stop order: {e}", alert=True)
        return None

def cancel_order_with_error_handling(client, product_id, order_id):
    try:
        response = client.cancel_order(product_id, order_id)
        return response
    except Exception as e:
        log(f"❌ Error cancelling order {order_id}: {e}", alert=True)
        return None
    
def get_live_orders_with_error_handling(client):
    try:
        return client.get_live_orders()
    except Exception as e:
        log(f"❌ Error getting live orders: {e}", alert=True)
        return []

# ---------------------------------------
# Main Loop
# ---------------------------------------
log("🚀 Starting live strategy for BTCUSD + ETHUSD...", alert=True)

while True:
    try:
        now = datetime.now()

        if now.second == 10 and datetime.now().minute % 15 == 0:  # every 15m candle
            log(f"Running strategy cycle at {now.strftime('%H:%M:%S')}", alert=True)

            for symbol in symbols_map:
                log(f"Processing {symbol}...")
                renko_param = process_symbol(symbol, renko_param)

            # Core trading logic
            for symbol, product_id in symbols_map.items():
                price = renko_param[symbol]['close']
                EMA_21 = renko_param[symbol]['EMA_21']
                EMA_21_UP = renko_param[symbol]['EMA_21_UP']
                EMA_21_DN = renko_param[symbol]['EMA_21_DN']
                single = renko_param[symbol]['single']
                option = renko_param[symbol]['option']

                # --- BUY SIGNAL ---
                if single == 1 and option == 0:
                    log(f"🚀 BUY signal for {symbol} at {price}", alert=True)
                    buy_order = place_order_with_error_handling(
                        client, product_id=product_id, order_type=OrderType.MARKET, side='buy', size=ORDER_QTY
                    )
                    if buy_order and buy_order.get('state') == 'closed':
                        renko_param[symbol]['option'] = 1
                        renko_param[symbol]['main_order_id'] = buy_order.get('id')
                        log(f"✅ BUY order filled for {symbol}, ID={buy_order.get('id')}", alert=True)
                        trailing_stop = place_stop_order_with_error_handling(
                            client, product_id=product_id, size=ORDER_QTY, side='sell',
                            order_type=OrderType.MARKET, trail_amount=price - EMA_21_DN,
                            isTrailingStopLoss=True
                        )
                        if trailing_stop:
                            renko_param[symbol]['stop_order_id'] = trailing_stop.get('id')
                            log(f"🛡️ Trailing stop placed for {symbol}, ID={trailing_stop.get('id')}", alert=True)
                        else:
                            log(f"❌ Failed to place trailing stop for {symbol}", alert=True)

                # --- SELL SIGNAL ---
                elif single == -1 and option == 0:
                    log(f"🔻 SELL signal for {symbol} at {price}", alert=True)
                    sell_order = place_order_with_error_handling(
                        client, product_id=product_id, order_type=OrderType.MARKET, side='sell', size=ORDER_QTY
                    )
                    if sell_order and sell_order.get('state') == 'closed':
                        renko_param[symbol]['option'] = 2
                        renko_param[symbol]['main_order_id'] = sell_order.get('id')
                        log(f"✅ SELL order filled for {symbol}, ID={sell_order.get('id')}", alert=True)
                        trailing_stop = place_stop_order_with_error_handling(
                            client, product_id=product_id, size=ORDER_QTY, side='buy',
                            order_type=OrderType.MARKET, trail_amount=EMA_21_UP - price,
                            isTrailingStopLoss=True
                        )
                        if trailing_stop:
                            renko_param[symbol]['stop_order_id'] = trailing_stop.get('id')
                            log(f"🛡️ Trailing stop placed for {symbol}, ID={trailing_stop.get('id')}", alert=True)
                        else:
                            log(f"❌ Failed to place trailing stop for {symbol}", alert=True)

            df_status = pd.DataFrame.from_dict(renko_param, orient='index')
            log(f"Cycle completed. Current status:\n{df_status[['Date', 'close', 'option', 'single', 'stop_order_id']]}")
            t.sleep(55)

    except KeyboardInterrupt:
        log("🛑 Shutting down gracefully...", alert=True)
        for symbol in symbols_map:
            stop_order_id = renko_param[symbol]['stop_order_id']
            if stop_order_id:
                log(f"Cancelling stop order {stop_order_id} for {symbol}", alert=True)
                cancel_order_with_error_handling(client, symbols_map[symbol], stop_order_id)
        break

    except Exception as e:
        log(f"[ERROR] {type(e).__name__}: {e}", alert=True)
        t.sleep(5)
        continue
