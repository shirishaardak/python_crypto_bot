# ================= HARD FIX FOR FYERS TIMEZONE BUG =================
# Must be FIRST lines in file — before any imports
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
from scipy.signal import argrelextrema
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

# ================= CONFIG =================
CLIENT_ID = "98E1TAKD4T-100"
QTY = 30
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

    # Convert from epoch → naive datetime (no timezone anywhere)
    df['Date'] = pd.to_datetime(df['Date'], unit='s')
    df.set_index("Date", inplace=True)
    return df

# ================= INSTRUMENT TOKEN =================
def get_stock_instrument_token(stock_name, fyers):
    tokens=[]
    for i in range(len(stock_name)):
        print(stock_name[i]['name']) 
        df = pd.read_csv("https://api.kite.trade/instruments")

        if stock_name[i]['segment'] in ['MCX-FUT', 'NFO-FUT']:
            df = df[(df['name'] == stock_name[i]['name']) & (df['segment'] == stock_name[i]['segment'])]
            df = df[df["expiry"] == sorted(list(df["expiry"].unique()))[stock_name[i]['expiry']]]
            df_instrument_token= df['instrument_token'].item()
            df_instrument_name= df['tradingsymbol'].item()
            tokens.append({'strategy_name': stock_name[i]['strategy_name'], 'instrument_token': df_instrument_token, 'tradingsymbol': df_instrument_name})

        elif stock_name[i]['segment'] in ['MCX-OPT', 'NFO-OPT']:
            spot_price = fyers.quotes(data={"symbols":"NSE:NIFTYBANK-INDEX"})['d'][0]['v']['lp']
            df = df[(df['name'] == stock_name[i]['name']) & (df['segment'] == stock_name[i]['segment'])]
            df = df[df["expiry"] == sorted(list(df["expiry"].unique()))[stock_name[i]['expiry']]]

            atm_strike = 100 * round(spot_price / 100)
            atm_strike_CE = atm_strike - 100 * stock_name[i]['offset']
            atm_strike_PE = atm_strike + 100 * stock_name[i]['offset']

            option_strike_CE = df[(df['strike'] == atm_strike_CE) & (df['instrument_type'] == 'CE')]['instrument_token'].item()
            option_strike_PE = df[(df['strike'] == atm_strike_PE) & (df['instrument_type'] == 'PE')]['instrument_token'].item()

            option_strike_name_CE = df[(df['strike'] == atm_strike_CE) & (df['instrument_type'] == 'CE')]['tradingsymbol'].item()
            option_strike_name_PE = df[(df['strike'] == atm_strike_PE) & (df['instrument_type'] == 'PE')]['tradingsymbol'].item()

            tokens.append({'strategy_name': stock_name[i]['strategy_name'], 'instrument_token': option_strike_CE, 'tradingsymbol': option_strike_name_CE})
            tokens.append({'strategy_name': stock_name[i]['strategy_name'], 'instrument_token': option_strike_PE, 'tradingsymbol': option_strike_name_PE})

    return tokens

def load_symbols(fyers):
    send_telegram("📄 Loading symbols")
    if os.path.exists(TOKEN_CSV):
        return pd.read_csv(TOKEN_CSV)

    tickers = [{
        "strategy_name": "Trend_Following",
        "name": "BANKNIFTY",
        "segment-name": "NSE:NIFTY BANK",
        "segment": "NFO-OPT",
        "expiry": 0,
        "offset": 1
    }]

    df = pd.DataFrame(get_stock_instrument_token(tickers, fyers))
    df.to_csv(TOKEN_CSV, index=False)
    return df

