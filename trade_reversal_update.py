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

SAVE_DIR = os.path.join(os.getcwd(), "data", "trade_reversal_update")
os.makedirs(SAVE_DIR, exist_ok=True)
TRADE_CSV = os.path.join(SAVE_DIR, "live_trades.csv")

# swing detection sensitivity (higher -> smoother fewer swings)
SWING_ORDER = 21

# Candles config
CANDLE_RESOLUTION = "5m"
CANDLE_DAYS = 15
TIMEZONE = "Asia/Kolkata"

# networking
HTTP_TIMEOUT = 10

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
    """Save a snapshot of processed HA+Trendline with timestamps preserved."""
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
        r = requests.get(url, headers=headers, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        data = r.json().get("result", {})
        price = float(data.get("mark_price", 0))
        return price if price > 0 else None
    except Exception as e:
        log(f"⚠️ Error fetching ticker price for {symbol}: {e}")
        return None

def fetch_candles(symbol, resolution=CANDLE_RESOLUTION, days=CANDLE_DAYS, tz=TIMEZONE):
    """
    Returns a DataFrame with columns Open, High, Low, Close, Volume and a timezone-aware index.
    """
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
        r = requests.get(url, params=params, headers=headers, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        data = r.json().get("result", [])
        if not data:
            log(f"No candle data for {symbol}")
            return None

        df = pd.DataFrame(data, columns=["time", "open", "high", "low", "close", "volume"])
        df.rename(columns=str.title, inplace=True)

        first_time = df["Time"].iloc[0]
        # detect seconds vs milliseconds
        if first_time > 1e12:
            df["Time"] = pd.to_datetime(df["Time"], unit="ms", utc=True)
        else:
            df["Time"] = pd.to_datetime(df["Time"], unit="s", utc=True)

        # convert to desired timezone and set as index
        df["Time"] = df["Time"].dt.tz_convert(tz)
        df.set_index("Time", inplace=True)
        df.sort_index(inplace=True)
        df = df[~df.index.duplicated(keep='last')]

        for col in ["Open", "High", "Low", "Close", "Volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")

        return df

    except Exception as e:
        log(f"⚠️ Error fetching candles for {symbol}: {e}")
        return None

# ================= TRENDLINE & HA ========================
def calculate_trendline(df, swing_order=SWING_ORDER):
    """
    Return a DataFrame of Heikin-Ashi bars aligned with `df.index` and a Trendline column.
    - Keeps the original time index.
    - Trendline carries the most recent swing high or swing low forward.
    """
    if df is None or len(df) == 0:
        return None

    ha = ta.ha(open_=df['Open'], high=df['High'], low=df['Low'], close=df['Close'])
    # align index to original candle timestamps
    ha.index = df.index

    # detect swing highs & lows on HA highs/lows
    max_idx = argrelextrema(ha['HA_high'].values, np.greater_equal, order=swing_order)[0]
    min_idx = argrelextrema(ha['HA_low'].values, np.less_equal, order=swing_order)[0]

    ha['max_high'] = np.nan
    ha['max_low'] = np.nan

    if len(max_idx) > 0:
        ha.iloc[max_idx, ha.columns.get_loc('max_high')] = ha['HA_high'].iloc[max_idx].values
    if len(min_idx) > 0:
        ha.iloc[min_idx, ha.columns.get_loc('max_low')] = ha['HA_low'].iloc[min_idx].values

    ha['max_high'] = ha['max_high'].ffill().bfill()
    ha['max_low']  = ha['max_low'].ffill().bfill()

    # Build Trendline by carrying the most recent detected swing forward
    ha['Trendline'] = np.nan

    # initialize trendline from first available max_high / max_low or fallback to HA_high
    if not pd.isna(ha['max_high'].iat[0]):
        trendline = ha['max_high'].iat[0]
    elif not pd.isna(ha['max_low'].iat[0]):
        trendline = ha['max_low'].iat[0]
    else:
        trendline = ha['HA_high'].iat[0]

    ha.loc[ha.index[0], 'Trendline'] = trendline

    for i in range(1, len(ha)):
        if not pd.isna(ha['max_high'].iat[i]) and ha['HA_high'].iat[i] == ha['max_high'].iat[i]:
            trendline = ha['HA_high'].iat[i]
        elif not pd.isna(ha['max_low'].iat[i]) and ha['HA_low'].iat[i] == ha['max_low'].iat[i]:
            trendline = ha['HA_low'].iat[i]
        ha.loc[ha.index[i], 'Trendline'] = trendline

    # Ensure desired column names exist for downstream code
    # pandas_ta returns HA_open, HA_high, HA_low, HA_close names already
    return ha

# ================= TRADING LOGIC (single-position) =========
def process_price_trend(symbol, price, positions, last_base_price,
                        trailing_level, upper_entry_limit, lower_entry_limit,
                        prev_ha_row, last_ha_row, is_new_bar, ha_df):
    """
    prev_ha_row: previous completed HA bar (Series, iloc[-2])
    last_ha_row: most recent HA row (Series, iloc[-1])
    is_new_bar: bool indicating prev bar just completed (only then evaluate entries)
    ha_df: full HA DataFrame (indexed by time)
    """
    contracts = DEFAULT_CONTRACTS.get(symbol, 0)
    contract_size = CONTRACT_SIZE.get(symbol, 1.0)
    pos = positions.get(symbol)

    # safety checks
    if ha_df is None or len(ha_df) < 4:
        return

    # Initialize per-symbol runtime state
    if last_base_price.get(symbol) is None:
        last_base_price[symbol] = price
        trailing_level[symbol] = None
        upper_entry_limit[symbol] = None
        lower_entry_limit[symbol] = None
        return

    # Use the previous completed HA row for signals
    trendline_prev = prev_ha_row['Trendline']
    prev_close = prev_ha_row['HA_close']
    prev_open = prev_ha_row['HA_open']

    # ================= ENTRY (no position open) =================
    if pos is None and is_new_bar:
        # LONG: prev HA bar closed bullish and above trendline
        if (prev_close > trendline_prev) and (prev_close > prev_open):
            positions[symbol] = {
                "side": "long",
                "entry": price,
                "stop": trendline_prev,          # initial stop = previous trendline
                "contracts": contracts,
                "contract_size": contract_size,
                "entry_time": datetime.now(),
                "trendline": trendline_prev
            }
            log(f"{symbol} | LONG ENTRY | TL(prev): {trendline_prev} | Entry: {price}")
            return

        # SHORT: prev HA bar closed bearish and below trendline
        if (prev_close < trendline_prev) and (prev_close < prev_open):
            positions[symbol] = {
                "side": "short",
                "entry": price,
                "stop": trendline_prev,
                "contracts": contracts,
                "contract_size": contract_size,
                "entry_time": datetime.now(),
                "trendline": trendline_prev
            }
            log(f"{symbol} | SHORT ENTRY | TL(prev): {trendline_prev} | Entry: {price}")
            return

    # ================= MANAGEMENT & EXIT (position exists) =================
    if pos is not None:
        side = pos.get("side")

        # LONG MANAGEMENT
        if side == "long":
            # If the previous completed bar created the current trendline as a swing high,
            # update stop to that bar's HA_low (only move stop up).
            if prev_ha_row['Trendline'] == prev_ha_row['HA_high']:
                new_stop = prev_ha_row['HA_low']
                if new_stop > pos["stop"]:
                    pos["stop"] = new_stop
                    log(f"{symbol} | LONG STOP MOVED UP -> {pos['stop']} (by new swing)")

            # Exit when price crosses below stop
            if price < pos["stop"]:
                pnl = (price - pos["entry"]) * pos["contract_size"] * pos["contracts"]
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
                # clear position slot
                positions[symbol] = None
                last_base_price[symbol] = price
                trailing_level[symbol] = None
                upper_entry_limit[symbol] = None
                lower_entry_limit[symbol] = None
                return

        # SHORT MANAGEMENT
        elif side == "short":
            # If the previous completed bar created the current trendline as a swing low,
            # update stop to that bar's HA_high (only move stop down).
            if prev_ha_row['Trendline'] == prev_ha_row['HA_low']:
                new_stop = prev_ha_row['HA_high']
                if new_stop < pos["stop"]:
                    pos["stop"] = new_stop
                    log(f"{symbol} | SHORT STOP MOVED DOWN -> {pos['stop']} (by new swing)")

            # Exit when price crosses above stop
            if price > pos["stop"]:
                pnl = (pos["entry"] - price) * pos["contract_size"] * pos["contracts"]
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
                positions[symbol] = None
                last_base_price[symbol] = price
                trailing_level[symbol] = None
                upper_entry_limit[symbol] = None
                lower_entry_limit[symbol] = None
                return

# ================= MAIN LOOP =================================
def run_live(sleep_seconds=20):
    positions = {s: None for s in SYMBOLS}
    last_base_price = {s: None for s in SYMBOLS}
    trailing_level = {s: None for s in SYMBOLS}
    upper_entry_limit = {s: None for s in SYMBOLS}
    lower_entry_limit = {s: None for s in SYMBOLS}
    last_bar_time = {s: None for s in SYMBOLS}

    log("🚀 Starting Trendline + HA Trailing Stop Strategy (SINGLE POSITION PER SYMBOL) - CLEAN REWRITE")

    while True:
        try:
            for symbol in SYMBOLS:
                df = fetch_candles(symbol, resolution=CANDLE_RESOLUTION, days=CANDLE_DAYS, tz=TIMEZONE)
                if df is None or len(df) < 50:
                    continue

                ha_df = calculate_trendline(df, swing_order=SWING_ORDER)
                if ha_df is None or len(ha_df) < 4:
                    continue

                # detect if last candle timestamp changed -> new completed bar
                last_bar = ha_df.index[-1]
                is_new_bar = last_bar_time[symbol] != last_bar
                if is_new_bar:
                    last_bar_time[symbol] = last_bar

                prev_ha_row = ha_df.iloc[-2]  # previous completed HA bar
                last_ha_row = ha_df.iloc[-1]  # current/latest HA bar (could be incomplete depending on source)

                save_processed_data(ha_df, symbol)

                price = fetch_ticker_price(symbol)
                if price is None:
                    continue

                process_price_trend(
                    symbol=symbol,
                    price=price,
                    positions=positions,
                    last_base_price=last_base_price,
                    trailing_level=trailing_level,
                    upper_entry_limit=upper_entry_limit,
                    lower_entry_limit=lower_entry_limit,
                    prev_ha_row=prev_ha_row,
                    last_ha_row=last_ha_row,
                    is_new_bar=is_new_bar,
                    ha_df=ha_df
                )

            time.sleep(sleep_seconds)

        except Exception as e:
            log(f"⚠️ Main loop error: {e}")
            time.sleep(5)

# ================= MAIN ENTRY ==============================
if __name__ == "__main__":
    run_live()
