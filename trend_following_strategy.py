import os
import time as t
import requests
import pandas as pd
import pytz
from datetime import datetime, time, timedelta
from fyers_apiv3 import fyersModel
from utility.common_utility import get_stock_instrument_token, high_low_trend
from dotenv import load_dotenv

load_dotenv()

# ================= TIME =================
IST = pytz.timezone("Asia/Kolkata")

def now_ist():
    return datetime.now(IST)

# ================= TELEGRAM =================
TELEGRAM_BOT_TOKEN = os.getenv("TEL_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TEL_CHAT_ID")

def send_telegram(msg):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg}, timeout=5)
    except:
        pass

# ================= CONFIG =================
CLIENT_ID = "98E1TAKD4T-100"
QTY = 1
SL_POINTS = 50
COMMISSION_RATE = 0.0004
EXIT_TIME = time(15, 15)

TOKEN_FILE = "auth/api_key/access_token.txt"
TOKEN_CSV = "data/Trend_Following/instrument_token.csv"
strategy_name = "Trend_Following"

folder = f"data/{strategy_name}"
os.makedirs(folder, exist_ok=True)

# ================= STATE =================
current_trading_day = now_ist().date()
token_generated = False
model_loaded = False

CE_position = PE_position = 0
CE_enter = PE_enter = 0
CE_enter_time = PE_enter_time = None
CE_SL = PE_SL = 0

fyers = None
token_df = None

# ================= UTILS =================
def commission(price, qty):
    return round(price * qty * COMMISSION_RATE, 6)

def calculate_pnl(entry, exit, qty):
    return round((exit - entry) * qty, 6)

def save_trade(row):
    path = f"{folder}/live_trades.csv"
    df = pd.DataFrame([row])
    df.to_csv(path, mode="a", header=not os.path.exists(path), index=False)

# ================= DO NOT TOUCH =================
def save_processed_data(df, ha, symbol):
    path = os.path.join(folder, f"{symbol}_processed.csv")
    out = pd.DataFrame({
        "time": df.index,
        "HA_open": ha["HA_open"],
        "HA_high": ha["HA_high"],
        "HA_low": ha["HA_low"],
        "HA_close": ha["HA_close"],
        "trendline": ha["trendline"],
        "atr_condition": ha["atr_condition"],
        "trade_single": ha["trade_single"]
    })
    out.to_csv(path, index=False)

# ================= FYERS =================
def generate_daily_token():
    send_telegram("🔐 Generating token")
    return os.system("python auth/fyers_auth.py") == 0

def load_fyers():
    token = open(TOKEN_FILE).read().strip()
    fy = fyersModel.FyersModel(
        client_id=CLIENT_ID,
        token=token,
        is_async=False,
        log_path=""
    )
    return fy if fy.get_profile().get("s") == "ok" else None

def load_tokens(fyers):
    if os.path.exists(TOKEN_CSV):
        return pd.read_csv(TOKEN_CSV)

    tickers = [{
        "strategy_name": strategy_name,
        "name": "BANKNIFTY",
        "segment-name": "NSE:NIFTY BANK",
        "segment": "NFO-OPT",
        "expiry": 0,
        "offset": 1
    }]

    df = pd.DataFrame(get_stock_instrument_token(tickers, fyers))
    df.to_csv(TOKEN_CSV, index=False)
    return df

# ================= STRATEGY =================
def run_strategy():
    global CE_position, PE_position, CE_enter, PE_enter
    global CE_enter_time, PE_enter_time, CE_SL, PE_SL

    now = now_ist()
    if now.second != 10:
        return

    CE_SYMBOL = "NSE:" + token_df.loc[0, "tradingsymbol"]
    PE_SYMBOL = "NSE:" + token_df.loc[1, "tradingsymbol"]

    start = (now.date() - timedelta(days=5)).strftime("%Y-%m-%d")
    end = now.date().strftime("%Y-%m-%d")

    base = {
        "resolution": "5",
        "date_format": "1",
        "range_from": start,
        "range_to": end,
        "cont_flag": "1"
    }

    df_CE = high_low_trend({**base, "symbol": CE_SYMBOL}, fyers)
    df_PE = high_low_trend({**base, "symbol": PE_SYMBOL}, fyers)

    save_processed_data(df_CE, df_CE, token_df.loc[0, "tradingsymbol"])
    save_processed_data(df_PE, df_PE, token_df.loc[1, "tradingsymbol"])

    quotes = fyers.quotes(data={"symbols": f"{CE_SYMBOL},{PE_SYMBOL}"})["d"]
    price = {q["v"]["short_name"]: q["v"]["lp"] for q in quotes}

    CE_price = price[token_df.loc[0, "tradingsymbol"]]
    PE_price = price[token_df.loc[1, "tradingsymbol"]]

    last_CE = df_CE.iloc[-2]
    last_PE = df_PE.iloc[-2]

    entry_allowed = now.minute % 5 == 0 and now.time() < EXIT_TIME

    # ===== CE =====
    if CE_position == 0 and last_CE["trade_single"] == 1 and entry_allowed:
        CE_position = 1
        CE_enter = CE_price
        CE_enter_time = now
        CE_SL = CE_price - SL_POINTS

    elif CE_position == 1 and (
        CE_price <= CE_SL
        or last_CE["HA_close"] < last_CE["trendline"]
        or now.time() >= EXIT_TIME
    ):
        pnl = calculate_pnl(CE_enter, CE_price, QTY)
        net = pnl - commission(CE_price, QTY)
        save_trade({
            "symbol": CE_SYMBOL,
            "side": "BUY",
            "entry_price": CE_enter,
            "exit_price": CE_price,
            "qty": QTY,
            "net_pnl": net,
            "entry_time": CE_enter_time,
            "exit_time": now
        })
        CE_position = 0

    # ===== PE =====
    if PE_position == 0 and last_PE["trade_single"] == 1 and entry_allowed:
        PE_position = 1
        PE_enter = PE_price
        PE_enter_time = now
        PE_SL = PE_price - SL_POINTS

    elif PE_position == 1 and (
        PE_price <= PE_SL
        or last_PE["HA_close"] < last_PE["trendline"]
        or now.time() >= EXIT_TIME
    ):
        pnl = calculate_pnl(PE_enter, PE_price, QTY)
        net = pnl - commission(PE_price, QTY)
        save_trade({
            "symbol": PE_SYMBOL,
            "side": "BUY",
            "entry_price": PE_enter,
            "exit_price": PE_price,
            "qty": QTY,
            "net_pnl": net,
            "entry_time": PE_enter_time,
            "exit_time": now
        })
        PE_position = 0

# ================= MAIN =================
send_telegram("🟢 Algo started")

while True:
    try:
        now = now_ist()

        if now.date() != current_trading_day:
            current_trading_day = now.date()
            token_generated = False
            model_loaded = False

        if not token_generated and time(9, 0) <= now.time() <= time(9, 5):
            token_generated = generate_daily_token()

        if token_generated and not model_loaded:
            fyers = load_fyers()
            token_df = load_tokens(fyers)
            model_loaded = True

        if model_loaded and time(9, 20) <= now.time() <= EXIT_TIME:
            run_strategy()

        t.sleep(1)

    except Exception as e:
        send_telegram(f"❌ {e}")
        t.sleep(5)
