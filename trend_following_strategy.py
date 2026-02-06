# ================= HARD FIX FOR FYERS TIMEZONE BUG =================
import zoneinfo
_original_zoneinfo = zoneinfo.ZoneInfo

class SafeZoneInfo(zoneinfo.ZoneInfo):
    def __new__(cls, key):
        if not key:
            key = "UTC"
        if isinstance(key, str) and key.lower() == "asia/kolkata":
            key = "Asia/Kolkata"
        return _original_zoneinfo(key)

zoneinfo.ZoneInfo = SafeZoneInfo

# ================= IMPORTS =================
import os
import sys
import time as t
import requests
import pandas as pd
import numpy as np
import pandas_ta as ta
from fyers_apiv3 import fyersModel
from datetime import datetime, time, timedelta, timezone
from dotenv import load_dotenv

# ================= PATH FIX =================
mydir = os.getcwd()
sys.path.append(mydir)

# ================= LOAD ENV =================
load_dotenv()

# ================= FORCE IST TIMEZONE =================
IST = timezone(timedelta(hours=5, minutes=30))

def ist_now():
    return datetime.now(timezone.utc).astimezone(IST)

def ist_today():
    return ist_now().date()

def ist_time():
    return ist_now().time()

# ================= TELEGRAM =================
TELEGRAM_BOT_TOKEN = os.getenv("TEL_BOTfg_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("fg")

def send_telegram(msg):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg},
            timeout=5
        )
    except Exception as e:
        print("Telegram error:", e)

# ================= CONFIG =================
CLIENT_ID = "98E1TAKD4T-100"
QTY = 30

SL_POINTS = 50
TARGET_POINTS = 250
TRAIL_POINTS = 1

COMMISSION_RATE = 0.0004

TOKEN_FILE = "auth/api_key/access_token.txt"
TOKEN_CSV = "data/Trend_Following/instrument_token.csv"
folder = "data/Trend_Following"
os.makedirs(folder, exist_ok=True)
TRADES_FILE = f"{folder}/live_trades.csv"

# ================= STATE =================
fyers = None
token_df = None

CURR_position = 0
NEXT_position = 0

CURR_enter = NEXT_enter = 0
CURR_SL = NEXT_SL = 0
CURR_TSL = NEXT_TSL = 0

CURR_enter_time = NEXT_enter_time = None
trade_active = False

# ================= UTILS =================
def commission(price, qty):
    return round(price * qty * COMMISSION_RATE, 6)

def calculate_pnl(entry, exit, qty, side):
    if side == "BUY":
        return round((exit - entry) * qty, 6)
    else:
        return round((entry - exit) * qty, 6)

def save_trade(trade):
    df = pd.DataFrame([trade])
    if not os.path.exists(TRADES_FILE):
        df.to_csv(TRADES_FILE, index=False)
    else:
        df.to_csv(TRADES_FILE, mode="a", header=False, index=False)

# ================= FYERS AUTH =================
def load_token():
    send_telegram("🔐 Loading token")
    os.system("python auth/fyers_auth.py")
    return True

def load_model():
    send_telegram("🔌 Loading Fyers model")
    token = open(TOKEN_FILE).read().strip()
    return fyersModel.FyersModel(
        client_id=CLIENT_ID,
        token=token,
        is_async=False,
        log_path=""
    )

# ================= MARKET DATA =================
def get_stock_historical_data(data, fyers): 
    final_data = fyers.history(data=data)
    if not final_data or "candles" not in final_data or not final_data["candles"]:
        raise ValueError("Fyers returned empty candle data")
    df = pd.DataFrame(final_data['candles'])  
    df = df.rename(columns={0: 'Date', 1: 'Open',2: 'High',3: 'Low',4: 'Close',5: 'V'})
    df['Date'] = pd.to_datetime(df['Date'], unit='s')
    df.set_index("Date", inplace=True)
    return df

# ================= INSTRUMENT TOKEN =================
def get_stock_instrument_token(stock_name, fyers):
    tokens=[]
    df = pd.read_csv("https://api.kite.trade/instruments")
    for i in range(len(stock_name)):
        print(stock_name[i]['name']) 
        if stock_name[i]['segment'] == 'NFO-FUT':
            dff = df[(df['name'] == stock_name[i]['name']) & (df['segment'] == stock_name[i]['segment'])]
            dff = dff[dff["expiry"] == sorted(list(dff["expiry"].unique()))[stock_name[i]['expiry']]]
            tokens.append({
                'strategy_name': stock_name[i]['strategy_name'],
                'instrument_token': dff['instrument_token'].item(),
                'tradingsymbol': dff['tradingsymbol'].item()
            })
    return tokens

def load_symbols(fyers):
    send_telegram("📄 Loading symbols")
    if os.path.exists(TOKEN_CSV):
        return pd.read_csv(TOKEN_CSV)
    tickers = [
        {"strategy_name": "HEDGE_CURR", "name": "BANKNIFTY", "segment": "NFO-FUT", "expiry": 0},
        {"strategy_name": "HEDGE_NEXT", "name": "BANKNIFTY", "segment": "NFO-FUT", "expiry": 1}
    ]
    df = pd.DataFrame(get_stock_instrument_token(tickers, fyers))
    df.to_csv(TOKEN_CSV, index=False)
    return df

