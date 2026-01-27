import os
import time as t
import requests
import pandas as pd
from datetime import datetime, time, timedelta, date
from fyers_apiv3 import fyersModel
from utility.common_utility import get_stock_instrument_token, high_low_trend
from dotenv import load_dotenv
load_dotenv()

# ================= TELEGRAM =================
TELEGRAM_BOT_TOKEN = os.getenv("TEL_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TEL_CHAT_ID")

def send_telegram(msg):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode":"HTML"}, timeout=5)
    except:
        pass

# ================= CONFIG =================
CLIENT_ID = "98E1TAKD4T-100"
QTY = 1
SL_POINTS = 50
COMMISSION_RATE = 0.0004

TOKEN_FILE = "auth/api_key/access_token.txt"
TOKEN_CSV = "data/Trend_Following/instrument_token.csv"
strategy_name = "Trend_Following"

# ================= STATE =================
current_trading_day = date.today()
token_generated = False
model_loaded = False
strategy_active = False

fyers = None
token_df = None

CE_position = PE_position = 0
CE_enter = PE_enter = 0
CE_SL = PE_SL = 0
orders = []

# ================= FOLDERS =================
folder = f"data/{strategy_name}"
os.makedirs(folder, exist_ok=True)

# ================= UTILS =================
def calculate_pnl(entry, exit):
    gross = (exit - entry) * QTY
    turnover = (entry + exit) * QTY
    return round(gross - turnover * COMMISSION_RATE, 2)

def generate_daily_token():
    while True:
        try:
            send_telegram("🔐 Generating Fyers token...")
            code = os.system("python auth/fyers_auth.py")
            if code == 0 and os.path.exists(TOKEN_FILE):
                send_telegram("✅ Token generated successfully")
                return True
            else:
                send_telegram("❌ Token generation failed, retrying...")
        except Exception as e:
            send_telegram(f"❌ Token error: {e}")
        t.sleep(5)

def load_fyers_model():
    global fyers
    while True:
        try:
            token = open(TOKEN_FILE).read().strip()
            fy = fyersModel.FyersModel(
                client_id=CLIENT_ID,
                token=token,
                is_async=False,
                log_path=""
            )
            if fy.get_profile().get("s") == "ok":
                send_telegram("✅ Fyers model loaded successfully")
                fyers = fy
                return fy
            else:
                send_telegram("❌ Model load failed, retrying...")
        except Exception as e:
            send_telegram(f"❌ Model error: {e}")
        t.sleep(5)

def load_option_tokens(fyers):
    if os.path.exists(TOKEN_CSV):
        return pd.read_csv(TOKEN_CSV)
    tickers = [{
        'strategy_name': strategy_name,
        'name': 'BANKNIFTY',
        'segment-name': 'NSE:NIFTY BANK',
        'segment': 'NFO-OPT',
        'expiry': 0,
        'offset': 1
    }]
    df = pd.DataFrame(get_stock_instrument_token(tickers, fyers))
    df.to_csv(TOKEN_CSV, index=False)
    return df

def save_live_trades():
    if not orders:
        return
    df = pd.DataFrame(orders)
    df.to_csv(f"{folder}/live_trades.csv", index=False)
    send_telegram("📁 Trades saved")

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

