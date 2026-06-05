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
CLIENT_ID="98E1TAKD4T-100"
QTY=30
COMMISSION_RATE=0.0004
SPOT_SYMBOL="NSE:NIFTYBANK-INDEX"

ADX_PERIOD=14
ADX_THRESHOLD=20

# --- risk config ---
HARD_SL_POINTS=60          # entry - 60 (was 50)
TRAIL_TRIGGER=60           # once price >= entry + 60, start trailing (was 25)
TRAIL_GAP=40               # keep SL this far below the running high (was 25)

# --- over-trading control ---
RE_ENTRY_COOLDOWN=600      # seconds to wait after any exit (was 300)
SAME_STRIKE_BLOCK_SEC=1800 # don't re-enter the SAME strike within this window

# --- perf config ---
INDICATOR_REFRESH_SEC=60   # recompute heavy indicators at most this often

folder="data/Trend_Following"
os.makedirs(folder,exist_ok=True)
TRADES_FILE=f"{folder}/live_trades.csv"
TOKEN_FILE="auth/api_key/access_token.txt"

# ================= TELEGRAM =================
TELEGRAM_BOT_TOKEN=os.getenv("testmyaglostrategy_bot")
TELEGRAM_CHAT_ID=os.getenv("TELEGRAM_CHAT_ID")

_tg_session = requests.Session()

def send_telegram(msg):
    try:
        if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
            url=f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            payload={"chat_id":TELEGRAM_CHAT_ID,"text":msg}
            _tg_session.post(url,json=payload,timeout=5)
    except:
        pass

# ================= GLOBAL STATE =================
fyers=None
position_type=None
entry_price=0
entry_time=None
symbol=None
last_reset_date=None
token_load_date=None
model_load_date=None
last_exit_time=None
stop_loss=None
trail_level=None
running_high=None      # highest price seen since entry (for trailing)

# track the last strike we exited + when, to block instant re-entry on it
last_exit_symbol=None
last_exit_symbol_time=None

NSE_HOLIDAYS=set()
holiday_load_date=None

# expiry cache (perf) - recomputed once per day
_cached_expiry=None
_cached_expiry_date=None

# indicator cache (perf): {symbol: (timestamp, computed_df)}
_indicator_cache={}

# ================= SAFE PRICE =================
def get_last_price(symbol):
    try:
        data = fyers.quotes({"symbols": symbol})
        if not data or "d" not in data or not data["d"]:
            return None
        return data["d"][0].get("v", {}).get("lp")
    except:
        return None

# ================= UTILS =================
def commission(price,qty):
    return round(price*qty*COMMISSION_RATE,6)

def calculate_pnl(entry,exit,qty):
    return round((exit-entry)*qty,6)

def save_trade(data):
    df=pd.DataFrame([data])
    if not os.path.exists(TRADES_FILE):
        df.to_csv(TRADES_FILE,index=False)
    else:
        df.to_csv(TRADES_FILE,mode="a",header=False,index=False)

# ================= HOLIDAY =================
def fetch_nse_holidays():
    global NSE_HOLIDAYS
    try:
        url="https://www.nseindia.com/api/holiday-master?type=trading"
        headers={"User-Agent":"Mozilla/5.0","Accept":"application/json"}
        session=requests.Session()
        session.get("https://www.nseindia.com",headers=headers,timeout=5)
        response=session.get(url,headers=headers,timeout=5)
        data=response.json()

        NSE_HOLIDAYS.clear()
        for item in data["CM"]:
            h_date=datetime.strptime(item["tradingDate"],"%d-%b-%Y").date()
            NSE_HOLIDAYS.add(h_date)

        send_telegram("✅ NSE Holidays Loaded")
    except Exception as e:
        send_telegram(f"⚠ Holiday Fetch Failed {e}")

def is_market_open():
    today=ist_today()
    if today.weekday()>=5:
        return False
    if today in NSE_HOLIDAYS:
        return False
    return True

# ================= EXPIRY =================
# NSE moved BANKNIFTY monthly expiry from the last THURSDAY to the last
# TUESDAY of the month, effective Sep 2025 (circular 111/2025). Weekly
# BankNifty options were discontinued (Nov 2024) — only the monthly series
# trades. If the last Tuesday is an exchange holiday, expiry shifts to the
# previous trading day.
def get_last_tuesday(year,month):
    last_day=date(year,month,1)+timedelta(days=32)
    last_day=last_day.replace(day=1)-timedelta(days=1)
    while last_day.weekday()!=1:   # Tuesday == 1
        last_day-=timedelta(days=1)
    return last_day

def _shift_for_holiday(d):
    # move back to the previous weekday/non-holiday if expiry lands on one
    guard=0
    while (d.weekday()>=5 or d in NSE_HOLIDAYS) and guard<10:
        d-=timedelta(days=1)
        guard+=1
    return d

