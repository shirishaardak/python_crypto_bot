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
# ===================== TELEGRAM CONFIG =====================
TELEGRAM_BOT_TOKEN = os.getenv("TEL_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TEL_CHAT_ID")

def send_telegram(message):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML"
        }
        r = requests.post(url, json=payload, timeout=5)
        if r.status_code != 200:
            print("Telegram failed:", r.text)
    except Exception as e:
        print("Telegram exception:", e)

# ===================== STRATEGY CONFIG =====================
CLIENT_ID = "98E1TAKD4T-100"
QTY = 1
SL_POINTS = 50
COMMISSION_RATE = 0.0004

TOKEN_FILE = "auth/api_key/access_token.txt"
TOKEN_CSV = "data/trend_following_strategy.CSV"

strategy_name = "Trend_Following"

# ===================== GLOBAL STATE =====================
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

# ===================== UTILS =====================
def calculate_pnl(entry, exit):
    gross = (exit - entry) * QTY
    turnover = (entry + exit) * QTY
    return round(gross - turnover * COMMISSION_RATE, 2)

def generate_daily_token():
    send_telegram("🔐 Generating Fyers access token...")
    try:
        exit_code = os.system("python auth/fyers_auth.py")

        if exit_code != 0:
            send_telegram("❌ Token generation script failed")
            return False

        if not os.path.exists(TOKEN_FILE):
            send_telegram("❌ Access token file not found")
            return False

        token = open(TOKEN_FILE).read().strip()
        if len(token) < 10:
            send_telegram("❌ Invalid / empty access token")
            return False

        send_telegram("✅ Fyers token generated successfully")
        return True

    except Exception as e:
        send_telegram(f"❌ Token generation error\n<code>{e}</code>")
        return False

def load_fyers_model():
    try:
        access_token = open(TOKEN_FILE).read().strip()

        fyers = fyersModel.FyersModel(
            client_id=CLIENT_ID,
            token=access_token,
            is_async=False,
            log_path=""
        )

        profile = fyers.get_profile()
        if profile.get("s") != "ok":
            raise Exception("Profile validation failed")

        send_telegram("✅ Fyers API login successful")
        return fyers

    except Exception as e:
        send_telegram(f"❌ Fyers API login failed\n<code>{e}</code>")
        return None

def load_option_tokens(fyers):
    try:
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

        df = pd.DataFrame(get_stock_instrument_token(tickers_name, fyers))
        df.to_csv(TOKEN_CSV, index=False)
        return df

    except Exception as e:
        send_telegram(f"❌ Token CSV generation failed\n<code>{e}</code>")
        return None

def create_strategy_folder(strategy_name):
    folder = f"data/{strategy_name}"
    os.makedirs(folder, exist_ok=True)
    return folder

def save_live_trades(orders, folder, trading_day):
    if not orders:
        return
    df = pd.DataFrame(orders)
    file = f"{folder}/live_trades_{trading_day}.csv"
    df.to_csv(file, index=False)
    send_telegram("📁 Live trades saved")

# ===================== STRATEGY =====================
def run_strategy(token_df, fyers, start_date, end_date):
    global CE_position, PE_position
    global CE_enter, PE_enter
    global CE_SL, PE_SL
    global orders, strategy_active

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

    try:
        df_CE = high_low_trend(data_CE, fyers)
        df_PE = high_low_trend(data_PE, fyers)

        if df_CE is None or df_PE is None or df_CE.empty or df_PE.empty:
            raise Exception("Empty candle data")

    except Exception as e:
        send_telegram(f"❌ Candle API failed\n<code>{e}</code>")
        return

    try:
        resp = fyers.quotes(data={"symbols": f"{CE_SYMBOL},{PE_SYMBOL}"})
        if resp.get("s") != "ok":
            raise Exception(resp)

        quotes = resp["d"]
        price_map = {q["v"]["short_name"]: q["v"]["lp"] for q in quotes}

        CE_price = price_map[token_df.loc[0, "tradingsymbol"]]
        PE_price = price_map[token_df.loc[1, "tradingsymbol"]]

    except Exception as e:
        send_telegram(f"❌ Quotes API failed\n<code>{e}</code>")
        return

    last_CE = df_CE.iloc[-2]
    last_PE = df_PE.iloc[-2]

    # ===== CE ENTRY =====
    if CE_position == 0 and last_CE["trade_single"] == 1:
        CE_position = 1
        CE_enter = CE_price
        CE_SL = CE_price - SL_POINTS
        strategy_active = True

        orders.append({"Time": datetime.now(), "Symbol": CE_SYMBOL, "Side": "BUY", "Price": CE_price})
        send_telegram(f"🟢 CE BUY\n{CE_SYMBOL}\nPrice: {CE_price}\nSL: {CE_SL}")

    elif CE_position == 1 and (CE_price <= CE_SL or last_CE["trade_single"] == -1):
        pnl = calculate_pnl(CE_enter, CE_price)
        CE_position = 0

        orders.append({"Time": datetime.now(), "Symbol": CE_SYMBOL, "Side": "SELL", "Price": CE_price, "PnL": pnl})
        send_telegram(f"🔴 CE SELL\n{CE_SYMBOL}\nExit: {CE_price}\nPnL: ₹{pnl}")

    # ===== PE ENTRY =====
    if PE_position == 0 and last_PE["trade_single"] == 1:
        PE_position = 1
        PE_enter = PE_price
        PE_SL = PE_price - SL_POINTS
        strategy_active = True

        orders.append({"Time": datetime.now(), "Symbol": PE_SYMBOL, "Side": "BUY", "Price": PE_price})
        send_telegram(f"🟢 PE BUY\n{PE_SYMBOL}\nPrice: {PE_price}\nSL: {PE_SL}")

    elif PE_position == 1 and (PE_price <= PE_SL or last_PE["trade_single"] == -1):
        pnl = calculate_pnl(PE_enter, PE_price)
        PE_position = 0

        orders.append({"Time": datetime.now(), "Symbol": PE_SYMBOL, "Side": "SELL", "Price": PE_price, "PnL": pnl})
        send_telegram(f"🔴 PE SELL\n{PE_SYMBOL}\nExit: {PE_price}\nPnL: ₹{pnl}")

# ===================== DAILY RESET =====================
def reset_day():
    global token_generated, model_loaded, strategy_active
    global CE_position, PE_position, orders

    token_generated = False
    model_loaded = False
    strategy_active = False
    CE_position = PE_position = 0
    orders = []

    send_telegram("♻ Daily reset completed")

# ===================== MAIN LOOP =====================
send_telegram("🟢 Trend Following Algo Started")

folder = create_strategy_folder(strategy_name)

while True:
    try:
        now = datetime.now()
        today = now.date()

        if today != current_trading_day:
            current_trading_day = today
            reset_day()

        if now.time() >= time(9, 0) and not token_generated:
            token_generated = generate_daily_token()

        if now.time() >= time(9, 20) and not model_loaded:
            fyers = load_fyers_model()
            if fyers:
                token_df = load_option_tokens(fyers)
                model_loaded = token_df is not None

        if time(9, 20) <= now.time() <= time(15, 15) and model_loaded:
            start_date = today - timedelta(days=5)
            end_date = today
            run_strategy(token_df, fyers, start_date, end_date)

        if now.time() > time(15, 15) and strategy_active:
            save_live_trades(orders, folder, current_trading_day)
            send_telegram("🔴 Trading session closed")
            strategy_active = False

        t.sleep(1)

    except Exception as e:
        send_telegram(f"❌ Algo crash\n<code>{e}</code>")
        t.sleep(5)
