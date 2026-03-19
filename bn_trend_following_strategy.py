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
import os, sys
import time as t
import pandas as pd
import numpy as np
import pandas_ta as ta
from fyers_apiv3 import fyersModel
from datetime import datetime, time, timedelta, timezone, date
from dotenv import load_dotenv
import requests

sys.path.append(os.getcwd())
load_dotenv()

# ================= IST TIME =================
IST = timezone(timedelta(hours=5, minutes=30))

def ist_now():
    return datetime.now(timezone.utc).astimezone(IST)

def ist_today():
    return ist_now().date()

def ist_time():
    return ist_now().time()


# ================= CONFIG =================
CLIENT_ID = "98E1TAKD4T-100"
QTY = 30
COMMISSION_RATE = 0.0004
SPOT_SYMBOL = "NSE:NIFTYBANK-INDEX"

ADX_PERIOD = 14
ADX_THRESHOLD = 20

folder = "data/Trend_Following"
os.makedirs(folder, exist_ok=True)

TRADES_FILE = f"{folder}/live_trades.csv"
TOKEN_FILE = "auth/api_key/access_token.txt"


# ================= TELEGRAM =================
TELEGRAM_BOT_TOKEN = os.getenv("TEL_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TEL_CHAT_ID")

def send_telegram(msg):
    try:
        if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            payload = {"chat_id": TELEGRAM_CHAT_ID, "text": msg}
            requests.post(url, json=payload, timeout=2)
    except:
        pass


# ================= GLOBAL STATE =================
fyers = None
last_login_date = None

position_type = None
entry_price = 0
entry_time = None
symbol = None

ce_symbol_cached = None
pe_symbol_cached = None
symbol_cache_date = None

last_candle_time = None
data_cache = {}
ltp_cache = {}


# ================= FYERS INIT =================
def init_fyers(force=False):

    global fyers, last_login_date

    today = ist_today()

    if not force and last_login_date == today and fyers is not None:
        return

    send_telegram("🔄 Initializing Fyers...")

    for _ in range(5):
        try:
            os.system("python auth/fyers_auth.py")

            if os.path.exists(TOKEN_FILE):
                token = open(TOKEN_FILE).read().strip()

                fyers_model = fyersModel.FyersModel(
                    client_id=CLIENT_ID,
                    token=token,
                    is_async=False
                )

                test = fyers_model.quotes({"symbols": SPOT_SYMBOL})

                if "d" in test:
                    fyers = fyers_model
                    last_login_date = today
                    send_telegram("✅ Fyers Connected")
                    return

        except Exception as e:
            send_telegram(f"Init Error: {e}")

        t.sleep(3)

    send_telegram("❌ Fyers Login Failed")


def is_fyers_alive():
    try:
        test = fyers.quotes({"symbols": SPOT_SYMBOL})
        return "d" in test
    except:
        return False


# ================= SAFE LTP =================
def get_ltp(sym):

    global fyers

    if sym in ltp_cache:
        return ltp_cache[sym]

    try:
        data = fyers.quotes({"symbols": sym})
        price = data["d"][0]["v"]["lp"]
        ltp_cache[sym] = price
        return price

    except Exception as e:
        send_telegram(f"LTP Error: {e}")

        init_fyers(force=True)

        try:
            data = fyers.quotes({"symbols": sym})
            return data["d"][0]["v"]["lp"]
        except:
            return None


# ================= CANDLE ENGINE =================
def is_new_candle():

    global last_candle_time, data_cache, ltp_cache

    now = ist_now().replace(second=0, microsecond=0)

    if now.minute % 5 == 0:
        if now != last_candle_time:
            last_candle_time = now
            data_cache = {}
            ltp_cache = {}
            return True

    return False


# ================= HIST DATA =================
def get_history(symbol):

    global fyers

    if symbol in data_cache:
        return data_cache[symbol]

    data = {
        "symbol": symbol,
        "resolution": "5",
        "date_format": "1",
        "range_from": (ist_today() - timedelta(days=5)).strftime("%Y-%m-%d"),
        "range_to": ist_today().strftime("%Y-%m-%d"),
        "cont_flag": "1"
    }

    try:
        res = fyers.history(data=data)

        if "candles" not in res:
            raise Exception("No candles")

        df = pd.DataFrame(res["candles"])
        df = df.rename(columns={0:'Date',1:'Open',2:'High',3:'Low',4:'Close',5:'V'})
        df['Date'] = pd.to_datetime(df['Date'], unit='s')
        df.set_index("Date", inplace=True)

        data_cache[symbol] = df
        return df

    except Exception as e:
        send_telegram(f"History Error: {e}")
        init_fyers(force=True)
        return None


# ================= INDICATORS =================
def calculate_indicators(df):

    ha = ta.ha(df["Open"], df["High"], df["Low"], df["Close"])
    df["HA_Close"] = ha["HA_close"]
    df["HA_Open"] = ha["HA_open"]

    st = ta.supertrend(df["High"], df["Low"], df["Close"], length=10, multiplier=3)
    st_dir_col = [c for c in st.columns if "SUPERTd" in c][0]
    df["ST_dir"] = st[st_dir_col]

    adx = ta.adx(df["High"], df["Low"], df["Close"], length=ADX_PERIOD)
    df["ADX"] = adx[f"ADX_{ADX_PERIOD}"]

    return df


