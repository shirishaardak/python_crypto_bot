import os
import requests
import pandas as pd
import pandas_ta as ta
import time as t
from datetime import datetime, timedelta
from scipy.signal import argrelextrema
import numpy as np

# ---------------------------------------
# SETTINGS
# ---------------------------------------
symbols = ["BTCUSD", "ETHUSD"] 
# Different stoploss per symbol
STOPLOSS_POINTS = {
    "BTCUSD": 100,
    "ETHUSD": 10
}  # trailing stop loss in points
DATA_DIR = "./strategy_report"

# Initialize trade history
buy_orders, sell_orders = [], []

# State tracking per symbol
renko_param = {
    symbol: {
        'Date': '',
        'close': '',
        'tradingsymbol': symbol,
        'option': 0,
        'trailing_stop': None,
        'trendline': None,
        'prev_close': None,
        'prev_open': None
    }
    for symbol in symbols
}

# ---------------------------------------
# Fetch Delta Exchange candles
# ---------------------------------------
def fetch_delta_candles(symbol, resolution='5m', days=2, tz='Asia/Kolkata'):
    headers = {'Accept': 'application/json'}
    start = int((datetime.now() - timedelta(days=days)).timestamp())
    params = {
        'resolution': resolution,
        'symbol': symbol,
        'start': str(start),
        'end': str(int(t.time())),
    }
    url = 'https://api.delta.exchange/v2/history/candles'
    try:
        r = requests.get(url, params=params, headers=headers, timeout=10)
        r.raise_for_status()
        data = r.json()
        if 'result' not in data:
            return None
        df = pd.DataFrame(data['result'], columns=['time', 'open', 'high', 'low', 'close', 'volume'])
        df['time'] = pd.to_datetime(df['time'], unit='s', utc=True).dt.tz_convert(tz)
        df.set_index("time", inplace=True)
        df = df.sort_index()  # enforce oldest -> newest
        return df
    except Exception as e:
        print(f"API error for {symbol}: {e}")
        return None

# ---------------------------------------
# Process data and indicators
# ---------------------------------------
def process_symbol(symbol, renko_param, ha_save_dir="./data/crypto"):
    df = fetch_delta_candles(symbol)
    if df is None or df.empty:
        return renko_param

    # Compute Heikin-Ashi & keep raw
    ha_df = ta.ha(open_=df['open'], high=df['high'], close=df['close'], low=df['low'])
    df = pd.concat([df, ha_df], axis=1)

    # Extremes
    max_idx = argrelextrema(df['HA_high'].values, np.greater_equal, order=21)[0]
    min_idx = argrelextrema(df['HA_low'].values, np.less_equal, order=21)[0]

    df['max_high'] = df.iloc[max_idx]['HA_high']
    df['max_low'] = df.iloc[min_idx]['HA_low']
    df[['max_low', 'max_high']] = df[['max_low', 'max_high']].ffill()


    # Initialize Trendline column with NaN
    df['Trendline'] = np.nan

# Start with first HA high as initial trendline
    trendline = df['HA_high'].iloc[0]

    for i in range(len(df)):
        if i == 0:  
            df.at[df.index[i], 'Trendline'] = trendline
            continue

        if (
            df['HA_high'].iloc[i] == df['max_high'].iloc[i]
        ):
            trendline = df['HA_high'].iloc[i] if not pd.isna(df['HA_high'].iloc[i]) else trendline
        elif (
            df['HA_low'].iloc[i] == df['max_low'].iloc[i] 
            
        ):
            trendline = df['HA_low'].iloc[i] if not pd.isna(df['HA_low'].iloc[i]) else trendline

        # Always store current trendline
        df.at[df.index[i], 'Trendline'] = trendline

    # Save CSV for debugging/backtest
    os.makedirs(ha_save_dir, exist_ok=True)
    df.to_csv(f"{ha_save_dir}/crypto_trendline_strategy_{symbol}_ha.csv")

    # Last completed candle and previous
    last_row = df.iloc[-1]
    prev_row = df.iloc[-2]

    renko_param[symbol].update({
        'Date': last_row.name,
        'close': last_row['HA_close'],
        'trendline': last_row['Trendline'],
        'prev_close': prev_row['HA_close'],
        'prev_open': prev_row['HA_open'],
        'max_low' : last_row['max_low'],
        'max_high' : last_row['max_high']
    })

    return renko_param

