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
            "Trendline": df["Trendline"].values
        })
        df_save.to_csv(save_path, index=False)
    except Exception as e:
        log(f"⚠️ Error saving processed data for {symbol}: {e}")

# ================= DATA FETCHING ==========================
def fetch_ticker_price(symbol):
    url = f"https://api.india.delta.exchange/v2/tickers/{symbol}"
    headers = {"Accept": "application/json"}
    try:
        r = requests.get(url, headers=headers, timeout=5)
        r.raise_for_status()
        data = r.json().get("result", {})
        price = float(data.get("mark_price", 0))
        return price if price > 0 else None
    except Exception as e:
        log(f"⚠️ Error fetching ticker price for {symbol}: {e}")
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

# ================= TRENDLINE CALCULATION ===================
def calculate_trendline(df):
    ha = ta.ha(open_=df['Open'], high=df['High'], low=df['Low'], close=df['Close'])
    ha = ha.reset_index(drop=True)

    max_idx = argrelextrema(ha['HA_high'].values, np.greater_equal, order=48)[0]
    min_idx = argrelextrema(ha['HA_low'].values, np.less_equal, order=48)[0]

    ha['max_high'] = np.nan
    ha['max_low'] = np.nan

    if len(max_idx) > 0:
        ha.loc[max_idx, 'max_high'] = ha.loc[max_idx, 'HA_high']
    if len(min_idx) > 0:
        ha.loc[min_idx, 'max_low'] = ha.loc[min_idx, 'HA_low']

    ha["max_high"] = ha["max_high"].ffill()
    ha["max_low"] = ha["max_low"].ffill()

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

def process_price_trend(symbol, price, positions, last_base_price,
                        trailing_level, upper_entry_limit, lower_entry_limit,
                        prav_close, last_close, df):

    contracts = DEFAULT_CONTRACTS[symbol]
    contract_size = CONTRACT_SIZE[symbol]

    pos_long = positions[symbol]["long"]
    pos_short = positions[symbol]["short"]

    # Safety: must have trendline
    if df is None or len(df) < 3 or "Trendline" not in df.columns:
        return

    raw_trendline = df["Trendline"].iloc[-1]

    # Initialize first update
    if last_base_price.get(symbol) is None:
        last_base_price[symbol] = price
        trailing_level[symbol] = None
        upper_entry_limit[symbol] = None
        lower_entry_limit[symbol] = None
        return

    # ===============================
    #        ENTRY LOGIC
    # ===============================

    # ------- LONG ENTRY -------
    if pos_long is None and last_close > raw_trendline and last_close > prav_close:
        positions[symbol]["long"] = {
            "side": "long",
            "entry": price,
            "stop": raw_trendline,          # no need to filter
            "contracts": contracts,
            "contract_size": contract_size,
            "entry_time": datetime.now(),
            "trendline": raw_trendline
        }
        log(f"{symbol} | LONG ENTRY | TL: {raw_trendline} | Entry: {price}")

    # ------- SHORT ENTRY -------
    if pos_short is None and last_close < raw_trendline and last_close < prav_close:
        positions[symbol]["short"] = {
            "side": "short",
            "entry": price,
            "stop": raw_trendline,
            "contracts": contracts,
            "contract_size": contract_size,
            "entry_time": datetime.now(),
            "trendline": raw_trendline
        }
        log(f"{symbol} | SHORT ENTRY | TL: {raw_trendline} | Entry: {price}")

    # ===============================
    #         EXIT LOGIC
    # ===============================

    # -------- LONG MANAGEMENT ----------
    pos = positions[symbol]["long"]
    if pos is not None:
            # If trendline bar indicates HA high swing (new high), update stop to that bar's HA_low
            # (using the previous bar values)
        if df["Trendline"].iloc[-1] == df["HA_high"].iloc[-1]:
                new_stop = df["HA_low"].iloc[-1]
                # only allow stop to move up (never down)
                if new_stop > pos["stop"]:
                    pos["stop"] = new_stop
                    log(f"{symbol} | LONG | Stop:{pos['stop']}")

            # Enforce directional movement: never decrease stop below original anchor
            # (pos["trendline"] is the anchor set on entry)
        pos["stop"] = max(pos["stop"], pos.get("trendline", pos["stop"]))
        

        # Exit condition
        if price < pos["stop"]:
            pnl = (price - pos["entry"]) * contract_size * pos["contracts"]
            fee = commission(price, pos["contracts"], symbol)
            net = pnl - fee

            save_trade_row({
                "symbol": symbol,
                "side": "long",
                "entry_time": pos["entry_time"],
                "exit_time": datetime.now(),
                "qty": pos["contracts"],
                "entry": pos["entry"],
                "exit": price,
                "gross_pnl": round(pnl, 6),
                "commission": round(fee, 6),
                "net_pnl": round(net, 6)
            })

            log(f"{symbol} | LONG EXIT | Stop:{pos['stop']} | Exit:{price} | Net:{net}")
            positions[symbol]["long"] = None

    # -------- SHORT MANAGEMENT ----------
    pos = positions[symbol]["short"]
    if pos is not None:

       # If trendline bar indicates HA low swing (new low), update stop to that bar's HA_high
        if df["Trendline"].iloc[-1] == df["HA_low"].iloc[-1]:
                new_stop = df["HA_high"].iloc[-1]
                # only allow stop to move down (never up) for short
                if new_stop < pos["stop"]:
                    pos["stop"] = new_stop
                    log(f"{symbol} | SHORT | Stop:{pos['stop']}")

            # Stop cannot move up for a short position relative to original anchor
        pos["stop"] = min(pos["stop"], pos.get("trendline", pos["stop"]))
        

        if price > pos["stop"]:
            pnl = (pos["entry"] - price) * contract_size * pos["contracts"]
            fee = commission(price, pos["contracts"], symbol)
            net = pnl - fee

            save_trade_row({
                "symbol": symbol,
                "side": "short",
                "entry_time": pos["entry_time"],
                "exit_time": datetime.now(),
                "qty": pos["contracts"],
                "entry": pos["entry"],
                "exit": price,
                "gross_pnl": round(pnl, 6),
                "commission": round(fee, 6),
                "net_pnl": round(net, 6)
            })

            log(f"{symbol} | SHORT EXIT | Stop:{pos['stop']} | Exit:{price} | Net:{net}")
            positions[symbol]["short"] = None

# ================= MAIN LOOP ===============================
def run_live():
    positions = {s: {"long": None, "short": None} for s in SYMBOLS}
    last_base_price = {s: None for s in SYMBOLS}
    trailing_level = {s: None for s in SYMBOLS}
    upper_entry_limit = {s: None for s in SYMBOLS}
    lower_entry_limit = {s: None for s in SYMBOLS}

    log("🚀 Starting Trendline + HA Low/High Trailing Stop Strategy (HEDGING ENABLED)")

    while True:
        try:
            for symbol in SYMBOLS:
                df = fetch_candles(symbol, resolution="1h", days=30)
                if df is None or len(df) < 50:
                    continue

                ha_df = calculate_trendline(df)
                if len(ha_df) < 3:
                    continue

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


# ================= MAIN ENTRY ==============================
if __name__ == "__main__":
    run_live()