# ================= EXPIRY =================
def get_last_thursday(year, month):
    last_day = date(year, month, 1) + timedelta(days=32)
    last_day = last_day.replace(day=1) - timedelta(days=1)

    while last_day.weekday() != 3:
        last_day -= timedelta(days=1)

    return last_day


def get_current_monthly_expiry():
    today = ist_today()
    expiry = get_last_thursday(today.year, today.month)

    if today > expiry:
        month = today.month + 1
        year = today.year

        if month == 13:
            month = 1
            year += 1

        expiry = get_last_thursday(year, month)

    return expiry


# ================= ATM OPTION =================
def get_atm_option(option_type):

    spot = get_ltp(SPOT_SYMBOL)

    if spot is None:
        return None

    strike = int(round(spot/100)*100)
    expiry = get_current_monthly_expiry()
    expiry_str = expiry.strftime("%y%b").upper()

    return f"NSE:BANKNIFTY{expiry_str}{strike}{option_type}"


# ================= TRADE SAVE =================
def save_trade(entry_time, exit_time, symbol, side, entry_price, exit_price, qty):

    gross = (exit_price - entry_price) * qty
    brokerage = (entry_price + exit_price) * qty * COMMISSION_RATE
    net = gross - brokerage

    df = pd.DataFrame([{
        "entry_time": entry_time,
        "exit_time": exit_time,
        "symbol": symbol,
        "side": side,
        "entry_price": entry_price,
        "exit_price": exit_price,
        "qty": qty,
        "net_pnl": round(net,2)
    }])

    df.to_csv(TRADES_FILE, mode="a", header=not os.path.exists(TRADES_FILE), index=False)


# ================= EXIT =================
def exit_trade(reason):

    global position_type

    price = get_ltp(symbol)
    if price is None:
        return

    exit_time = ist_now()
    save_trade(entry_time, exit_time, symbol, position_type, entry_price, price, QTY)

    pnl = (price - entry_price) * QTY
    send_telegram(f"EXIT {symbol} {reason} PnL {round(pnl,2)}")

    position_type = None


# ================= STRATEGY =================
def run_strategy():

    global position_type, entry_price, entry_time, symbol
    global ce_symbol_cached, pe_symbol_cached, symbol_cache_date

    if symbol_cache_date != ist_today():
        ce_symbol_cached = get_atm_option("CE")
        pe_symbol_cached = get_atm_option("PE")
        symbol_cache_date = ist_today()

    ce_symbol = ce_symbol_cached
    pe_symbol = pe_symbol_cached

    df_index = get_history(SPOT_SYMBOL)
    df_ce = get_history(ce_symbol)
    df_pe = get_history(pe_symbol)

    if df_index is None or df_ce is None or df_pe is None:
        return

    df_index = calculate_indicators(df_index)
    df_ce = calculate_indicators(df_ce)
    df_pe = calculate_indicators(df_pe)

    index_last = df_index.iloc[-2]
    ce_last = df_ce.iloc[-2]
    ce_prv = df_ce.iloc[-3]
    pe_last = df_pe.iloc[-2]
    pe_prv = df_pe.iloc[-3]

    ce_momentum = (ce_last.HA_Close > ce_prv.HA_Close and ce_last.HA_Close > ce_prv.HA_Open)
    pe_momentum = (pe_last.HA_Close < pe_prv.HA_Close and pe_last.HA_Close < pe_prv.HA_Open)

    if position_type is None and time(9,30) <= ist_time() <= time(15,15):

        if index_last.ST_dir == 1 and ce_last.ST_dir == 1 and ce_momentum and ce_last.ADX > ADX_THRESHOLD:
            symbol = ce_symbol
            position_type = "CE"

        elif index_last.ST_dir == -1 and pe_last.ST_dir == 1 and pe_momentum and pe_last.ADX > ADX_THRESHOLD:
            symbol = pe_symbol
            position_type = "PE"

        else:
            return

        price = get_ltp(symbol)
        if price is None:
            return

        entry_price = price
        entry_time = ist_now()

        send_telegram(f"BUY {symbol} {price}")

    if position_type:

        if position_type == "CE" and ce_last.ST_dir == -1:
            exit_trade("Supertrend Flip")

        if position_type == "PE" and pe_last.ST_dir == -1:
            exit_trade("Supertrend Flip")

        if ist_time() >= time(15,15):
            exit_trade("Time Exit")


# ================= MAIN LOOP =================
send_telegram("🚀 BankNifty Algo Started")

while True:

    try:
        now = ist_time()

        # ✅ DAILY LOGIN WINDOW
        if time(8,55) <= now <= time(9,5):
            init_fyers()

        # ✅ AUTO RECONNECT
        if fyers is None or not is_fyers_alive():
            send_telegram("⚠️ Reconnecting Fyers...")
            init_fyers(force=True)

        # ✅ RUN STRATEGY
        if time(9,30) <= now <= time(15,30):
            if is_new_candle():
                run_strategy()

        t.sleep(0.5)

    except Exception as e:
        send_telegram(f"Algo Error: {e}")
        t.sleep(5)