def get_current_monthly_expiry():
    global _cached_expiry, _cached_expiry_date
    today=ist_today()
    if _cached_expiry is not None and _cached_expiry_date==today:
        return _cached_expiry

    now=ist_time()
    expiry=_shift_for_holiday(get_last_tuesday(today.year,today.month))

    # once we're past this month's (possibly shifted) expiry, roll to next month
    if today>expiry or (today==expiry and now>=time(15,30)):
        next_month=today.month+1
        next_year=today.year
        if next_month==13:
            next_month=1
            next_year+=1
        expiry=_shift_for_holiday(get_last_tuesday(next_year,next_month))

    _cached_expiry=expiry
    _cached_expiry_date=today
    return expiry

# ================= ATM OPTION =================
def get_atm_option(option_type, spot=None):
    if not is_market_open():
        return None

    if spot is None:
        spot=get_last_price(SPOT_SYMBOL)
    if spot is None:
        return None

    strike=int(round(spot/100)*100)
    expiry=get_current_monthly_expiry()
    expiry_str=expiry.strftime("%y%b").upper()

    return f"NSE:BANKNIFTY{expiry_str}{strike}{option_type}"

# ================= DATA =================
def get_stock_historical_data(data):
    try:
        final_data = fyers.history(data=data)

        if not isinstance(final_data, dict):
            return pd.DataFrame()

        candles = final_data.get("candles")
        if not candles:
            return pd.DataFrame()

        df = pd.DataFrame(candles)
        if df.shape[1] < 6:
            return pd.DataFrame()

        df = df.iloc[:, :6]
        df.columns=['Date','Open','High','Low','Close','Volume']

        df['Date']=pd.to_datetime(df['Date'],unit='s',errors='coerce')
        df.dropna(inplace=True)

        if df.empty:
            return pd.DataFrame()

        df.set_index("Date",inplace=True)
        return df

    except Exception as e:
        send_telegram(f"⚠ Data Fetch Error {e}")
        return pd.DataFrame()

# ================= TRENDLINE =================
def calculate_trendline(df):
    try:
        if df is None or df.empty or len(df) < 30:
            return pd.DataFrame()

        ha = ta.ha(df["Open"],df["High"],df["Low"],df["Close"])
        if ha is None or ha.empty:
            return pd.DataFrame()

        data=df.copy()
        data["HA_Close"]=ha["HA_close"]
        data["HA_Open"]=ha["HA_open"]
        data["HA_High"]=ha["HA_high"]
        data["HA_Low"]=ha["HA_low"]

        st = ta.supertrend(
            high=data["HA_High"],
            low=data["HA_Low"],
            close=data["HA_Close"],
            length=10,
            multiplier=3
        )

        if st is None or st.empty:
            return pd.DataFrame()

        st_col = [c for c in st.columns if "SUPERT" in c]
        if not st_col:
            return pd.DataFrame()

        data["ST"] = st[st_col[0]].ffill()

        adx = ta.adx(df["High"],df["Low"],df["Close"],length=ADX_PERIOD)
        if adx is None or "ADX_14" not in adx.columns:
            return pd.DataFrame()

        data["ADX"] = adx["ADX_14"]

        data.dropna(inplace=True)
        return data

    except Exception as e:
        send_telegram(f"⚠ Trendline Error {e}")
        return pd.DataFrame()

# ================= CACHED INDICATOR FETCH (perf) =================
def get_indicators(sym, hist_template):
    """Fetch history + compute indicators, but reuse a cached result if it
    is younger than INDICATOR_REFRESH_SEC. 5-min candles only change every
    300s, so recomputing every loop is wasted work."""
    now=ist_now()
    cached=_indicator_cache.get(sym)
    if cached is not None:
        ts, cdf = cached
        if (now-ts).total_seconds() < INDICATOR_REFRESH_SEC:
            return cdf

    h=hist_template.copy()
    h["symbol"]=sym
    raw=get_stock_historical_data(h)
    if raw.empty:
        return pd.DataFrame()
    computed=calculate_trendline(raw)
    if not computed.empty:
        _indicator_cache[sym]=(now, computed)
    return computed

# ================= EXIT =================
def exit_trade(reason):
    global position_type,last_exit_time,stop_loss,trail_level,running_high
    global entry_price, entry_time, symbol
    global last_exit_symbol, last_exit_symbol_time

    price = get_last_price(symbol)
    if price is None:
        return

    net=calculate_pnl(entry_price,price,QTY)-commission(price,QTY)

    save_trade({
        "entry_time":entry_time.isoformat(),
        "exit_time":ist_now().isoformat(),
        "symbol":symbol,
        "side":position_type,
        "entry_price":entry_price,
        "exit_price":price,
        "qty":QTY,
        "net_pnl":net
    })

    send_telegram(f"🔁 EXIT {symbol} | {reason} | PnL ₹{round(net,2)}")

    # remember which strike we just exited so we don't jump right back in
    last_exit_symbol=symbol
    last_exit_symbol_time=ist_now()

    position_type=None
    stop_loss=None
    trail_level=None
    running_high=None
    last_exit_time=ist_now()

