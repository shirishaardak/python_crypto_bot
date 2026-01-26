import os
import sys
import pandas as pd
from datetime import datetime, time, timedelta, date
import time as t

from fyers_apiv3 import fyersModel
from utility.common_utility import (
    get_stock_instrument_token,
    high_low_trend
)

CLIENT_ID = "98E1TAKD4T-100"
QTY = 1
SL_POINTS = 50
COMMISSION_RATE = 0.0004

TOKEN_FILE = "auth/api_key/access_token.txt"
TOKEN_CSV = "data/trend_following_strategy.CSV"

current_trading_day = date.today()
token_generated = False
model_loaded = False
strategy_active = False

fyers = None
token_df = None

CE_position = 0
PE_position = 0
CE_enter = PE_enter = 0
CE_SL = PE_SL = 0

orders = []

def calculate_pnl(entry, exit):
    gross = (exit - entry) * QTY
    turnover = (entry + exit) * QTY
    return round(gross - turnover * COMMISSION_RATE, 2)

def generate_daily_token():
    print("🔐 Generating daily token...")
    os.system("python auth/fyers_auth.py")
    print("✅ Token generated")

def load_fyers_model():
    access_token = open(TOKEN_FILE).read().strip()
    return fyersModel.FyersModel(
        client_id=CLIENT_ID,
        token=access_token,
        is_async=False,
        log_path=""
    )

def load_option_tokens(fyers):
    if os.path.exists(TOKEN_CSV):
        return pd.read_csv(TOKEN_CSV)

    tickers_name = [{
        'strategy_name': 'renko_traditional_box',
        'name': 'BANKNIFTY',
        'segment-name': 'NSE:NIFTY BANK',
        'segment': 'NFO-OPT',
        'expiry': 0,
        'offset': 1
    }]

    df = pd.DataFrame(
        get_stock_instrument_token(tickers_name, fyers)
    )
    df.to_csv(TOKEN_CSV, index=False)
    return df

def create_strategy_folder(strategy_name):
    folder_path = f"data/{strategy_name}"
    os.makedirs(folder_path, exist_ok=True)
    return folder_path

def save_processed_data(df, symbol, folder_path):
    processed_cols = [
        "time",
        "HA_open",
        "HA_high",
        "HA_low",
        "HA_close",        
        "atr_condition",
        "Trendline",
        "trade_single",
    ]
    if df.empty:
        print(f"⚠ Skipped {symbol}: DataFrame is empty")
        return
    existing_cols = [c for c in processed_cols if c in df.columns]
    if len(existing_cols) == 0:
        print(f"⚠ Skipped {symbol}: None of the required columns exist")
        return
    filename = f"{symbol}_processed.csv"
    path = os.path.join(folder_path, filename)
    df[existing_cols].to_csv(path, index=False)
    print(f"✅ Processed data saved: {path}")

def save_live_trades(orders, folder_path, trading_day):
    if not orders:
        print("⚠ No live trades to save")
        return
    df_orders = pd.DataFrame(orders)
    if "PnL" in df_orders.columns and df_orders["PnL"].sum() == 0:
        print("⚠ Skipped live trades: All PnL zero")
        return
    filename = f"live_trades_{trading_day}.csv"
    path = os.path.join(folder_path, filename)
    df_orders.to_csv(path, index=False)
    print(f"✅ Live trades saved: {path}")

