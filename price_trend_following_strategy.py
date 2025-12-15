import os
import time
import requests
import pandas as pd
from datetime import datetime, timedelta
import pandas_ta as ta
from scipy.signal import argrelextrema
import numpy as np

# ================= SETTINGS ===============================
SYMBOLS = ["BTCUSD", "ETHUSD"]

DEFAULT_CONTRACTS = {"BTCUSD": 30, "ETHUSD": 30}
CONTRACT_SIZE = {"BTCUSD": 0.001, "ETHUSD": 0.01}
TAKER_FEE = 0.0005

# HARD STOP LOSS (POINTS)
STOP_LOSS = {"BTCUSD": 500, "ETHUSD": 25}

SAVE_DIR = os.path.join(os.getcwd(), "data", "price_trend_following_strategy")
os.makedirs(SAVE_DIR, exist_ok=True)
TRADE_CSV = os.path.join(SAVE_DIR, "live_trades.csv")

# ================= UTILITIES ==============================
def log(msg: str):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}")

def commission(price, contracts, symbol):
    notional = price * CONTRACT_SIZE.get(symbol, 1.0) * contracts
    return notional * TAKER_FEE

def save_trade_row(trade):
    df_trade = pd.DataFrame([trade])
    if not os.path.exists(TRADE_CSV):
        df_trade.to_csv(TRADE_CSV, index=False)
    else:
        df_trade.to_csv(TRADE_CSV, mode="a", header=False, index=False)

def save_processed_data(df, symbol):
    try:
        save_path = os.path.join(SAVE_DIR, f"{symbol}_processed.csv")
        df_save = pd.DataFrame({
            "time": df.index.astype(str),
            "HA_open": df["HA_open"].values,
            "HA_high": df["HA_high"].values,
            "HA_low": df["HA_low"].values,
            "HA_close": df["HA_close"].values,
            "Trendline": df["Trendline"].values,
            "ATR": df["ATR"].values,
            "ATR_avg": df["ATR_avg"].values
        })
        df_save.to_csv(save_path, index=False)
    except Exception as e:
        log(f"⚠️ Error saving processed data for {symbol}: {e}")

# ================= DATA FETCHING ==========================
def fetch_ticker_price(symbol):
    url = f"https://api.india.delta.exchange/v2/tickers/{symbol}"
    try:
        r = requests.get(url, headers={"Accept": "application/json"}, timeout=5)
        r.raise_for_status()
        data = r.json().get("result", {})
        price = float(data.get("mark_price", 0))
        return price if price > 0 else None
    except Exception as e:
        log(f"⚠️ Error fetching ticker price for {symbol}: {e}")
        return None

def fetch_candles(symbol, resolution="15m", days=15, tz='Asia/Kolkata'):
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
            return None

        df = pd.DataFrame(data, columns=["time", "open", "high", "low", "close", "volume"])
        df.rename(columns=str.title, inplace=True)

        first_time = df["Time"].iloc[0]
        unit = "ms" if first_time > 1e12 else "s"
        df["Time"] = pd.to_datetime(df["Time"], unit=unit, utc=True)
        df["Time"] = df["Time"].dt.tz_convert(tz)

        df.set_index("Time", inplace=True)
        df.sort_index(inplace=True)
        df.dropna(inplace=True)
        return df

    except Exception as e:
        log(f"⚠️ Error fetching {symbol}: {e}")
        return None

# ================= TRENDLINE ==============================
def calculate_trendline(df):
    ha = ta.ha(open_=df['Open'], high=df['High'], low=df['Low'], close=df['Close'])
    ha = ha.reset_index(drop=True)

    ha["ATR"] = ta.atr(ha['HA_high'], ha['HA_low'], ha['HA_close'], length=14)
    ha["ATR_avg"] = ha["ATR"].rolling(14).mean()

    max_idx = argrelextrema(ha['HA_high'].values, np.greater_equal, order=48)[0]
    min_idx = argrelextrema(ha['HA_low'].values, np.less_equal, order=48)[0]

    ha['max_high'] = np.nan
    ha['max_low'] = np.nan

    ha.loc[max_idx, 'max_high'] = ha.loc[max_idx, 'HA_high']
    ha.loc[min_idx, 'max_low'] = ha.loc[min_idx, 'HA_low']

    ha[['max_high', 'max_low']] = ha[['max_high', 'max_low']].ffill().bfill()

    ha['Trendline'] = np.nan
    trendline = ha['max_high'].iloc[0]
    ha.loc[0, 'Trendline'] = trendline

    for i in range(1, len(ha)):
        if ha.loc[i, 'HA_high'] == ha.loc[i, 'max_high']:
            trendline = ha.loc[i, 'HA_low']
        elif ha.loc[i, 'HA_low'] == ha.loc[i, 'max_low']:
            trendline = ha.loc[i, 'HA_high']
        ha.loc[i, 'Trendline'] = trendline

    ha["Trendline"] = ha["Trendline"].ffill().bfill()
    return ha

