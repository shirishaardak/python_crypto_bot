import os
import time
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import pandas_ta as ta
from scipy.signal import argrelextrema

# ================= SETTINGS =================
SYMBOLS = ["BTCUSD", "ETHUSD"]

DEFAULT_CONTRACTS = {"BTCUSD": 30, "ETHUSD": 30}
CONTRACT_SIZE = {"BTCUSD": 0.001, "ETHUSD": 0.01}
TAKE_PROFIT = {"BTCUSD": 300, "ETHUSD": 15}
MAX_SL = {"BTCUSD": 500, "ETHUSD": 30}
TAKER_FEE = 0.0005

BASE_DIR = os.getcwd()
SAVE_DIR = os.path.join(BASE_DIR, "data", "trade_reversal")
os.makedirs(SAVE_DIR, exist_ok=True)

TRADE_CSV = os.path.join(SAVE_DIR, "live_trades.csv")

# ================= UTILITIES =================
def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}")

def commission(price, qty, symbol):
    notional = price * CONTRACT_SIZE[symbol] * qty
    return notional * TAKER_FEE

def save_trade(trade):
    df = pd.DataFrame([trade])
    df.to_csv(
        TRADE_CSV,
        mode="a",
        header=not os.path.exists(TRADE_CSV),
        index=False
    )

def save_processed_data(df, ha, symbol):
    path = os.path.join(SAVE_DIR, f"{symbol}_processed.csv")
    out = pd.DataFrame({
        "time": df.index,
        "HA_open": ha["HA_open"],
        "HA_high": ha["HA_high"],
        "HA_low": ha["HA_low"],
        "HA_close": ha["HA_close"],
        "Trendline": ha["Trendline"]
    })
    out.to_csv(path, index=False)

# ================= DATA =================
def fetch_price(symbol):
    try:
        r = requests.get(
            f"https://api.india.delta.exchange/v2/tickers/{symbol}",
            timeout=5
        )
        return float(r.json()["result"]["mark_price"])
    except:
        return None

def fetch_candles(symbol, resolution="15m", days=1, tz='Asia/Kolkata'):
    headers = {'Accept': 'application/json'}
    start = int((datetime.now() - timedelta(days=days)).timestamp())
    params = {
        'resolution': resolution,
        'symbol': symbol,
        'start': str(start),
        'end': str(int(time.time())),
    }
    url = 'https://api.india.delta.exchange/v2/history/candles'
    try:
        r = requests.get(url, params=params, headers=headers, timeout=10)
        r.raise_for_status()
        data = r.json().get("result", [])
        if not data:
            log(f"No data for {symbol}")
            return None

        df = pd.DataFrame(data, columns=["time", "open", "high", "low", "close", "volume"])
        df.rename(columns=str.title, inplace=True)

        first_time = df["Time"].iloc[0]
        if first_time > 1e12:
            df["Time"] = pd.to_datetime(df["Time"], unit="ms", utc=True)
        else:
            df["Time"] = pd.to_datetime(df["Time"], unit="s", utc=True)

        df["Time"] = df["Time"].dt.tz_convert(tz)
        df["time"] = df["Time"]
        df.set_index("Time", inplace=True)
        df.sort_index(inplace=True)
        df = df[~df.index.duplicated(keep='last')]

        for col in ["Open", "High", "Low", "Close", "Volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")

        return df

    except Exception as e:
        log(f"⚠️ Error fetching {symbol}: {e}")
        return None

# ================= TRENDLINE =================
def calculate_trendline(df):
    ha = ta.ha(open_=df['Open'], high=df['High'], low=df['Low'], close=df['Close'])
    ha = ha.reset_index(drop=True)

    max_idx = argrelextrema(ha['HA_high'].values, np.greater_equal, order=11)[0]
    min_idx = argrelextrema(ha['HA_low'].values, np.less_equal, order=11)[0]

    ha['max_high'] = np.nan
    ha['max_low'] = np.nan

    if len(max_idx) > 0:
        ha.loc[max_idx, 'max_high'] = ha.loc[max_idx, 'HA_high']
    if len(min_idx) > 0:
        ha.loc[min_idx, 'max_low'] = ha.loc[min_idx, 'HA_low']

    ha[['max_high', 'max_low']] = ha[['max_high', 'max_low']].ffill()

    ha['Trendline'] = np.nan
    if len(ha) == 0:
        return ha

    trendline = ha['max_high'].iloc[0] if not pd.isna(ha['max_high'].iloc[0]) else ha['HA_high'].iloc[0]
    ha.loc[0, 'Trendline'] = trendline

    for i in range(1, len(ha)):
        if not pd.isna(ha.loc[i, 'max_high']) and ha.loc[i, 'HA_high'] == ha.loc[i, 'max_high']:
            trendline = ha.loc[i, 'HA_high']
        elif not pd.isna(ha.loc[i, 'max_low']) and ha.loc[i, 'HA_low'] == ha.loc[i, 'max_low']:
            trendline = ha.loc[i, 'HA_low']
        ha.loc[i, 'Trendline'] = trendline

    return ha
