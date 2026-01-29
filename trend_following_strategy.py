import os
import time
import requests
import pandas as pd
from datetime import datetime, timedelta, time as dtime
from dotenv import load_dotenv
from utility.common_utility import get_stock_instrument_token, high_low_trend

load_dotenv()

# ================= TELEGRAM =================
TELEGRAM_BOT_TOKEN = os.getenv("TEL_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TEL_CHAT_ID")

def send_telegram(msg):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg},
            timeout=5
        )
    except Exception as e:
        print("Telegram error:", e)

# ================= TIME =================
def ist_now():
    return datetime.utcnow() + timedelta(hours=5, minutes=30)

# ================= CONFIG =================
QTY = 1
SL_POINTS = 50
COMMISSION_RATE = 0.0004
TOKEN_CSV = "data/Trend_Following/instrument_token.csv"
folder = "data/Trend_Following"
os.makedirs(folder, exist_ok=True)
TRADES_FILE = f"{folder}/live_trades.csv"

# ================= STATE =================
token_df = None
CE_position = PE_position = 0
CE_enter = PE_enter = 0
CE_SL = PE_SL = 0
CE_enter_time = PE_enter_time = None

# ================= FYERS SETUP =================
from fyers_apiv3 import fyersModel
client_id = os.getenv("FYERS_CLIENT_ID")
access_token = os.getenv("FYERS_ACCESS_TOKEN")

fyers = fyersModel.FyersModel(
    client_id=client_id,
    token=access_token,
    log_path="."
)

# ================= LOAD SYMBOLS =================
def load_symbols():
    global token_df
    if os.path.exists(TOKEN_CSV):
        token_df = pd.read_csv(TOKEN_CSV)
        return

    tickers = [{
        "strategy_name": "Trend_Following",
        "name": "BANKNIFTY",
        "segment": "NFO-OPT",
        "expiry": 0,
        "offset": 1
    }]

    tokens = get_stock_instrument_token(tickers, fyers)
    token_df = pd.DataFrame(tokens)
    token_df.to_csv(TOKEN_CSV, index=False)

# ================= PNL =================
def commission(price, qty):
    return round(price * qty * COMMISSION_RATE, 6)

def calculate_pnl(entry, exit, qty):
    return round((exit - entry) * qty, 6)

def save_trade(trade):
    df = pd.DataFrame([trade])
    if not os.path.exists(TRADES_FILE):
        df.to_csv(TRADES_FILE, index=False)
    else:
        df.to_csv(TRADES_FILE, mode="a", header=False, index=False)

# ================= STRATEGY =================
def run_strategy():
    global CE_position, PE_position, CE_enter, PE_enter, CE_SL, PE_SL, CE_enter_time, PE_enter_time

    CE_SYMBOL = "NSE:" + token_df.loc[0, "tradingsymbol"]
    PE_SYMBOL = "NSE:" + token_df.loc[1, "tradingsymbol"]

    start = (ist_now().date() - timedelta(days=5)).strftime("%Y-%m-%d")
    end = ist_now().date().strftime("%Y-%m-%d")

    CE_data = {
        "symbol": CE_SYMBOL,
        "resolution": "5",
        "date_format": "1",
        "range_from": start,
        "range_to": end,
        "cont_flag": "1"
    }

    PE_data = {
        "symbol": PE_SYMBOL,
        "resolution": "5",
        "date_format": "1",
        "range_from": start,
        "range_to": end,
        "cont_flag": "1"
    }

    df_CE = high_low_trend(CE_data, fyers)
    df_PE = high_low_trend(PE_data, fyers)

    last_CE = df_CE.iloc[-2]
    last_PE = df_PE.iloc[-2]

    CE_price = fyers.quotes(data={"symbols": CE_SYMBOL})['d'][0]['v']['lp']
    PE_price = fyers.quotes(data={"symbols": PE_SYMBOL})['d'][0]['v']['lp']

    # ===== CE =====
    if CE_position == 0 and last_CE["trade_single"] == 1:
        CE_position = 1
        CE_enter = CE_price
        CE_enter_time = ist_now()
        CE_SL = CE_price - SL_POINTS
        send_telegram(f"🟢 CE BUY @ {CE_price}")

    elif CE_position == 1 and CE_price <= CE_SL:
        net = calculate_pnl(CE_enter, CE_price, QTY) - commission(CE_price, QTY)
        save_trade({
            "symbol": CE_SYMBOL,
            "entry_price": CE_enter,
            "exit_price": CE_price,
            "qty": QTY,
            "net_pnl": net,
            "entry_time": CE_enter_time.isoformat(),
            "exit_time": ist_now().isoformat()
        })
        CE_position = 0
        send_telegram(f"🔴 CE EXIT @ {CE_price} | Net ₹{round(net, 2)}")

    # ===== PE =====
    if PE_position == 0 and last_PE["trade_single"] == 1:
        PE_position = 1
        PE_enter = PE_price
        PE_enter_time = ist_now()
        PE_SL = PE_price - SL_POINTS
        send_telegram(f"🟢 PE BUY @ {PE_price}")

    elif PE_position == 1 and PE_price <= PE_SL:
        net = calculate_pnl(PE_enter, PE_price, QTY) - commission(PE_price, QTY)
        save_trade({
            "symbol": PE_SYMBOL,
            "entry_price": PE_enter,
            "exit_price": PE_price,
            "qty": QTY,
            "net_pnl": net,
            "entry_time": PE_enter_time.isoformat(),
            "exit_time": ist_now().isoformat()
        })
        PE_position = 0
        send_telegram(f"🔴 PE EXIT @ {PE_price} | Net ₹{round(net, 2)}")

# ================= MAIN LOOP =================
send_telegram("🚀 Trend Following Algo Started")
load_symbols()
current_trading_date = ist_now().date()

while True:
    try:
        now = ist_now()
        market_open = dtime(9, 15)
        market_close = dtime(15, 30)

        if now.time() > market_close and current_trading_date == now.date():
            CE_position = PE_position = 0
            current_trading_date = now.date() + timedelta(days=1)
            send_telegram("♻ End of day reset done")

        if market_open <= now.time() <= market_close:
            run_strategy()

        time.sleep(2)

    except Exception as e:
        send_telegram(f"❌ Algo error: {e}")
        time.sleep(5)
