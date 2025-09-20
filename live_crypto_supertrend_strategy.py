import os
import time as t
import requests
import pandas as pd
import pandas_ta as ta
from datetime import datetime, timedelta
from delta_rest_client import DeltaRestClient, OrderType

# ---------------------------------------
# SETTINGS
# ---------------------------------------
symbols = ["BTCUSD", "ETHUSD"]   # trading pairs you want
ORDER_QTY = 10


client = DeltaRestClient(
    base_url='https://api.india.delta.exchange',  # Use the appropriate base URL
    api_key='6979WTZlN4vOUf2hOtAJ35A3t6GZc7',
    api_secret='89Pv4SCxEb5YrX2lbMBNhJPDKFJKHDFqJtXfZ6BKu18M0kMdoV4r8pT8RJsF'
)
# ---------------------------------------
# Get product IDs (futures only)
# ---------------------------------------
def get_futures_product_ids(symbols):
    url = "https://api.delta.exchange/v2/products"
    r = requests.get(url)
    data = r.json()

    product_map = {}
    if "result" in data:
        for p in data["result"]:
            if p["symbol"] in symbols and p.get("contract_type") == "perpetual_futures":
                product_map[p["symbol"]] = p["id"]
    return product_map

symbols_map = get_futures_product_ids(symbols)
if not symbols_map:
    raise ValueError("Could not fetch futures product IDs from Delta API")
print("Loaded futures product IDs:", symbols_map)

# Status dictionary
renko_param = {
    symbol: {
        'Date': '',
        'close': '',
        'option': 0,  # 0 = no position, 1 = long, 2 = short
        'single': 0,
        'EMA_21': None,
        'EMA_21_UP': None,
        'EMA_21_DN': None
    }
    for symbol in symbols_map
}

# ---------------------------------------
# Candle Fetch
# ---------------------------------------
def fetch_and_save_delta_candles(symbol, resolution='5m', days=7, save_dir='.', tz='Asia/Kolkata'):
    headers = {'Accept': 'application/json'}
    start = int((datetime.now() - timedelta(days=days)).timestamp())
    params = {
        'resolution': resolution,
        'symbol': symbol,
        'start': str(start),
        'end': str(int(t.time())),
    }
    url = 'https://api.india.delta.exchange/v2/history/candles'
    r = requests.get(url, params=params, headers=headers)
    data = r.json()

    if 'result' in data:
        df = pd.DataFrame(data['result'], columns=['time', 'open', 'high', 'low', 'close', 'volume'])
        df['time'] = pd.to_datetime(df['time'], unit='s', utc=True).dt.tz_convert(tz)
        df.set_index("time", inplace=True)
        df['symbol'] = symbol
        return df
    else:
        print(f"No data for {symbol}")
        return None

# ---------------------------------------
# Process symbol
# ---------------------------------------
def process_symbol(symbol, renko_param, ha_save_dir="./data/crypto"):
    df = fetch_and_save_delta_candles(symbol, resolution='5m', days=7, save_dir=ha_save_dir)
    if df is None or df.empty:
        return renko_param

    df = df.sort_index()

    # Heikin-Ashi
    df = ta.ha(open_=df['open'], high=df['high'], close=df['close'], low=df['low'])

    # EMA(21)
    df['EMA_21'] = ta.ema(df['HA_close'], length=21)

    # Fixed offsets
    offset = 200 if symbol == "BTCUSD" else 20
    df['EMA_21_UP'] = df['EMA_21'] + offset
    df['EMA_21_DN'] = df['EMA_21'] - offset

    # Trade signals
    df['single'] = 0
    df.loc[df['HA_close'] > df['EMA_21_UP'], 'single'] = 1
    df.loc[df['HA_close'] < df['EMA_21_DN'], 'single'] = -1

    # Save for debugging/backtest
    os.makedirs(ha_save_dir, exist_ok=True)
    df.to_csv(f"{ha_save_dir}/supertrend_live_{symbol}.csv")

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
# Main Loop
# ---------------------------------------
print("Starting live strategy for BTCUSD + ETHUSD...")

while True:
    try:
        now = datetime.now()

        if now.second == 10:  # run every minute at second 10
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

                # --- BUY ---
                if single == 1 and option == 0:
                    print(f"BUY signal for {symbol} at {price}")
                    renko_param[symbol]['option'] = 1
                    client.place_order(
                        product_id=product_id,
                        order_type=OrderType.MARKET,
                        side='buy',
                        size=ORDER_QTY
                    )

                elif option == 1 and price < EMA_21:
                    print(f"Exit BUY for {symbol} at {price}")
                    renko_param[symbol]['option'] = 0
                    client.place_order(
                        product_id=product_id,
                        order_type=OrderType.MARKET,
                        side='sell',
                        size=ORDER_QTY
                    )

                # --- SELL ---
                if single == -1 and option == 0:
                    print(f"SELL signal for {symbol} at {price}")
                    renko_param[symbol]['option'] = 2
                    client.place_order(
                        product_id=product_id,
                        order_type=OrderType.MARKET,
                        side='sell',
                        size=ORDER_QTY
                    )

                elif option == 2 and price > EMA_21:
                    print(f"Exit SELL for {symbol} at {price}")
                    renko_param[symbol]['option'] = 0
                    client.place_order(
                        product_id=product_id,
                        order_type=OrderType.MARKET,
                        side='buy',
                        size=ORDER_QTY
                    )

            # Show status
            df_status = pd.DataFrame.from_dict(renko_param, orient='index')
            print("\nCurrent Strategy Status:")
            print(df_status)

            print("Waiting for next cycle...\n")
            t.sleep(55)

    except Exception as e:
        print(f"[ERROR] {type(e).__name__}: {e}")
        t.sleep(5)
        continue
