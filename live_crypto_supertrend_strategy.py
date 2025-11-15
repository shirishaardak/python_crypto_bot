import os
import time as t
import requests
import pandas as pd
import pandas_ta as ta
from datetime import datetime, timedelta
from delta_rest_client import DeltaRestClient, OrderType
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# ---------------------------------------
# SETTINGS
# ---------------------------------------
symbols = ["BTCUSD", "ETHUSD"]
ORDER_QTY = 30
QTY = {
    "BTCUSD": 0.03,  # 0.01 BTC
    "ETHUSD": 0.3   # 0.1 ETH
}

# Telegram
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

# Delta Exchange API keys
api_key = os.getenv('DELTA_API_KEY')
api_secret = os.getenv('DELTA_API_SECRET')

client = DeltaRestClient(
    base_url='https://api.india.delta.exchange',
    api_key=api_key,
    api_secret=api_secret
)

# ---------------------------------------
# Telegram Utility
# ---------------------------------------
def send_telegram_message(msg: str) -> bool:
    """Send alert message via Telegram. Returns True if sent successfully."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        # Make sure to surface this as an alert as well
        print("⚠️ Telegram not configured. Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID.")
        return False
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": msg}
        r = requests.post(url, json=payload, timeout=8)
        r.raise_for_status()
        return True
    except Exception as e:
        # fallback: print to console
        print(f"⚠️ Telegram Error: {e} | message was: {msg}")
        return False

def log(msg, alert=False):
    """Print and optionally send Telegram alert. Telegram will receive timestamped message."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    full_msg = f"[{timestamp}] {msg}"
    print(full_msg)
    if alert:
        send_telegram_message(f"📣 {full_msg}")

# ---------------------------------------
# Get product IDs
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
        if not product_map:
            log("Could not fetch futures product IDs from Delta API", alert=True)
        return product_map
    except Exception as e:
        log(f"Error fetching product IDs: {e}", alert=True)
        return {}

symbols_map = get_futures_product_ids(symbols)
if not symbols_map:
    raise ValueError("Could not fetch futures product IDs from Delta API")
print("Loaded futures product IDs:", symbols_map)

# ---------------------------------------
# Renko parameters (cleaned up - removed stop loss fields)
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
        'main_order_id': None,
        'entry_price': None,
        'exit_price': None,
        'pnl': 0.0
    }
    for symbol in symbols_map
}

# ---------------------------------------
# Fetch candles
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
        r = requests.get(url, params=params, headers=headers, timeout=12)
        r.raise_for_status()
        data = r.json()

        if 'result' in data and data['result']:
            df = pd.DataFrame(data['result'], columns=['time', 'open', 'high', 'low', 'close', 'volume'])
            df['time'] = pd.to_datetime(df['time'], unit='s', utc=True).dt.tz_convert(tz)
            df.set_index("time", inplace=True)
            df['symbol'] = symbol
            return df
        else:
            log(f"No candle data returned for {symbol}", alert=True)
            return None
    except Exception as e:
        log(f"Error fetching candles for {symbol}: {e}", alert=True)
        return None