# ================= STRATEGY LOGIC =================
def high_low_trend(data, fyers):

    df = pd.DataFrame(get_stock_historical_data(data, fyers))
    df = df.reset_index(drop=True)

    # ---- Heikin Ashi ----
    ha = pd.DataFrame(index=df.index)
    ha["HA_close"] = (df["Open"] + df["High"] + df["Low"] + df["Close"]) / 4
    ha["HA_open"] = np.nan
    ha.loc[0, "HA_open"] = (df.loc[0, "Open"] + df.loc[0, "Close"]) / 2

    for i in range(1, len(df)):
        ha.loc[i, "HA_open"] = 0.5 * (ha.loc[i - 1, "HA_open"] + ha.loc[i - 1, "HA_close"])

    ha["HA_high"] = pd.concat([df["High"], ha["HA_open"], ha["HA_close"]], axis=1).max(axis=1)
    ha["HA_low"] = pd.concat([df["Low"], ha["HA_open"], ha["HA_close"]], axis=1).min(axis=1)

    # ---- Smooth ----
    ha["high_smooth"] = ta.ema(ha["HA_high"], length=5)
    ha["low_smooth"] = ta.ema(ha["HA_low"], length=5)

    # ---- Swing Points ----
    max_idx = argrelextrema(ha["high_smooth"].values, np.greater_equal, order=21)[0]
    min_idx = argrelextrema(ha["low_smooth"].values, np.less_equal, order=21)[0]

    ha["max_high"] = np.nan
    ha["max_low"] = np.nan
    ha.loc[max_idx, "max_high"] = ha.loc[max_idx, "HA_high"]
    ha.loc[min_idx, "max_low"] = ha.loc[min_idx, "HA_low"]
    ha["max_high"] = ha["max_high"].ffill()
    ha["max_low"] = ha["max_low"].ffill()

    # ---- Trendline ----
    ha["trendline"] = np.nan
    trendline = ha.loc[0, "HA_close"]
    ha.loc[0, "trendline"] = trendline

    for i in range(1, len(ha)):
        if ha.loc[i, "HA_high"] == ha.loc[i, "max_high"]:
            trendline = ha.loc[i, "HA_low"]
        elif ha.loc[i, "HA_low"] == ha.loc[i, "max_low"]:
            trendline = ha.loc[i, "HA_high"]
        ha.loc[i, "trendline"] = trendline

    # ---- ATR ----
    ha["ATR"] = ta.atr(high=ha["HA_high"], low=ha["HA_low"], close=ha["HA_close"], length=14)
    ha["ATR_EMA"] = ta.ema(ha["ATR"], length=14)
    ha["atr_condition"] = ha["ATR"] > ha["ATR_EMA"]

    # ---- Trade Signals ----
    ha["trade_single"] = 0
    for i in range(1, len(ha)):
        if not ha.loc[i, "atr_condition"]:
            continue

        # BUY
        if (
            ha.loc[i, "HA_close"] > ha.loc[i, "trendline"] and
            ha.loc[i, "HA_close"] > ha.loc[i - 1, "HA_open"] and
            ha.loc[i, "HA_close"] > ha.loc[i - 1, "HA_close"]
        ):
            ha.loc[i, "trade_single"] = 1

        # SELL
        elif (
            ha.loc[i, "HA_close"] < ha.loc[i, "trendline"] and
            ha.loc[i, "HA_close"] < ha.loc[i - 1, "HA_open"] and
            ha.loc[i, "HA_close"] < ha.loc[i - 1, "HA_close"]
        ):
            ha.loc[i, "trade_single"] = -1

    return ha

# ================= STRATEGY EXECUTION =================
def run_strategy():
    global CE_position, PE_position, CE_enter, PE_enter, CE_SL, PE_SL
    global CE_enter_time, PE_enter_time

    CE_SYMBOL = "NSE:" + token_df.loc[0, "tradingsymbol"]
    PE_SYMBOL = "NSE:" + token_df.loc[1, "tradingsymbol"]

    # IMPORTANT: Only pass DATE STRINGS to Fyers — no datetimes!
    start = (ist_today() - timedelta(days=5)).strftime("%Y-%m-%d")
    end = ist_today().strftime("%Y-%m-%d")

    base = {
        "resolution": "5",
        "date_format": "1",
        "range_from": start,
        "range_to": end,
        "cont_flag": "1"
    }

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

    # ===== CE =====
    if CE_position == 0 and last_CE["trade_single"] == 1:
        CE_position = 1
        CE_enter = CE_price
        CE_enter_time = ist_now()
        CE_SL = CE_price - SL_POINTS
        send_telegram(f"🟢 CE BUY @ {CE_price}")

    elif CE_position == 1 and (CE_price <= last_CE["trendline"] or ist_time() >= time(15, 15)):
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
        send_telegram(f"🔴 CE EXIT @ {CE_price} | Net ₹{round(net,2)}")

    # ===== PE =====
    if PE_position == 0 and last_PE["trade_single"] == 1:
        PE_position = 1
        PE_enter = PE_price
        PE_enter_time = ist_now()
        PE_SL = PE_price - SL_POINTS
        send_telegram(f"🟢 PE BUY @ {PE_price}")

    elif PE_position == 1 and (PE_price <= last_PE["trendline"] or ist_time() >= time(15, 15)):
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
        send_telegram(f"🔴 PE EXIT @ {PE_price} | Net ₹{round(net,2)}")

# ================= MAIN LOOP =================
send_telegram("🚀 Trend Following Algo Started")

current_trading_date = ist_today()

while True:
    try:
        # ===== MARKET WINDOW (IST ONLY) =====
        market_open = time(9, 15)
        market_close = time(15, 30)

        # ===== DAILY RESET (IST) =====
        if ist_time() > market_close and current_trading_date == ist_today():
            token_loaded = model_loaded = symbols_loaded = False
            CE_position = PE_position = 0
            current_trading_date = ist_today() + timedelta(days=1)
            send_telegram("♻ End of day reset done")

        # ===== LOAD PHASE =====
        if market_open <= ist_time() < market_close:
            if not token_loaded:
                token_loaded = load_token()
            if not model_loaded:
                fyers = load_model()
                model_loaded = True
            if not symbols_loaded:
                token_df = load_symbols(fyers)
                symbols_loaded = True

        # ===== STRATEGY =====
        if time(9, 30) <= ist_time() <= market_close and symbols_loaded:
            run_strategy()

        t.sleep(2)

    except Exception as e:
        send_telegram(f"❌ Algo error: {e}")
        t.sleep(5)
