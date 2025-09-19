import os
import sys
import requests
import pandas as pd
import pandas_ta as ta
import time as t
from datetime import datetime, timedelta
from delta_rest_client import DeltaRestClient, OrderType
import requests

# ---------------------------------------
# SETTINGS
# ---------------------------------------
symbols = ['ETHUSD']
buy_orders = []
sell_orders = []

STOPLOSS_POINTS = 20  # trailing stop loss in points
ORDER_QTY = 10  # number of contracts per trade
product_id = 3136


client = DeltaRestClient(
    base_url='https://api.india.delta.exchange',  # Use the appropriate base URL
    api_key='6979WTZlN4vOUf2hOtAJ35A3t6GZc7',
    api_secret='89Pv4SCxEb5YrX2lbMBNhJPDKFJKHDFqJtXfZ6BKu18M0kMdoV4r8pT8RJsF'
)

renko_param = {
    symbol: {
        'Date': '',
        'close': '',
        'tradingsymbol': '',
        'option': 0,
        'trailing_stop': None,
        'single': 0,
        'SUPERT': None
    }
    for symbol in symbols
}

# ---------------------------------------
# Fetch Delta Exchange candles
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
# Process data and indicators
# ---------------------------------------
def process_symbol(symbol, renko_param, ha_save_dir="./data/crypto"):
    df = fetch_and_save_delta_candles(symbol, resolution='5m', days=7, save_dir=ha_save_dir)
    if df is None or df.empty:
        return renko_param

    # Ensure sorted oldest → newest
    df = df.sort_index()

    # Compute Heikin-Ashi
    df = ta.ha(open_=df['open'], high=df['high'], close=df['close'], low=df['low'])

    # Supertrend
    df['EMA_21'] = ta.ema(df['HA_close'], length=9)
    df['EMA_21_UP'] =df['EMA_21'] + 20
    df['EMA_21_DN'] = df['EMA_21'] - 20

    # Trade signal column
    df['single'] = 0
    df.loc[(df['HA_close'] > df['EMA_21_UP']), 'single'] = 1
    df.loc[(df['HA_close'] < df['EMA_21_DN']), 'single'] = -1

    # Save CSV for debugging/backtest
    os.makedirs(ha_save_dir, exist_ok=True)
    df.to_csv(f"{ha_save_dir}/crypto_supertrend_live_strategy_{symbol}_ha.csv")

    # Last completed candle (use -2, not last unfinished one)
    last_row = df.iloc[-2]

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
# Main trading loop
# ---------------------------------------
print("Starting live crypto supertrend strategy...")
while True:
    try:
        now = datetime.now()
        # Run strategy every minute near second=10
        if now.second % 60 == 10:
            print(f"\n[{now.strftime('%Y-%m-%d %H:%M:%S')}] Running strategy cycle...")
            for symbol in symbols:
                print(f"Processing symbol: {symbol}")
                renko_param = process_symbol(symbol, renko_param)

            for symbol in symbols:
                price = renko_param[symbol]['close']
                EMA_21_UP = renko_param[symbol]['EMA_21_UP']
                EMA_21_DN = renko_param[symbol]['EMA_21_DN']
                EMA_21 = renko_param[symbol]['EMA_21']
                single = renko_param[symbol]['single']

                # --- BUY logic ---
                if single == 1 and renko_param[symbol]['option'] == 0:
                    print(f"Buy signal detected for {symbol} at price {price}. Placing buy order...")
                    renko_param[symbol]['option'] = 1
                    buy_order_place = client.place_order(
                        product_id=product_id,
                        order_type=OrderType.MARKET,
                        side='buy',
                        size=ORDER_QTY,
                    )
                   
                   
                    # if buy_order_place.get('state') == 'closed':
                    #     print(f"Placing trailing stop loss for BUY on {symbol}")
                    #     trailing_stop_order_buy = client.place_stop_order(
                    #         product_id=product_id,
                    #         size=ORDER_QTY,
                    #         side='sell',
                    #         order_type=OrderType.MARKET,
                    #         stop_price=EMA_21
                    #     )

                elif renko_param[symbol]['option'] == 1 and price < EMA_21:
                    renko_param[symbol]['option'] = 0
                    buy_order_place = client.place_order(
                        product_id=product_id,
                        order_type=OrderType.MARKET,
                        side='sell',
                        size=ORDER_QTY,
                    ) 
                    # get_order = client.get_live_orders()
                    # # for order in get_order:
                    # #     if order['state'] == 'untriggered' and order['id'] == trailing_stop_order_buy['id']:
                    # #         client.cancel_order(product_id=product_id, order_id=trailing_stop_order_buy['id'])
                    # #         print(f"Untriggered condition met for {symbol}  Cancelling trailing stop loss order...")                     
                    print(f"Buy exit condition met for {symbol} at price {price}. Cancelling trailing stop loss order...")

                    
                    
                    
                # --- SELL logic ---
                if single == -1 and renko_param[symbol]['option'] == 0:
                    print(f"Sell signal detected for {symbol} at price {price}. Placing sell order...")
                    renko_param[symbol]['option'] = 2
                    sell_order_place = client.place_order(
                        product_id=product_id,
                        order_type=OrderType.MARKET,
                        side='sell',
                        size=ORDER_QTY,
                    )
                             
                    
                    # if sell_order_place.get('state') == 'closed':
                    #     print(f"Placing trailing stop loss for SELL on {symbol}")
                    #     trailing_stop_order_sell = client.place_stop_order(
                    #         product_id=product_id,
                    #         size=10,
                    #         side='buy',
                    #         order_type=OrderType.MARKET,  
                    #         stop_price=EMA_21
                    #     )

                elif renko_param[symbol]['option'] == 2 and price > EMA_21:
                    renko_param[symbol]['option'] = 0       
                    sell_order_place = client.place_order(
                        product_id=product_id,
                        order_type=OrderType.MARKET,
                        side='buy',
                        size=ORDER_QTY,
                    )  
                    # get_order = client.get_live_orders()
                    # for order in get_order:
                    #     if order['state'] == 'untriggered' and order['id'] == trailing_stop_order_sell['id']:
                    #         client.cancel_order(product_id=product_id, order_id=trailing_stop_order_sell['id'])
                    #         print(f"Untriggered condition met for {symbol}  Cancelling trailing stop loss order...")                     
                    print(f"Sell exit condition met for {symbol} at price {price}. Cancelling trailing stop loss order...")

            # Save status & orders
            df_status = pd.DataFrame.from_dict(renko_param, orient='index')
            print("\nCurrent strategy status:")
            print(df_status)            

            print("Waiting for next cycle...\n")
            t.sleep(55)

    except Exception as e:
        print(f"[ERROR] Main Trading Loop: {type(e).__name__}: {e}")
        t.sleep(5)
        continue
