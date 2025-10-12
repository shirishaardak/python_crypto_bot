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
symbols = ["BTCUSD", "ETHUSD"]   # trading pairs you want
ORDER_QTY = 1

TRAIL_AMOUNTS = {
    "BTCUSD": 10,
    "ETHUSD": 5
}

# Use environment variables for security
api_key = os.getenv('DELTA_API_KEY')
api_secret = os.getenv('DELTA_API_SECRET')

client = DeltaRestClient(
    base_url='https://api.india.delta.exchange',
    api_key=api_key,
    api_secret=api_secret
)

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
        print(f"Error fetching product IDs: {e}")
        return {}

symbols_map = get_futures_product_ids(symbols)
if not symbols_map:
    raise ValueError("Could not fetch futures product IDs from Delta API")
print("Loaded futures product IDs:", symbols_map)

# Status dictionary - Track stop order IDs and main order IDs
renko_param = {
    symbol: {
        'Date': '',
        'close': '',
        'option': 0,  # 0 = no position, 1 = long, 2 = short
        'single': 0,
        'EMA_21': None,
        'EMA_21_UP': None,
        'EMA_21_DN': None,
        'stop_order_id': None,  # Track stop order IDs
        'main_order_id': None   # Track main order IDs
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
            print(f"No data for {symbol}")
            return None
    except Exception as e:
        print(f"Error fetching candles for {symbol}: {e}")
        return None

# ---------------------------------------
# Process symbol
# ---------------------------------------
def process_symbol(symbol, renko_param, ha_save_dir="./data/crypto"):
    df = fetch_and_save_delta_candles(symbol, resolution='1h', days=7, save_dir=ha_save_dir)
    if df is None or df.empty:
        return renko_param

    df = df.sort_index()

    # Heikin-Ashi
    df = ta.ha(open_=df['open'], high=df['high'], close=df['close'], low=df['low'])

    # EMA(21) - Note: Using length=9 as in original
    df['EMA_21'] = ta.ema(df['HA_close'], length=5)

    # Fixed offsets
    offset = 300 if symbol == "BTCUSD" else 30
    df['EMA_21_UP'] = df['EMA_21'] + offset
    df['EMA_21_DN'] = df['EMA_21'] - offset

    # Trade signals
    df['single'] = 0
    df.loc[(df['HA_close'] > df['EMA_21']) & (df['HA_close'] > df['HA_open'].shift(1)), 'single'] = 1
    df.loc[(df['HA_close'] < df['EMA_21']) & (df['HA_close'] < df['HA_open'].shift(1)), 'single'] = -1

    # Save for debugging/backtest
    os.makedirs(ha_save_dir, exist_ok=True)
    df.to_csv(f"{ha_save_dir}/supertrend_live_{symbol}.csv")

    if len(df) < 2:
        print(f"Not enough data for {symbol}")
        return renko_param

    last_row = df.iloc[-2]  # last closed candle

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
        print(f"Error placing order: {e}")
        return None

def place_stop_order_with_error_handling(client, **kwargs):
    try:
        response = client.place_stop_order(**kwargs)
        return response
    except Exception as e:
        print(f"Error placing stop order: {e}")
        return None

def cancel_order_with_error_handling(client, product_id, order_id):
    try:
        response = client.cancel_order(product_id, order_id)
        print(response)
        return response
    except Exception as e:
        print(f"Error cancelling order {order_id}: {e}")
        return None
    
def get_history_orders_with_error_handling(client, product_id):
    try:
        query = { "product_id": product_id }
        response = client.order_history(query, page_size=10)
        return response['result']
    
    except Exception as e:
        print(f"Error getting order history: {e}")
        return []

def get_live_orders_with_error_handling(client):
    try:
        return client.get_live_orders()
    
    except Exception as e:
        print(f"Error getting live orders: {e}")
        return []

# ---------------------------------------
# Main Loop
# ---------------------------------------
print("Starting live strategy for BTCUSD + ETHUSD...")

while True:
    try:
        now = datetime.now()

        if now.second == 10 and datetime.now().minute % 5 == 0:  # run every minute at second 10
            print(f"\n[{now.strftime('%Y-%m-%d %H:%M:%S')}] Running cycle...")

            # Process symbols
            for symbol in symbols_map:
                print(f"Processing {symbol}...")
                renko_param = process_symbol(symbol, renko_param)

            # Trading logic
            for symbol, product_id in symbols_map.items():
                price = renko_param[symbol]['close']
                EMA_21 = renko_param[symbol]['EMA_21']
                EMA_21_UP = renko_param[symbol]['EMA_21_UP']
                EMA_21_DN = renko_param[symbol]['EMA_21_DN']
                single = renko_param[symbol]['single']
                option = renko_param[symbol]['option']

                # --- BUY SIGNAL ---
                if single == 1 and option == 0:
                    print(f"BUY signal for {symbol} at {price}")
                    
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
                        
                        print(f"Placing trailing stop loss for BUY on {symbol}")
                        trailing_stop_order_buy = place_stop_order_with_error_handling(
                            client,
                            product_id=product_id,
                            size=ORDER_QTY,
                            side='sell',
                            order_type=OrderType.MARKET,
                            trail_amount=TRAIL_AMOUNTS[symbol],
                            isTrailingStopLoss=True
                        )
                        
                        if trailing_stop_order_buy:
                            renko_param[symbol]['stop_order_id'] = trailing_stop_order_buy.get('id')
                            print(f"Stop order placed with ID: {trailing_stop_order_buy.get('id')}")
                        else:
                            print(f"Failed to place stop order for {symbol}")

                # --- BUY POSITION MANAGEMENT ---
                elif option == 1:
                    stop_order_id = renko_param[symbol]['stop_order_id']
                    if stop_order_id:
                        print(f"Stop loss condition triggered for BUY position on {symbol}")
                        
                        # Get live orders to check stop order status
                        get_orders = get_live_orders_with_error_handling(client)
                        stop_order_found = False
                        stop_order_state = None
                        
                        # Check stop order status
                        for order in get_orders:
                            if order['id'] == stop_order_id:
                                stop_order_state = order['state']
                                if order['state'] == 'pending':
                                    stop_order_found = False
                                break
                        
                        # Handle different stop order states
                        if stop_order_found and price < EMA_21:
                            cancel_result = cancel_order_with_error_handling(client, product_id, stop_order_id)
                            print(f"Cancelled pending stop order {stop_order_id} for {symbol}")
                            
                            # Place market exit order after cancelling stop order
                            buy_exit_order_place = place_order_with_error_handling(
                                client,
                                product_id=product_id,
                                order_type=OrderType.MARKET,
                                side='sell',
                                size=ORDER_QTY
                            )                            
                            
                            if buy_exit_order_place:
                                print(f"Manual exit executed for BUY position on {symbol}")
                                renko_param[symbol]['option'] = 0
                                renko_param[symbol]['stop_order_id'] = None
                                renko_param[symbol]['main_order_id'] = None
                            else:
                                print(f"Failed to place exit order for BUY position on {symbol}")
                                
                        elif stop_order_state == 'closed':
                            print(f"Stop order {stop_order_id} already executed for {symbol}")
                            # Reset position since stop order already executed
                            renko_param[symbol]['option'] = 0
                            renko_param[symbol]['stop_order_id'] = None
                            renko_param[symbol]['main_order_id'] = None
                            
                        elif stop_order_state is None:
                            print(f"Stop order {stop_order_id} not found in live orders for {symbol}")
                            # Reset position state as stop order is missing
                            renko_param[symbol]['option'] = 0
                            renko_param[symbol]['stop_order_id'] = None
                            renko_param[symbol]['main_order_id'] = None

                # --- SELL SIGNAL ---
                if single == -1 and option == 0:
                    print(f"SELL signal for {symbol} at {price}")
                    
                    sell_order_place = place_order_with_error_handling(
                        client,
                        product_id=product_id,
                        order_type=OrderType.MARKET,
                        side='sell',
                        size=ORDER_QTY,                        
                    )
                    
                    if sell_order_place and sell_order_place.get('state') == 'closed':
                        renko_param[symbol]['option'] = 2  # Set to 2 for short position
                        renko_param[symbol]['main_order_id'] = sell_order_place.get('id')
                        
                        print(f"Placing trailing stop loss for SELL on {symbol}")
                        trailing_stop_order_sell = place_stop_order_with_error_handling(
                            client,
                            product_id=product_id,
                            size=ORDER_QTY,
                            side='buy',
                            order_type=OrderType.MARKET,
                            trail_amount=TRAIL_AMOUNTS[symbol],
                            isTrailingStopLoss=True
                        )
                        
                        if trailing_stop_order_sell:
                            renko_param[symbol]['stop_order_id'] = trailing_stop_order_sell.get('id')
                            print(f"Stop order placed with ID: {trailing_stop_order_sell.get('id')}")
                        else:
                            print(f"Failed to place stop order for {symbol}")

                # --- SELL POSITION MANAGEMENT ---
                elif option == 2:
                    stop_order_id = renko_param[symbol]['stop_order_id']
                    if stop_order_id:
                        print(f"Stop loss condition triggered for SELL position on {symbol}")
                        
                        # Get live orders to check stop order status
                        get_orders = get_live_orders_with_error_handling(client)
                        stop_order_found = False
                        stop_order_state = None
                        
                        # Check stop order status
                        for order in get_orders:
                            if order['id'] == stop_order_id:
                                stop_order_state = order['state']
                                if order['state'] == 'pending':
                                    stop_order_found = False
                                break
                        
                        # Handle different stop order states
                        if stop_order_found and price > EMA_21:
                            cancel_result = cancel_order_with_error_handling(client, product_id, stop_order_id)
                            print(f"Cancelled pending stop order {stop_order_id} for {symbol}")
                            
                            # Place market exit order after cancelling stop order
                            sell_exit_order_place = place_order_with_error_handling(
                                client,
                                product_id=product_id,
                                order_type=OrderType.MARKET,
                                side='buy',
                                size=ORDER_QTY
                            )                            
                            
                            if sell_exit_order_place:
                                print(f"Manual exit executed for SELL position on {symbol}")
                                renko_param[symbol]['option'] = 0
                                renko_param[symbol]['stop_order_id'] = None
                                renko_param[symbol]['main_order_id'] = None
                            else:
                                print(f"Failed to place exit order for SELL position on {symbol}")
                                
                        elif stop_order_state == 'closed':
                            print(f"Stop order {stop_order_id} already executed for {symbol}")
                            # Reset position since stop order already executed
                            renko_param[symbol]['option'] = 0
                            renko_param[symbol]['stop_order_id'] = None
                            renko_param[symbol]['main_order_id'] = None
                            
                        elif stop_order_state is None:
                            print(f"Stop order {stop_order_id} not found in live orders for {symbol}")
                            # Reset position state as stop order is missing
                            renko_param[symbol]['option'] = 0
                            renko_param[symbol]['stop_order_id'] = None
                            renko_param[symbol]['main_order_id'] = None

            # Show status
            df_status = pd.DataFrame.from_dict(renko_param, orient='index')
            print("\nCurrent Strategy Status:")
            print(df_status[['Date', 'close', 'option', 'single', 'stop_order_id']])

            print("Waiting for next cycle...\n")
            t.sleep(55)

    except KeyboardInterrupt:
        print("\nShutting down gracefully...")
        # Cancel any open stop orders before exiting
        for symbol in symbols_map:
            stop_order_id = renko_param[symbol]['stop_order_id']
            if stop_order_id:
                print(f"Cancelling stop order {stop_order_id} for {symbol}")
                cancel_order_with_error_handling(client, stop_order_id, symbols_map[symbol])
        break
        
    except Exception as e:
        print(f"[ERROR] {type(e).__name__}: {e}")
        t.sleep(5)
        continue