# ---------------------------------------
# Process symbol
# ---------------------------------------
def process_symbol(symbol, renko_param, ha_save_dir="./data/live_crypto_supertrend_strategy"):
    df = fetch_and_save_delta_candles(symbol, resolution='1h', days=7, save_dir=ha_save_dir)
    df_15m = fetch_and_save_delta_candles(symbol, resolution='15m', days=7, save_dir=ha_save_dir)
    if df is None or df.empty:
        return renko_param

    df = df.sort_index()

    # Heikin Ashi
    df = ta.ha(open_=df['open'], high=df['high'], close=df['close'], low=df['low'])
    df['EMA_21'] = ta.ema(df['HA_close'], length=5)
    # Removed ADX calculation as it's not needed

    offset = 300 if symbol == "BTCUSD" else 20
    df['EMA_21_UP'] = df['EMA_21'] + offset
    df['EMA_21_DN'] = df['EMA_21'] - offset

    df['single'] = 0
    df.loc[(df['HA_close'] > df['EMA_21_UP']) & (df['HA_close'] > df['HA_close'].shift(1)) & (df['HA_close'] > df['HA_open']), 'single'] = 1
    df.loc[(df['HA_close'] < df['EMA_21_DN']) & (df['HA_close'] < df['HA_close'].shift(1)) & (df['HA_close'] < df['HA_open']), 'single'] = -1

    os.makedirs(ha_save_dir, exist_ok=True)
    try:
        df.to_csv(f"{ha_save_dir}/supertrend_live_{symbol}.csv")
    except Exception as e:
        log(f"Error saving CSV for {symbol}: {e}", alert=True)

    if len(df) < 2:
        log(f"Not enough data for {symbol}", alert=True)
        return renko_param

    last_row = df.iloc[-1]
    last_row_15m = df_15m.iloc[-1]
    # Removed unused prv_row variable
    renko_param[symbol].update({
        'Date': last_row.name,
        'close': last_row['HA_close'],
        'close_15m': last_row_15m['HA_close'],
        'single': last_row['single'],
        'EMA_21_UP': last_row['EMA_21_UP'],
        'EMA_21_DN': last_row['EMA_21_DN'],
        'EMA_21': last_row['EMA_21'],
    })

    return renko_param

# ---------------------------------------
# Helper functions (cleaned up - removed stop loss functions)
# ---------------------------------------
def place_order_with_error_handling(client, **kwargs):
    try:
        resp = client.place_order(**kwargs)
        # If response is falsy: alert
        if not resp:
            log(f"⚠️ place_order returned empty response for payload: {kwargs}", alert=True)
            return None
        return resp
    except Exception as e:
        log(f"⚠️ Error placing order: {e} | payload: {kwargs}", alert=True)
        return None

# ---------------------------------------
# MAIN LOOP
# ---------------------------------------
print("Starting live strategy for BTCUSD + ETHUSD...")