# ================= STRATEGY =================
def process_symbol(symbol, df, price, state):
    ha = calculate_trendline(df)
    save_processed_data(df, ha, symbol)

    last = ha.iloc[-1]
    prev = ha.iloc[-2]
    candle_time = df.index[-1]

    pos = state["position"]

    # ========== ENTRY ==========
    if pos is None and state["last_trade_time"] != candle_time:

        # LONG
        if last.HA_close > last.Trendline and last.HA_close > prev.HA_close:
            state["position"] = {
                "side": "long",
                "entry": price,
                "stop": max(last.Trendline, price - MAX_SL[symbol]),
                "qty": DEFAULT_CONTRACTS[symbol],
                "entry_time": candle_time
            }
            state["last_trade_time"] = candle_time
            log(f"{symbol} | LONG ENTRY @ {price}")
            return

        # SHORT
        if last.HA_close < last.Trendline and last.HA_close < prev.HA_close:
            state["position"] = {
                "side": "short",
                "entry": price,
                "stop": min(last.Trendline, price + MAX_SL[symbol]),
                "qty": DEFAULT_CONTRACTS[symbol],
                "entry_time": candle_time
            }
            state["last_trade_time"] = candle_time
            log(f"{symbol} | SHORT ENTRY @ {price}")
            return

    # ========== MANAGEMENT ==========
    if pos:
        side = pos["side"]

        # TRAILING STOP
        if side == "long" and last.HA_high == last.Trendline:
            pos["stop"] = max(pos["stop"], last.HA_low)

        if side == "short" and last.HA_low == last.Trendline:
            pos["stop"] = min(pos["stop"], last.HA_high)

        exit_trade = False

        # EXIT CONDITIONS
        if side == "long":
            if last.HA_close < pos["stop"] or price >= pos["entry"] + TAKE_PROFIT[symbol]:
                pnl = (price - pos["entry"]) * CONTRACT_SIZE[symbol] * pos["qty"]
                exit_trade = True

        if side == "short":
            if last.HA_close > pos["stop"] or price <= pos["entry"] - TAKE_PROFIT[symbol]:
                pnl = (pos["entry"] - price) * CONTRACT_SIZE[symbol] * pos["qty"]
                exit_trade = True

        if exit_trade:
            fee = commission(price, pos["qty"], symbol)
            net = pnl - fee

            save_trade({
                "symbol": symbol,
                "side": side,
                "entry_price": pos["entry"],
                "exit_price": price,
                "qty": pos["qty"],
                "net_pnl": round(net, 6),
                "entry_time": pos["entry_time"],
                "exit_time": datetime.now()
            })

            log(f"{symbol} | {side.upper()} EXIT @ {price} | NET: {round(net, 6)}")
            state["position"] = None

# ================= MAIN =================
def run():
    # create trade file early
    if not os.path.exists(TRADE_CSV):
        pd.DataFrame(columns=[
            "symbol", "side",
            "entry_price", "exit_price",
            "qty", "net_pnl",
            "entry_time", "exit_time"
        ]).to_csv(TRADE_CSV, index=False)

    state = {
        s: {"position": None, "last_trade_time": None}
        for s in SYMBOLS
    }

    log("🚀 Trendline HA Strategy Started")

    while True:
        try:
            for symbol in SYMBOLS:
                df = fetch_candles(symbol)
                if df is None or len(df) < 50:
                    continue

                price = fetch_price(symbol)
                if price is None:
                    continue

                process_symbol(symbol, df, price, state[symbol])

            time.sleep(20)

        except Exception as e:
            log(f"⚠️ Error: {e}")
            time.sleep(5)

if __name__ == "__main__":
    run()