def run_strategy(token_df, fyers, start_date, end_date, folder):
    global CE_position, PE_position
    global CE_enter, PE_enter
    global CE_SL, PE_SL
    global orders

    if datetime.now().second != 10:
        return

    CE_SYMBOL = "NSE:" + token_df.loc[0, "tradingsymbol"]
    PE_SYMBOL = "NSE:" + token_df.loc[1, "tradingsymbol"]

    data_CE = {
        "symbol": CE_SYMBOL,
        "resolution": "15",
        "date_format": "1",
        "range_from": start_date.strftime('%Y-%m-%d'),
        "range_to": end_date.strftime('%Y-%m-%d'),
        "cont_flag": "1"
    }

    data_PE = {
        "symbol": PE_SYMBOL,
        "resolution": "15",
        "date_format": "1",
        "range_from": start_date.strftime('%Y-%m-%d'),
        "range_to": end_date.strftime('%Y-%m-%d'),
        "cont_flag": "1"
    }

    df_CE = high_low_trend(data_CE, fyers)
    df_PE = high_low_trend(data_PE, fyers)

    save_processed_data(df_CE, "BANKNIFTY_CE", folder)
    save_processed_data(df_PE, "BANKNIFTY_PE", folder)

    last_CE, prev_CE = df_CE.iloc[-2], df_CE.iloc[-3]
    last_PE, prev_PE = df_PE.iloc[-2], df_PE.iloc[-3]

    quotes = fyers.quotes(
        data={"symbols": f"{CE_SYMBOL},{PE_SYMBOL}"}
    )["d"]

    price_map = {q["v"]["short_name"]: q["v"]["lp"] for q in quotes}
    CE_price = price_map[token_df.loc[0, "tradingsymbol"]]
    PE_price = price_map[token_df.loc[1, "tradingsymbol"]]

    if CE_position == 0 and last_CE["trade_single"] == 1:
        CE_position = 1
        CE_enter = CE_price
        CE_SL = CE_price - SL_POINTS
        order = {"Time": datetime.now(), "Strategy": "Trend_Following", "Symbol": CE_SYMBOL, "Side": "BUY", "Price": CE_price}
        orders.append(order)

    elif CE_position == 1 and (CE_price <= CE_SL or last_PE["trade_single"] == -1):
        pnl = calculate_pnl(CE_enter, CE_price)
        CE_position = 0
        order = {"Time": datetime.now(), "Strategy": "Trend_Following", "Symbol": CE_SYMBOL, "Side": "SELL", "Price": CE_price, "PnL": pnl}
        orders.append(order)

    if PE_position == 0 and last_PE["trade_single"] == 1:
        PE_position = 1
        PE_enter = PE_price
        PE_SL = PE_price - SL_POINTS
        order = {"Time": datetime.now(), "Strategy": "Trend_Following", "Symbol": PE_SYMBOL, "Side": "BUY", "Price": PE_price}
        orders.append(order)

    elif PE_position == 1 and (PE_price <= PE_SL or last_PE["trade_single"] == -1):
        pnl = calculate_pnl(PE_enter, PE_price)
        PE_position = 0
        order = {"Time": datetime.now(), "Strategy": "Trend_Following", "Symbol": PE_SYMBOL, "Side": "SELL", "Price": PE_price, "PnL": pnl}
        orders.append(order)

def reset_day():
    global token_generated, model_loaded, strategy_active
    global CE_position, PE_position, orders

    token_generated = False
    model_loaded = False
    strategy_active = False

    CE_position = PE_position = 0
    orders = []

    print("♻ Daily reset complete")

print("🟢 Algo service started (24×7)")

strategy_name = "Trend_Following"
folder = create_strategy_folder(strategy_name)

while True:
    now = datetime.now()
    today = now.date()

    if today != current_trading_day:
        current_trading_day = today
        reset_day()

    if now.time() >= time(9, 0) and not token_generated:
        generate_daily_token()
        token_generated = True

    if now.time() >= time(9, 20) and not model_loaded:
        fyers = load_fyers_model()
        token_df = load_option_tokens(fyers)
        model_loaded = True
        print("✅ Model & tokens loaded")

    if time(9, 20) <= now.time() <= time(15, 15) and model_loaded:
        start_date = today - timedelta(days=5)
        end_date = today
        run_strategy(token_df, fyers, start_date, end_date, folder)

    if now.time() > time(15, 15) and strategy_active:
        save_live_trades(orders, folder, current_trading_day)
        strategy_active = False
        print("🔴 Trading closed")

    t.sleep(1)