# ================= TRADING LOGIC ==========================
def process_price_trend(symbol, price, positions, last_base_price,
                        trailing_level, upper_entry_limit, lower_entry_limit,
                        prav_close, last_close, df):

    contracts = DEFAULT_CONTRACTS[symbol]
    contract_size = CONTRACT_SIZE[symbol]
    pos = positions.get(symbol)

    raw_trendline = df["Trendline"].iloc[-1]

    if last_base_price[symbol] is None:
        last_base_price[symbol] = price
        return

    # ================= ENTRY =================
    if pos is None:

        if last_close > raw_trendline and last_close > prav_close and datetime.now().minute % 15 == 0:
            positions[symbol] = {
                "side": "long",
                "entry": price,
                "contracts": contracts,
                "entry_time": datetime.now()
            }
            log(f"{symbol} | LONG ENTRY | {price}")
            return

        if last_close < raw_trendline and last_close < prav_close and datetime.now().minute % 15 == 0:
            positions[symbol] = {
                "side": "short",
                "entry": price,
                "contracts": contracts,
                "entry_time": datetime.now()
            }
            log(f"{symbol} | SHORT ENTRY | {price}")
            return

    # ================= EXIT =================
    if pos is not None:

        sl = STOP_LOSS[symbol]

        # ---- HARD STOP LOSS ----
        if pos["side"] == "long" and price <= raw_trendline - sl:
            pnl = (price - pos["entry"]) * contract_size * pos["contracts"]
            fee = commission(price, pos["contracts"], symbol)
            net = pnl - fee
            log(f"{symbol} | LONG STOP LOSS | {price} | Net: {net}")
            positions[symbol] = None
            return

        if pos["side"] == "short" and price >= raw_trendline + sl:
            pnl = (pos["entry"] - price) * contract_size * pos["contracts"]
            fee = commission(price, pos["contracts"], symbol)
            net = pnl - fee
            log(f"{symbol} | SHORT STOP LOSS | {price} | Net: {net}")
            positions[symbol] = None
            return

        # ---- EXISTING TRENDLINE EXIT (UNCHANGED) ----
        if pos["side"] == "long" and last_close < raw_trendline:
            log(f"{symbol} | LONG EXIT (TRENDLINE) | {price}")
            positions[symbol] = None
            return

        if pos["side"] == "short" and last_close > raw_trendline:
            log(f"{symbol} | SHORT EXIT (TRENDLINE) | {price}")
            positions[symbol] = None
            return

# ================= MAIN LOOP ==============================
def run_live():
    positions = {s: None for s in SYMBOLS}
    last_base_price = {s: None for s in SYMBOLS}
    trailing_level = {s: None for s in SYMBOLS}
    upper_entry_limit = {s: None for s in SYMBOLS}
    lower_entry_limit = {s: None for s in SYMBOLS}

    log("🚀 Starting Trendline + Hard Stop Loss Strategy")

    while True:
        try:
            for symbol in SYMBOLS:
                df = fetch_candles(symbol)
                if df is None or len(df) < 50:
                    continue

                ha_df = calculate_trendline(df)

                last_close = ha_df["HA_close"].iloc[-2]
                prav_close = ha_df["HA_open"].iloc[-3]

                save_processed_data(ha_df, symbol)

                price = fetch_ticker_price(symbol)
                if price is None:
                    continue

                process_price_trend(
                    symbol, price, positions, last_base_price,
                    trailing_level, upper_entry_limit, lower_entry_limit,
                    prav_close, last_close, ha_df
                )

            time.sleep(20)

        except Exception as e:
            log(f"⚠️ Main loop error: {e}")
            time.sleep(5)

# ================= MAIN ==============================
if __name__ == "__main__":
    run_live()
