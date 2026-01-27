import os
import time as t
import requests
import pandas as pd

from datetime import datetime, time, timedelta, date
from fyers_apiv3 import fyersModel
from utility.common_utility import (
    get_stock_instrument_token,
    high_low_trend
)

from dotenv import load_dotenv
load_dotenv()

# ===================== TELEGRAM =====================
TELEGRAM_BOT_TOKEN = os.getenv("TEL_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TEL_CHAT_ID")

def send_telegram(msg):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg}, timeout=5)
    except:
        pass

# ===================== CONFIG =====================
CLIENT_ID = "98E1TAKD4T-100"
QTY = 1
SL_POINTS = 50
COMMISSION_RATE = 0.0004

TOKEN_FILE = "auth/api_key/access_token.txt"
TOKEN_CSV = "data/trend_following_strategy.CSV"

# ===================== STATE =====================
fyers = None
token_df = None

token_loaded = False
model_loaded = False
last_run_minute = None
last_token_refresh_day = None

CE_position = PE_position = 0
CE_enter = PE_enter = 0
CE_SL = PE_SL = 0

# ===================== UTILS =====================
def generate_token():
    send_telegram("🔐 Generating Fyers token...")
    os.system("python auth/fyers_auth.py")

def load_fyers_model():
    access_token = open(TOKEN_FILE).read().strip()
    fyers = fyersModel.FyersModel(
        client_id=CLIENT_ID,
        token=access_token,
        is_async=False,
        log_path=""
    )
    fyers.get_profile()  # force validation
    send_telegram("✅ Fyers model loaded")
    return fyers

def load_option_tokens(fyers):
    if os.path.exists(TOKEN_CSV):
        return pd.read_csv(TOKEN_CSV)

    tickers = [{
        'strategy_name': 'renko_traditional_box',
        'name': 'BANKNIFTY',
        'segment-name': 'NSE:NIFTY BANK',
        'segment': 'NFO-OPT',
        'expiry': 0,
        'offset': 1
    }]

    df = pd.DataFrame(get_stock_instrument_token(tickers, fyers))
    df.to_csv(TOKEN_CSV, index=False)
    return df

def calculate_pnl(entry, exit):
    gross = (exit - entry) * QTY
    turnover = (entry + exit) * QTY
    return round(gross - turnover * COMMISSION_RATE, 2)

# ===================== STRATEGY =====================
def run_strategy(token_df, fyers):
    global CE_position, PE_position
    global CE_enter, PE_enter
    global CE_SL, PE_SL
    global last_run_minute

    now = datetime.now()
    if last_run_minute == now.minute:
        return
    last_run_minute = now.minute

    # ?end_telegram(f"⏱ Strategy running {now.strftime('%H:%M')}")

    CE_SYMBOL = "NSE:" + token_df.loc[0, "tradingsymbol"]
    PE_SYMBOL = "NSE:" + token_df.loc[1, "tradingsymbol"]

    start_date = date.today() - timedelta(days=5)
    end_date = date.today()

    df_CE = high_low_trend({
        "symbol": CE_SYMBOL,
        "resolution": "5",
        "date_format": "1",
        "range_from": start_date.strftime('%Y-%m-%d'),
        "range_to": end_date.strftime('%Y-%m-%d'),
        "cont_flag": "1"
    }, fyers)

    df_PE = high_low_trend({
        "symbol": PE_SYMBOL,
        "resolution": "5",
        "date_format": "1",
        "range_from": start_date.strftime('%Y-%m-%d'),
        "range_to": end_date.strftime('%Y-%m-%d'),
        "cont_flag": "1"
    }, fyers)

    quotes = fyers.quotes({"symbols": f"{CE_SYMBOL},{PE_SYMBOL}"})["d"]
    prices = {q["v"]["short_name"]: q["v"]["lp"] for q in quotes}

    CE_price = prices[token_df.loc[0, "tradingsymbol"]]
    PE_price = prices[token_df.loc[1, "tradingsymbol"]]

    last_CE = df_CE.iloc[-2]
    last_PE = df_PE.iloc[-2]

    if CE_position == 0 and last_CE["trade_single"] == 1:
        CE_position = 1
        CE_enter = CE_price
        CE_SL = CE_price - SL_POINTS
        send_telegram(f"🟢 CE BUY @ {CE_price}")

    elif CE_position == 1 and (CE_price <= CE_SL or last_CE["trade_single"] == -1):
        CE_position = 0
        pnl = calculate_pnl(CE_enter, CE_price)
        send_telegram(f"🔴 CE SELL @ {CE_price} | PnL ₹{pnl}")

    if PE_position == 0 and last_PE["trade_single"] == 1:
        PE_position = 1
        PE_enter = PE_price
        PE_SL = PE_price - SL_POINTS
        send_telegram(f"🟢 PE BUY @ {PE_price}")

    elif PE_position == 1 and (PE_price <= PE_SL or last_PE["trade_single"] == -1):
        PE_position = 0
        pnl = calculate_pnl(PE_enter, PE_price)
        send_telegram(f"🔴 PE SELL @ {PE_price} | PnL ₹{pnl}")

# ===================== MAIN =====================
send_telegram("🟢 Algo Started")

while True:
    try:
        now = datetime.now()

        # 🔹 START OR RESTART → FORCE TOKEN + MODEL
        if not token_loaded:
            generate_token()
            fyers = load_fyers_model()
            token_df = load_option_tokens(fyers)
            token_loaded = model_loaded = True
            last_token_refresh_day = now.date()

        # 🔹 DAILY 9:00 REFRESH
        if now.time() >= time(9, 0) and last_token_refresh_day != now.date():
            send_telegram("♻ Daily 9:00 token refresh")
            generate_token()
            fyers = load_fyers_model()
            token_df = load_option_tokens(fyers)
            last_token_refresh_day = now.date()

        # 🔹 STRATEGY
        if model_loaded and time(9, 20) <= now.time() <= time(15, 15):
            run_strategy(token_df, fyers)

        t.sleep(1)

    except Exception as e:
        send_telegram(f"❌ Algo error: {e}")
        t.sleep(5)