# ================= VOLATILITY FILTER =================
def volatility_ok(symbol, fyers):
    base = {
        "resolution": "5",
        "date_format": "1",
        "range_from": (ist_today() - timedelta(days=3)).strftime("%Y-%m-%d"),
        "range_to": ist_today().strftime("%Y-%m-%d"),
        "cont_flag": "1",
        "symbol": symbol
    }
    df = get_stock_historical_data(base, fyers)

    atr = ta.atr(df["High"], df["Low"], df["Close"], length=14)
    atr_ema = ta.ema(atr, length=14)

    if pd.isna(atr.iloc[-1]) or pd.isna(atr_ema.iloc[-1]):
        return False

    return atr.iloc[-1] < atr_ema.iloc[-1]

# ================= STRATEGY =================
def run_strategy():
    global CURR_position, CURR_enter, CURR_SL, CURR_TSL
    global CURR_enter_time, trade_active

    CURR_SYMBOL = "NSE:" + token_df.loc[0, "tradingsymbol"]

    # ===== GET PRICE =====
    quotes = fyers.quotes({"symbols": CURR_SYMBOL})["d"]
    CURR_price = quotes[0]["v"]["lp"]

    # ===== GET CANDLE DATA FOR SUPERTREND =====
    hist_data = {
        "symbol": CURR_SYMBOL,
        "resolution": "5",
        "date_format": "1",
        "range_from": (ist_today() - timedelta(days=5)).strftime("%Y-%m-%d"),
        "range_to": ist_today().strftime("%Y-%m-%d"),
        "cont_flag": "1"
    }

    df = get_stock_historical_data(hist_data, fyers)

    if len(df) < 50:
        return

    # ===== CALCULATE SUPERTREND =====
    st = ta.supertrend(
        high=df["High"],
        low=df["Low"],
        close=df["Close"],
        length=21,
        multiplier=1.5
    )

    df["SUPERTREND"] = st["SUPERT_21_1.5"]

    last = df.iloc[-2]
    prev = df.iloc[-3]

    # ===== ENTRY =====
    if not trade_active and ist_time() >= time(9, 30):

        if not volatility_ok(CURR_SYMBOL, fyers):
            return

        # ----- REVERSE LOGIC -----
        if last.Close < last.SUPERTREND and prev.Close > prev.SUPERTREND:
            CURR_position = 1   # BUY
            side = "BUY"

        elif last.Close > last.SUPERTREND and prev.Close < prev.SUPERTREND:
            CURR_position = -1  # SELL
            side = "SELL"

        else:
            return

        CURR_enter = CURR_price

        if CURR_position == 1:
            CURR_SL = CURR_price - SL_POINTS
        else:
            CURR_SL = CURR_price + SL_POINTS

        CURR_TSL = CURR_SL
        CURR_enter_time = ist_now()
        trade_active = True

        send_telegram(f"⚡ {side} BANKNIFTY FUT @ {CURR_price}")

    # ===== TRAILING SL =====
    if trade_active:

        if CURR_position == 1:
            profit = CURR_price - CURR_enter
            if profit > TRAIL_POINTS:
                CURR_TSL = max(CURR_TSL, CURR_price - TRAIL_POINTS)

        if CURR_position == -1:
            profit = CURR_enter - CURR_price
            if profit > TRAIL_POINTS:
                CURR_TSL = min(CURR_TSL, CURR_price + TRAIL_POINTS)

    # ===== EXIT CONDITIONS =====
    exit_reason = None

    if trade_active:

        if CURR_position == 1 and CURR_price > last.SUPERTREND:
            exit_reason = "TSL_EXIT"

        if CURR_position == -1 and CURR_price < last.SUPERTREND:
            exit_reason = "TSL_EXIT"

        if CURR_position == 1 and (CURR_price - CURR_enter) >= TARGET_POINTS:
            exit_reason = "TARGET_EXIT"

        if CURR_position == -1 and (CURR_enter - CURR_price) >= TARGET_POINTS:
            exit_reason = "TARGET_EXIT"

        if ist_time() >= time(15, 15):
            exit_reason = "TIME_EXIT"

    if trade_active and exit_reason:

        side = "BUY" if CURR_position == 1 else "SELL"

        net = calculate_pnl(
            CURR_enter,
            CURR_price,
            QTY,
            side
        ) - commission(CURR_price, QTY)

        save_trade({
            "entry_time": CURR_enter_time.isoformat(),
            "exit_time": ist_now().isoformat(),
            "symbol": CURR_SYMBOL,
            "side": side,
            "entry_price": CURR_enter,
            "exit_price": CURR_price,
            "qty": QTY,
            "net_pnl": net
        })

        send_telegram(
            f"🔁 EXIT BANKNIFTY FUT | Net ₹{round(net,2)} | Reason: {exit_reason}"
        )

        CURR_position = 0
        trade_active = False


# ================= MAIN LOOP =================
send_telegram("🚀 Simple Hedge Algo with Volatility Filter Started")
current_trading_date = ist_today()

token_loaded = model_loaded = symbols_loaded = False

while True:
    try:
        market_open = time(9, 15)
        market_close = time(15, 30)

        if ist_time() > market_close and current_trading_date == ist_today():
            token_loaded = model_loaded = symbols_loaded = False
            CURR_position = NEXT_position = 0
            trade_active = False
            current_trading_date = ist_today() + timedelta(days=1)
            send_telegram("♻ End of day reset done")

        if market_open <= ist_time() < market_close:
            if not token_loaded:
                token_loaded = load_token()
            if not model_loaded:
                fyers = load_model()
                model_loaded = True
            if not symbols_loaded:
                token_df = load_symbols(fyers)
                symbols_loaded = True

        if time(9, 30) <= ist_time() <= market_close and symbols_loaded:
            run_strategy()

        t.sleep(2)

    except Exception as e:
        send_telegram(f"❌ Algo error: {e}")
        t.sleep(5)
