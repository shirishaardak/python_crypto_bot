import os
import time as t
import requests
import pandas as pd
import pytz
from datetime import datetime, time
from fyers_apiv3 import fyersModel
from utility.common_utility import get_stock_instrument_token, high_low_trend
from dotenv import load_dotenv

load_dotenv()

# ================= TIMEZONE FIX =================
# Monkey patch pytz to auto-correct lowercase 'asia/kolkata'
_real_timezone = pytz.timezone
def fixed_timezone(name):
    if name.lower() == "asia/kolkata":
        name = "Asia/Kolkata"
    return _real_timezone(name)
pytz.timezone = fixed_timezone

# IST timezone
IST = pytz.timezone("Asia/Kolkata")

def ist_now():
    """Return current datetime in IST with tz info"""
    return datetime.now(IST)

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
        print(f"Telegram error: {e}")  # for debug

# ================= CONFIG =================
CLIENT_ID = "98E1TAKD4T-100"
QTY = 1
SL_POINTS = 50
COMMISSION_RATE = 0.0004

TOKEN_FILE = "auth/api_key/access_token.txt"
TOKEN_CSV = "data/Trend_Following/instrument_token.csv"
folder = "data/Trend_Following"
os.makedirs(folder, exist_ok=True)
TRADES_FILE = f"{folder}/live_trades.csv"

# ================= FLAGS =================
token_loaded = False
model_loaded = False
symbols_loaded = False

# ================= STATE =================
fyers = None
token_df = None
CE_position = PE_position = 0
CE_enter = PE_enter = 0
CE_SL = PE_SL = 0
CE_enter_time = PE_enter_time = None

# ================= UTILS =================
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

def save_processed_data(ha, symbol):
    safe_symbol = symbol.replace(":", "_").replace("/", "_")
    path = os.path.join(folder, f"{safe_symbol}_processed.csv")
    out = pd.DataFrame({
        "time": ha.index,
        "HA_open": ha["HA_open"],
        "HA_high": ha["HA_high"],
        "HA_low": ha["HA_low"],
        "HA_close": ha["HA_close"],
        "trendline": ha["trendline"],
        "atr_condition": ha["atr_condition"],
        "trade_signal": ha["trade_single"]
    })
    out.to_csv(path, index=False)

# ================= FYERS =================
def load_token():
    send_telegram("🔐 Loading token")
    os.system("python auth/fyers_auth.py")
    return True

def load_model():
    send_telegram("🔌 Loading Fyers model")
    token = open(TOKEN_FILE).read().strip()
    fy = fyersModel.FyersModel(client_id=CLIENT_ID, token=token, is_async=False, log_path="")
    return fy

def load_symbols(fyers):
    send_telegram("📄 Loading symbols")
    if os.path.exists(TOKEN_CSV):
        return pd.read_csv(TOKEN_CSV)
    tickers = [{"strategy_name":"Trend_Following","name":"BANKNIFTY","segment-name":"NSE:NIFTY BANK","segment":"NFO-OPT","expiry":0,"offset":1}]
    df = pd.DataFrame(get_stock_instrument_token(tickers, fyers))
    df.to_csv(TOKEN_CSV, index=False)
    return df

# ================= STRATEGY =================
def run_strategy():
    global CE_position, PE_position, CE_enter, PE_enter, CE_SL, PE_SL
    global CE_enter_time, PE_enter_time

    CE_SYMBOL = "NSE:" + token_df.loc[0, "tradingsymbol"]
    PE_SYMBOL = "NSE:" + token_df.loc[1, "tradingsymbol"]

    start = (ist_now().date() - pd.Timedelta(days=5)).strftime("%Y-%m-%d")
    end = ist_now().date().strftime("%Y-%m-%d")
    base = {"resolution": "5", "date_format": "1", "range_from": start, "range_to": end, "cont_flag": "1"}

    df_CE = high_low_trend({**base, "symbol": CE_SYMBOL}, fyers)
    df_PE = high_low_trend({**base, "symbol": PE_SYMBOL}, fyers)

    save_processed_data(df_CE, token_df.loc[0, "tradingsymbol"])
    save_processed_data(df_PE, token_df.loc[1, "tradingsymbol"])

    quotes = fyers.quotes({"symbols": f"{CE_SYMBOL},{PE_SYMBOL}"})["d"]
    price_map = {q["v"]["short_name"]: q["v"]["lp"] for q in quotes}
    CE_price = price_map[token_df.loc[0, "tradingsymbol"]]
    PE_price = price_map[token_df.loc[1, "tradingsymbol"]]

    last_CE = df_CE.iloc[-2]
    last_PE = df_PE.iloc[-2]

    # ===== CE Entry/Exit =====
    if CE_position == 0 and last_CE["trade_single"] == 1:
        CE_position = 1
        CE_enter = CE_price
        CE_enter_time = ist_now()
        CE_SL = CE_price - SL_POINTS
        send_telegram(f"🟢 CE BUY @ {CE_price}")

    elif CE_position == 1 and CE_price <= CE_SL:
        net = calculate_pnl(CE_enter, CE_price, QTY) - commission(CE_price, QTY)
        save_trade({"symbol": CE_SYMBOL, "entry_price": CE_enter, "exit_price": CE_price, "qty": QTY,
                    "net_pnl": net, "entry_time": CE_enter_time.isoformat(), "exit_time": ist_now().isoformat()})
        CE_position = 0
        send_telegram(f"🔴 CE EXIT @ {CE_price} | Net ₹{round(net,2)}")

    # ===== PE Entry/Exit =====
    if PE_position == 0 and last_PE["trade_single"] == 1:
        PE_position = 1
        PE_enter = PE_price
        PE_enter_time = ist_now()
        PE_SL = PE_price - SL_POINTS
        send_telegram(f"🟢 PE BUY @ {PE_price}")

    elif PE_position == 1 and PE_price <= PE_SL:
        net = calculate_pnl(PE_enter, PE_price, QTY) - commission(PE_price, QTY)
        save_trade({"symbol": PE_SYMBOL, "entry_price": PE_enter, "exit_price": PE_price, "qty": QTY,
                    "net_pnl": net, "entry_time": PE_enter_time.isoformat(), "exit_time": ist_now().isoformat()})
        PE_position = 0
        send_telegram(f"🔴 PE EXIT @ {PE_price} | Net ₹{round(net,2)}")

# ================= MAIN LOOP =================
send_telegram("🚀 Trend Following Algo Started")
current_day = ist_now().date()

while True:
    try:
        now = ist_now()
        today = now.date()

        # ===== DAILY RESET at 15:30 =====
        if now.time() >= time(15,30) and current_day == today:
            token_loaded = model_loaded = symbols_loaded = False
            CE_position = PE_position = 0
            current_day = today + pd.Timedelta(days=1)
            send_telegram("♻ End of day reset done")

        # ===== LOAD EVERYTHING FIRST TIME / scheduled at 09:20 =====
        if not token_loaded and (time(9,15) <= now.time() < time(15,25)):
            load_token()
            token_loaded = True
        if not model_loaded and (time(9,20) <= now.time() < time(15,25)):
            fyers = load_model()
            model_loaded = True
        if not symbols_loaded and (time(9,25) <= now.time() < time(15,25)):
            token_df = load_symbols(fyers)
            symbols_loaded = True

        # ===== RUN STRATEGY ONLY DURING MARKET HOURS 09:30-15:30 =====
        if time(9,30) <= now.time() <= time(15,30) and symbols_loaded:
            run_strategy()

        t.sleep(1)

    except Exception as e:
        send_telegram(f"❌ Algo error: {e}")
        t.sleep(5)