# ================= STRATEGY =================
def run_strategy():
    global CE_position, PE_position, CE_enter, PE_enter, CE_SL, PE_SL, strategy_active

    # Only run at second = 10 to avoid multiple runs per second
    if datetime.now().second != 10:
        return

    CE_SYMBOL = "NSE:" + token_df.loc[0, "tradingsymbol"]
    PE_SYMBOL = "NSE:" + token_df.loc[1, "tradingsymbol"]

    start = (date.today() - timedelta(days=5)).strftime("%Y-%m-%d")
    end = date.today().strftime("%Y-%m-%d")

    data_CE = {
        "symbol": CE_SYMBOL,
        "resolution": "5",
        "date_format": "1",
        "range_from": start,
        "range_to": end,
        "cont_flag": "1"
    }

    data_PE = data_CE.copy()
    data_PE["symbol"] = PE_SYMBOL

    # Calculate trend / indicators
    df_CE = high_low_trend(data_CE, fyers)
    df_PE = high_low_trend(data_PE, fyers)

    # Save processed data per candle
    save_processed_data(df_CE, df_CE, token_df.loc[0, "tradingsymbol"])
    save_processed_data(df_PE, df_PE, token_df.loc[1, "tradingsymbol"])

    # Get current prices
    quotes = fyers.quotes(data={"symbols": f"{CE_SYMBOL},{PE_SYMBOL}"})["d"]
    price = {q["v"]["short_name"]: q["v"]["lp"] for q in quotes}

    CE_price = price[token_df.loc[0, "tradingsymbol"]]
    PE_price = price[token_df.loc[1, "tradingsymbol"]]

    last_CE = df_CE.iloc[-2]
    last_PE = df_PE.iloc[-2]

    # Check if current time is at 15-min entry slot
    entry_allowed = datetime.now().minute % 15 == 0

    # ===== CE =====
    if CE_position == 0 and last_CE["trade_single"] == 1 and entry_allowed:
        CE_position = 1
        CE_enter = CE_price
        CE_SL = CE_price - SL_POINTS
        strategy_active = True
        orders.append({
            "symbol": CE_SYMBOL,
            "action": "BUY",
            "price": CE_price,
            "time": datetime.now()
        })
        send_telegram(f"🟢 CE BUY {CE_price}")

    elif CE_position == 1 and (CE_price <= CE_SL or last_CE["HA_close"] < last_CE["trendline"] ):
        pnl = calculate_pnl(CE_enter, CE_price)
        CE_position = 0
        orders.append({
            "symbol": CE_SYMBOL,
            "action": "SELL",
            "price": CE_price,
            "pnl": pnl,
            "time": datetime.now()
        })
        send_telegram(f"🔴 CE SELL {CE_price} | PnL ₹{pnl}")

    # ===== PE =====
    if PE_position == 0 and last_PE["trade_single"] == 1 and entry_allowed:
        PE_position = 1
        PE_enter = PE_price
        PE_SL = PE_price - SL_POINTS
        strategy_active = True
        orders.append({
            "symbol": PE_SYMBOL,
            "action": "BUY",
            "price": PE_price,
            "time": datetime.now()
        })
        send_telegram(f"🟢 PE BUY {PE_price}")

    elif PE_position == 1 and (PE_price <= PE_SL or last_PE["HA_close"] < last_PE["trendline"] ):
        pnl = calculate_pnl(PE_enter, PE_price)
        PE_position = 0
        orders.append({
            "symbol": PE_SYMBOL,
            "action": "SELL",
            "price": PE_price,
            "pnl": pnl,
            "time": datetime.now()
        })
        send_telegram(f"🔴 PE SELL {PE_price} | PnL ₹{pnl}")

# ================= RESET =================
def reset_day():
    global token_generated, model_loaded, strategy_active
    global CE_position, PE_position, orders

    token_generated = False
    model_loaded = False
    strategy_active = False
    CE_position = PE_position = 0
    orders = []

    send_telegram("♻ Daily reset completed")

# ================= MAIN =================
send_telegram("🟢 Trend Following Algo Started")

# Try loading token & model immediately at script start
try:
    if os.path.exists(TOKEN_FILE):
        token_generated = True
        fyers = load_fyers_model()
        if fyers:
            token_df = load_option_tokens(fyers)
            model_loaded = True
except Exception as e:
    send_telegram(f"❌ Initial load failed: {e}")

# ================= LOOP =================
while True:
    try:
        now = datetime.now()

        # Daily reset
        if now.date() != current_trading_day:
            current_trading_day = now.date()
            reset_day()

        # Generate token at 9:00 AM
        if not token_generated and time(9,0) <= now.time() <= time(9,5):
            token_generated = generate_daily_token()

        # Load Fyers model after token
        if token_generated and not model_loaded:
            fyers = load_fyers_model()
            if fyers:
                token_df = load_option_tokens(fyers)
                model_loaded = True

        # Strategy window
        if model_loaded and time(9,20) <= now.time() <= time(15,30):
            run_strategy()

        # Market close
        if now.time() > time(15,30) and strategy_active:
            save_live_trades()
            send_telegram("🔴 Market closed – strategy stopped")
            strategy_active = False

        t.sleep(1)

    except Exception as e:
        send_telegram(f"❌ Algo crash\n<code>{e}</code>")
        t.sleep(5)