# ---------------------------------------
# Main trading loop
# ---------------------------------------
while True:
    try:
        now = datetime.now()

        # Run strategy every minute near second=10
        if now.second % 60 == 10:
            for symbol in symbols:
                renko_param = process_symbol(symbol, renko_param)

            for symbol in symbols:
                price = renko_param[symbol]['close']
                trend = renko_param[symbol]['trendline']
                prev_close = renko_param[symbol]['prev_close']
                prev_open = renko_param[symbol]['prev_open']
                update_stoploss = price - trend
                print(f"Update Stoploss: {update_stoploss}")

                # --- BUY logic ---
                if price > trend  and price > prev_open and renko_param[symbol]['option'] == 0:
                    renko_param[symbol]['option'] = 1
                    renko_param[symbol]['trailing_stop'] = price - STOPLOSS_POINTS[symbol]
                    buy_orders.append({
                        'Date': datetime.now(), 'Symbol': symbol, 'Qty': 1,
                        'Buy/Sell': 'Buy', 'entry_price': price,
                        'exit_price': None, 'trailing_stop': price - STOPLOSS_POINTS[symbol]
                    })

                elif renko_param[symbol]['option'] == 1:
                    last_order = next((o for o in reversed(buy_orders) if o['exit_price'] is None), None)
                    if last_order:
                        # Update trailing stop
                        new_stop = price - STOPLOSS_POINTS[symbol]
                        if new_stop > last_order['trailing_stop']:
                            last_order['trailing_stop'] = new_stop
                            renko_param[symbol]['trailing_stop'] = new_stop

                        # Exit if price hits stop or breaks SuperTrend
                        if price < renko_param[symbol]['trailing_stop']:
                            renko_param[symbol]['option'] = 0
                            last_order['exit_price'] = price 
                            renko_param[symbol]['trailing_stop'] = None

                # --- SELL logic ---
                if price < trend  and price < prev_open and renko_param[symbol]['option'] == 0:
                    renko_param[symbol]['option'] = 2
                    renko_param[symbol]['trailing_stop'] = price + STOPLOSS_POINTS[symbol]
                    sell_orders.append({
                        'Date': datetime.now(), 'Symbol': symbol, 'Qty': 1,
                        'Buy/Sell': 'Sell', 'entry_price': price,
                        'exit_price': None, 'trailing_stop': price + STOPLOSS_POINTS[symbol]
                    })


                elif renko_param[symbol]['option'] == 2:
                    last_order = next((o for o in reversed(sell_orders) if o['exit_price'] is None), None)
                    if last_order:
                        # Update trailing stop
                        new_stop = price + STOPLOSS_POINTS[symbol]
                        if new_stop < last_order['trailing_stop']:
                            last_order['trailing_stop'] = new_stop
                            renko_param[symbol]['trailing_stop'] = new_stop

                        # Exit if price hits stop or breaks SuperTrend
                        if price > renko_param[symbol]['trailing_stop']:
                            renko_param[symbol]['option'] = 0
                            last_order['exit_price'] = price
                            renko_param[symbol]['trailing_stop'] = None

            # Save status & orders
            df_status = pd.DataFrame.from_dict(renko_param, orient='index')
            print(df_status)

            os.makedirs("strategy_report/bitcoin_strategy", exist_ok=True)

            if buy_orders:
                df_buy_orders = pd.DataFrame(buy_orders).sort_values(by=['Symbol'])
                df_buy_orders.to_csv('strategy_report/bitcoin_strategy/crypto_trendline_strategy_buy.csv', index=False)
                print(df_buy_orders.tail(5))

            if sell_orders:
                df_sell_orders = pd.DataFrame(sell_orders).sort_values(by=['Symbol'])
                df_sell_orders.to_csv('strategy_report/bitcoin_strategy/crypto_trendline_strategy_sell.csv', index=False)
                print(df_sell_orders.tail(5))

            # wait until next cycle
            t.sleep(55)

    except Exception as e:
        print(f"Error: {e}")
        t.sleep(5)
        continue