while True:
    try:
        now = datetime.now()

        if now.second == 10 and now.minute % 15 == 0:
            print(f"\n[{now.strftime('%Y-%m-%d %H:%M:%S')}] Running cycle...")

            # Process symbols
            for symbol in symbols_map:
                renko_param = process_symbol(symbol, renko_param)

            # Trading logic
            for symbol, product_id in symbols_map.items():
                price = renko_param[symbol]['close']
                close_15m = renko_param[symbol]['close_15m']
                EMA_21 = renko_param[symbol]['EMA_21']
                EMA_21_UP = renko_param[symbol]['EMA_21_UP']
                EMA_21_DN = renko_param[symbol]['EMA_21_DN']
                single = renko_param[symbol]['single']
                option = renko_param[symbol]['option']

                # --- BUY ENTRY ---
                if close_15m > EMA_21_UP and option == 0:
                    log(f"🟢 BUY signal for {symbol} at {price}", alert=True)
                    buy_order = place_order_with_error_handling(
                        client,
                        product_id=product_id,
                        order_type=OrderType.MARKET,
                        side='buy',
                        size=ORDER_QTY
                    )
                    if buy_order:
                        order_state = buy_order.get('state')
                        order_id = buy_order.get('id')
                        if order_state == 'closed':
                            renko_param[symbol].update({
                                'option': 1,
                                'main_order_id': order_id,
                                'entry_price': price
                            })
                            log(f"✅ BUY executed on {symbol} — Entry: {price} | OrderID: {order_id}", alert=True)                            
                        else:
                            # Order exists but not closed (open/partially filled/etc). Alert and save response.
                            log(f"⚠️ BUY placed for {symbol} but not filled/closed. State: {order_state} | Order: {buy_order}", alert=True)
                    else:
                        log(f"⚠️ BUY order failed to be placed for {symbol}", alert=True)

                # --- BUY MANAGEMENT (simplified - no stop loss) ---
                elif option == 1:
                    # Exit condition: price breaks below EMA_21_DN or signal reversal
                    if close_15m < EMA_21:
                        # Exit position with market order
                        exit_order = place_order_with_error_handling(
                            client,
                            product_id=product_id,
                            order_type=OrderType.MARKET,
                            side='sell',
                            size=ORDER_QTY
                        )
                        
                        if exit_order and exit_order.get('state') == 'closed':
                            exit_price = price
                            entry_price = renko_param[symbol]['entry_price']
                            pnl = (exit_price - entry_price) * QTY[symbol]
                            
                            renko_param[symbol].update({
                                'option': 0,
                                'main_order_id': None,
                                'exit_price': exit_price,
                                'pnl': pnl
                            })
                            
                            reason = "Signal Reversal" if single == -1 else "EMA Break"
                            log(f"🔴 LONG Position Closed on {symbol} ({reason}) — Exit: {exit_price} | PnL: {pnl:.4f}", alert=True)
                        else:
                            log(f"⚠️ Failed to exit LONG position for {symbol}", alert=True)

                # --- SELL ENTRY ---
                if close_15m < EMA_21_UP and option == 0:
                    log(f"🔻 SELL signal for {symbol} at {price}", alert=True)
                    sell_order = place_order_with_error_handling(
                        client,
                        product_id=product_id,
                        order_type=OrderType.MARKET,
                        side='sell',
                        size=ORDER_QTY
                    )
                    if sell_order:
                        order_state = sell_order.get('state')
                        order_id = sell_order.get('id')
                        if order_state == 'closed':
                            renko_param[symbol].update({
                                'option': 2,
                                'main_order_id': order_id,
                                'entry_price': price
                            })
                            log(f"✅ SELL executed on {symbol} — Entry: {price} | OrderID: {order_id}", alert=True)
                            
                        else:
                            log(f"⚠️ SELL placed for {symbol} but not filled/closed. State: {order_state} | Order: {sell_order}", alert=True)
                    else:
                        log(f"⚠️ SELL order failed to be placed for {symbol}", alert=True)

                # --- SELL MANAGEMENT (simplified - no stop loss) ---
                elif option == 2:
                    # Exit condition: price breaks above EMA_21_UP or signal reversal
                    if close_15m > EMA_21:                  
                        # Exit position with market order
                        exit_order = place_order_with_error_handling(
                            client,
                            product_id=product_id,
                            order_type=OrderType.MARKET,
                            side='buy',
                            size=ORDER_QTY
                        )
                        
                        if exit_order and exit_order.get('state') == 'closed':
                            exit_price = price
                            entry_price = renko_param[symbol]['entry_price']
                            pnl = (entry_price - exit_price) * QTY[symbol]
                            
                            renko_param[symbol].update({
                                'option': 0,
                                'main_order_id': None,
                                'exit_price': exit_price,
                                'pnl': pnl
                            })
                            
                            reason = "Signal Reversal" if single == 1 else "EMA Break"
                            log(f"🔴 SHORT Position Closed on {symbol} ({reason}) — Exit: {exit_price} | PnL: {pnl:.4f}", alert=True)
                        else:
                            log(f"⚠️ Failed to exit SHORT position for {symbol}", alert=True)

            df_status = pd.DataFrame.from_dict(renko_param, orient='index')
            print("\nCurrent Strategy Status:")
            print(df_status[['Date', 'close', 'option', 'single', 'pnl']])

            print("Waiting for next cycle...\n")
            t.sleep(55)

    except KeyboardInterrupt:
        log("🛑 Manual shutdown detected. Exiting gracefully...", alert=True)
        # Removed stop order cancellation logic
        break

    except Exception as e:
        log(f"[ERROR] {type(e).__name__}: {e}", alert=True)
        t.sleep(5)
        continue
