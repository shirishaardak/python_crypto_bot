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
STOP_LOSS = {"BTCUSD": 100, "ETHUSD": 10}
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
        # prepare a tidy save (time as ISO string)
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
    """
    Returns HA dataframe with Trendline column.
    Uses argrelextrema to pick highs/lows and forwards the active trendline.
    """
    ha = ta.ha(open_=df['Open'], high=df['High'], low=df['Low'], close=df['Close'])
    ha = ha.reset_index(drop=True)

    # Identify extrema (order chosen similar to your original approach)
    max_idx = argrelextrema(ha['HA_high'].values, np.greater_equal, order=48)[0]
    min_idx = argrelextrema(ha['HA_low'].values, np.less_equal, order=48)[0]

    ha['max_high'] = np.nan
    ha['max_low'] = np.nan

    if len(max_idx) > 0:
        ha.loc[max_idx, 'max_high'] = ha.loc[max_idx, 'HA_high']
    if len(min_idx) > 0:
        ha.loc[min_idx, 'max_low'] = ha.loc[min_idx, 'HA_low']
    ha['max_high'] = ha['max_high'] = ha['max_high'].ffill()
    ha['max_low']= ha['max_low'] = ha['max_high'].ffill()

    # initialize Trendline
    ha['Trendline'] = np.nan
    if len(ha) == 0:
        return ha

    # fallback initial trendline to first HA_high if needed
    trendline = ha['max_high'].iloc[0] if not pd.isna(ha['max_high'].iloc[0]) else ha['HA_high'].iloc[0]
    ha.loc[0, 'Trendline'] = trendline

    for i in range(1, len(ha)):
        if not pd.isna(ha.loc[i, 'max_high']) and ha.loc[i, 'HA_high'] == ha.loc[i, 'max_high']:
            trendline = ha.loc[i, 'HA_low']
        elif not pd.isna(ha.loc[i, 'max_low']) and ha.loc[i, 'HA_low'] == ha.loc[i, 'max_low']:
            trendline = ha.loc[i, 'HA_high']
        # else keep previous trendline
        ha.loc[i, 'Trendline'] = trendline

    return ha

# ================= TRADING LOGIC (FULL) ===================
def process_price_trend(symbol, price, positions, last_base_price,
                        trailing_level, upper_entry_limit, lower_entry_limit,
                        prav_close, last_close, df):
    """
    - ENTRY uses raw_trendline = df['Trendline'].iloc[-1]
    - STOP uses filtered_trendline:
        - LONG: filtered_trendline can only go UP (never down)
        - SHORT: filtered_trendline can only go DOWN (never up)
    - Stop is set equal to filtered_trendline (exact)
    """

    contracts = DEFAULT_CONTRACTS[symbol]
    contract_size = CONTRACT_SIZE[symbol]
    SL = STOP_LOSS[symbol]
    pos = positions.get(symbol)

    # guard
    if df is None or len(df) == 0 or "Trendline" not in df.columns:
        return

    raw_trendline = df["Trendline"].iloc[-1]  # used for ENTRY

    # initialize symbol state if first time
    if last_base_price.get(symbol) is None:
        last_base_price[symbol] = price
        trailing_level[symbol] = None
        upper_entry_limit[symbol] = None
        lower_entry_limit[symbol] = None
        return

    if pos is None:
        # LONG ENTRY (using raw trendline for decision)
        if last_close > raw_trendline and last_close > prav_close and datetime.now().minute % 5 == 0:
            positions[symbol] = {
                "side": "long",
                "entry": price,
                "stop": raw_trendline - SL,               # initialize stop to filtered_trendline (same as raw on entry)
                "contracts": contracts,
                "contract_size": contract_size,
                "entry_time": datetime.now(),
                "trendline": raw_trendline          # anchor for filtering on subsequent ticks
            }
            log(f"{symbol} | LONG ENTRY | Entry: {price}")
            return

        # SHORT ENTRY (using raw trendline for decision)
        if last_close < raw_trendline and last_close < prav_close and datetime.now().minute % 5 == 0:
            positions[symbol] = {
                "side": "short",
                "entry": price,
                "stop": raw_trendline + SL,
                "contracts": contracts,
                "contract_size": contract_size,
                "entry_time": datetime.now(),
                "trendline": raw_trendline
            }
            log(f"{symbol} | SHORT ENTRY | Entry: {price}")
            return

    # =======================
    # MANAGEMENT (STOP = FILTERED)
    # =======================
    if pos is not None:
        # Always keep stop equal to filtered trendline
        # pos["stop"] = filtered_trendline

        # LONG side exit (using filtered trendline)
        if pos.get("side") == "long":

            if df['max_high'].iloc[-1] == df['HA_high'].iloc[-1]:
                new_stop = df['HA_low'].iloc[-1]
                if new_stop > pos["stop"]:
                    pos["stop"] = new_stop
                    log(f"{symbol} | LONG SL UPDATE → {new_stop}")

            if price < pos['stop']:
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

                log(f"{symbol} | LONG EXIT | Exit: {price} | Net: {net}")

                # reset
                positions[symbol] = None
                last_base_price[symbol] = price
                trailing_level[symbol] = None
                upper_entry_limit[symbol] = None
                lower_entry_limit[symbol] = None

        # SHORT side exit (using filtered trendline)
        elif pos.get("side") == "short":
            if df['max_low'].iloc[-1] == df['HA_low'].iloc[-1]:
                new_stop = df['HA_high'].iloc[-1]
                if new_stop > pos["stop"]:
                    pos["stop"] = new_stop
                    log(f"{symbol} | SHORT SL UPDATE → {new_stop}")
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

                log(f"{symbol} | SHORT EXIT  | Exit: {price} | Net: {net}")

                # reset
                positions[symbol] = None
                last_base_price[symbol] = price
                trailing_level[symbol] = None
                upper_entry_limit[symbol] = None
                lower_entry_limit[symbol] = None

# ================= MAIN LOOP ===============================
def run_live():
    positions = {s: None for s in SYMBOLS}
    last_base_price = {s: None for s in SYMBOLS}
    trailing_level = {s: None for s in SYMBOLS}
    upper_entry_limit = {s: None for s in SYMBOLS}
    lower_entry_limit = {s: None for s in SYMBOLS}

    log("🚀 Starting Trendline STOP-only Live Strategy")

    while True:
        try:
            for symbol in SYMBOLS:
                df = fetch_candles(symbol, resolution="5m", days=5)
                if df is None or len(df) < 50:
                    continue

                ha_df = calculate_trendline(df)
                if len(ha_df) < 3:
                    continue

                # last_close and prav_close (HA closes)
                last_close = ha_df["HA_close"].iloc[-1]
                prav_close = ha_df["HA_open"].iloc[-2]

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