# ================= SL / TRAILING (perf: price-only, no history) =================
def update_trailing(price):
    """Move the stop up as price makes new highs once it clears the trigger.
    Pure arithmetic on the live price; no API/indicator calls."""
    global stop_loss, running_high
    if price is None or stop_loss is None:
        return
    if running_high is None or price > running_high:
        running_high = price
        # once price has run past entry+TRAIL_TRIGGER, trail the stop
        if running_high >= entry_price + TRAIL_TRIGGER:
            new_sl = running_high - TRAIL_GAP
            if new_sl > stop_loss:
                stop_loss = new_sl

def check_hard_exits(price):
    """Returns True if an SL/trail exit fired. Price-only, runs every poll."""
    if price is None or stop_loss is None:
        return False
    if price <= stop_loss:
        exit_trade("Stop Loss / Trail")
        return True
    return False

# ================= SAME-STRIKE RE-ENTRY GUARD =================
def blocked_recent_strike(sym):
    """True if we exited this exact strike inside SAME_STRIKE_BLOCK_SEC.
    This is what kills the back-to-back churn on the same option."""
    if last_exit_symbol is None or last_exit_symbol_time is None:
        return False
    if sym != last_exit_symbol:
        return False
    return (ist_now()-last_exit_symbol_time).total_seconds() < SAME_STRIKE_BLOCK_SEC

# ================= STRATEGY =================
def run_strategy():
    global position_type, entry_price, entry_time
    global symbol, stop_loss, trail_level, running_high

    if last_exit_time and (ist_now()-last_exit_time).total_seconds()<RE_ENTRY_COOLDOWN:
        return

    # ---------- HISTORY TEMPLATE ----------
    hist_template={
        "resolution":"5",
        "date_format":"1",
        "range_from":(ist_today()-timedelta(days=5)).strftime("%Y-%m-%d"),
        "range_to":ist_today().strftime("%Y-%m-%d"),
        "cont_flag":"1"
    }

    # ---------- SPOT (one quote, reused for both legs) ----------
    spot=get_last_price(SPOT_SYMBOL)
    if spot is None:
        return

    ce_symbol=get_atm_option("CE", spot=spot)
    pe_symbol=get_atm_option("PE", spot=spot)

    if ce_symbol is None or pe_symbol is None:
        return

    # ================= EXIT BRANCH (checked first, price-driven) =================
    if position_type:

        price = get_last_price(symbol)

        # 1) hard SL / trailing — pure price, most responsive
        update_trailing(price)
        if check_hard_exits(price):
            return

        # 2) force time exit (no price dependency)
        if ist_time() >= time(15,15):
            exit_trade("Time Exit")
            return

        if price is None:
            return

        # 3) supertrend break on the leg we actually hold (cached indicators)
        if position_type=="CE":
            df_ce=get_indicators(ce_symbol, hist_template)
            if df_ce.empty or len(df_ce)<5:
                return
            ce_last=df_ce.iloc[-2]
            if ce_last.HA_Close < ce_last.ST:
                exit_trade("CE Supertrend Break")
                return

        if position_type=="PE":
            df_pe=get_indicators(pe_symbol, hist_template)
            if df_pe.empty or len(df_pe)<5:
                return
            pe_last=df_pe.iloc[-2]
            if pe_last.HA_Close < pe_last.ST:
                exit_trade("PE Supertrend Break")
                return
        return

    # ================= ENTRY BRANCH =================
    # Index indicators always needed for entry
    df_index=get_indicators(SPOT_SYMBOL, hist_template)
    if df_index.empty or len(df_index)<5:
        return

    index_last=df_index.iloc[-2]
    index_perv=df_index.iloc[-3]   # previous index candle for confirmation

    if position_type is None and time(9,30)<=ist_time()<=time(15,15):

        if index_last.ADX < ADX_THRESHOLD:
            return

        # ---------- INDEX CANDLE CONFIRMATION ----------
        # Bullish (for CE side): close > prev close AND close > prev open
        index_bullish = (index_last.HA_Close > index_perv.HA_Close
                         and index_last.HA_Close > index_perv.HA_Open)

        # Bearish (for PE side): close < prev close AND close < prev low
        index_bearish = (index_last.HA_Close < index_perv.HA_Close
                         and index_last.HA_Close < index_perv.HA_Low)

        # ---------- CE ENTRY ----------
        # Index above supertrend + bullish index candle, then confirm the CE
        # option itself is breaking out upward.
        if index_last.HA_Close > index_last.ST and index_bullish:
            if blocked_recent_strike(ce_symbol):
                return
            df_ce=get_indicators(ce_symbol, hist_template)
            if df_ce.empty or len(df_ce)<5:
                return
            ce_last=df_ce.iloc[-2]
            ce_perv=df_ce.iloc[-3]

            if (ce_last.HA_Close > ce_last.ST
                    and ce_last.HA_Close > ce_perv.HA_Close
                    and ce_last.HA_Close > ce_perv.HA_Open):
                symbol=ce_symbol
                position_type="CE"
            else:
                return

        # ---------- PE ENTRY ----------
        # Index below supertrend + bearish index candle, then confirm the PE
        # option itself is breaking out upward (PE price rises as index falls).
        elif index_last.HA_Close < index_last.ST and index_bearish:
            if blocked_recent_strike(pe_symbol):
                return
            df_pe=get_indicators(pe_symbol, hist_template)
            if df_pe.empty or len(df_pe)<5:
                return
            pe_last=df_pe.iloc[-2]
            pe_perv=df_pe.iloc[-3]

            if (pe_last.HA_Close > pe_last.ST
                    and pe_last.HA_Close > pe_perv.HA_Close
                    and pe_last.HA_Close > pe_perv.HA_Open):
                symbol=pe_symbol
                position_type="PE"
            else:
                return

        else:
            return

        price=get_last_price(symbol)
        if price is None:
            position_type=None
            return

        entry_price=price
        entry_time=ist_now()

        # initialise risk levels
        stop_loss=entry_price-HARD_SL_POINTS
        trail_level=entry_price+TRAIL_TRIGGER
        running_high=entry_price

        send_telegram(f"⚡ BUY {symbol} @ {price} | SL {stop_loss} | Trail>{trail_level}")
        return

# ================= AUTH =================
def load_token():
    os.system("python auth/fyers_auth.py")
    return True

def load_model():
    try:
        with open(TOKEN_FILE) as f:
            token=f.read().strip()
    except FileNotFoundError:
        send_telegram(f"⚠ Token file not found: {TOKEN_FILE}")
        return None
    if not token:
        send_telegram("⚠ Token file is empty")
        return None

    model=fyersModel.FyersModel(
        client_id=CLIENT_ID,
        token=token,
        is_async=False,
        log_path=""
    )

    send_telegram("📡 Fyers Model Connected")
    return model

# ================= DAILY RESET =================
def daily_reset():
    global position_type, entry_price, entry_time
    global symbol, last_exit_time
    global stop_loss, trail_level, running_high
    global _indicator_cache, _cached_expiry, _cached_expiry_date
    global last_exit_symbol, last_exit_symbol_time

    position_type=None
    entry_price=0
    entry_time=None
    symbol=None
    last_exit_time=None
    stop_loss=None
    trail_level=None
    running_high=None
    last_exit_symbol=None
    last_exit_symbol_time=None
    _indicator_cache={}
    _cached_expiry=None
    _cached_expiry_date=None

    send_telegram("🔄 Daily Reset Completed")

# ================= MAIN LOOP =================
send_telegram("🚀 BankNifty Option Trend Algo Started")
print("🚀 BankNifty Option Trend Algo Started")

while True:
    try:
        now=ist_time()
        today=ist_today()

        if now >= time(16,0) and last_reset_date != today:
            daily_reset()
            last_reset_date = today

        # Load holidays at 8:55, OR immediately if the bot started late and
        # hasn't loaded them yet today (otherwise holidays look like open days).
        if holiday_load_date!=today and (time(8,55)<=now<time(9,0) or now>=time(9,0)):
            fetch_nse_holidays()
            holiday_load_date=today

        if time(9,0)<=now<time(15,30):
            if token_load_date!=today:
                load_token()
                token_load_date=today

        if time(9,0)<=now<time(15,30):
            if model_load_date!=today:
                m=load_model()
                if m is not None:
                    fyers=m
                    model_load_date=today
                # if login failed, leave model_load_date unset so we retry next loop

        if time(9,20)<=now<=time(15,30) and model_load_date==today:
            if is_market_open():
                run_strategy()

        # Adaptive sleep: poll fast while holding a position so SL/trail/time
        # exits stay responsive; slower while idle. Heavy indicator work is
        # throttled separately by INDICATOR_REFRESH_SEC inside get_indicators.
        t.sleep(2 if position_type else 20)

    except Exception as e:
        send_telegram(f"❌ Algo Error: {e}")
        t.sleep(5)