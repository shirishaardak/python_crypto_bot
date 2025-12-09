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
TAKE_PROFIT = {"BTCUSD": 200, "ETHUSD": 10}
TAKER_FEE = 0.0005

SAVE_DIR = os.path.join(os.getcwd(), "data", "price_trend_following_strategie")
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

    max_idx = argrelextrema(ha['HA_high'].values, np.greater_equal, order=42)[0]
    min_idx = argrelextrema(ha['HA_low'].values, np.less_equal, order=42)[0]

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

    # Keep HA columns named as you expect for later use
    ha = ha.rename(columns={
        "HA_open": "HA_open",
        "HA_high": "HA_high",
        "HA_low": "HA_low",
        "HA_close": "HA_close"
    })

    return ha

# ================= TRADING LOGIC (single-position) ===================
def process_price_trend(symbol, price, positions, last_base_price,
                        trailing_level, upper_entry_limit, lower_entry_limit,
                        prav_close, last_close, df):

    contracts = DEFAULT_CONTRACTS[symbol]
    contract_size = CONTRACT_SIZE[symbol]
    take_profit = TAKE_PROFIT[symbol]

    # Single position slot per symbol: None or dict
    pos = positions.get(symbol)

    # Safety: must have trendline and sufficient rows
    if df is None or len(df) < 3 or "Trendline" not in df.columns:
        return

    # Use previous completed HA bar's trendline for entry/management
    raw_trendline = df["Trendline"].iloc[-1]

    # Initialize per-symbol state on first run
    if last_base_price.get(symbol) is None:
        last_base_price[symbol] = price
        trailing_level[symbol] = None
        upper_entry_limit[symbol] = None
        lower_entry_limit[symbol] = None
        return

    # ===============================
    # ENTRY (only if no position open)
    # ===============================
    if pos is None:
        # LONG ENTRY
        if last_close > raw_trendline and last_close > prav_close and datetime.now().minute % 5 == 0:
            positions[symbol] = {
                "side": "long",
                "entry": price,
                "stop": raw_trendline,          # initial stop = raw trendline
                "contracts": contracts,
                "contract_size": contract_size,
                "entry_time": datetime.now(),
                "trendline": raw_trendline     # anchor for directional filtering
            }
            log(f"{symbol} | LONG ENTRY | TL: {raw_trendline} | Entry: {price}")
            return

        # SHORT ENTRY
        if last_close < raw_trendline and last_close < prav_close and datetime.now().minute % 5 == 0:
            positions[symbol] = {
                "side": "short",
                "entry": price,
                "stop": raw_trendline,
                "contracts": contracts,
                "contract_size": contract_size,
                "entry_time": datetime.now(),
                "trendline": raw_trendline
            }
            log(f"{symbol} | SHORT ENTRY | TL: {raw_trendline} | Entry: {price}")
            return

    # ===============================
    # MANAGEMENT & EXIT (only if position exists)
    # ===============================
    if pos is not None:
        side = pos.get("side")

        # ---------- LONG MANAGEMENT ----------
        if side == "long":
            # If trendline bar indicates HA high swing (new high), update stop to that bar's HA_low
            # (using the previous bar values)
            if raw_trendline == df["HA_high"].iloc[-1]:
                new_stop = df["HA_high"].iloc[-1]
                # only allow stop to move up (never down)
                if new_stop > pos["stop"]:
                    pos["stop"] = new_stop

            # Enforce directional movement: never decrease stop below original anchor
            # (pos["trendline"] is the anchor set on entry)
            pos["stop"] = max(pos["stop"], pos.get("trendline", pos["stop"]))

            # Exit on price crossing below stop
            if last_close < pos["stop"]:
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
                # clear single slot
                positions[symbol] = None
                last_base_price[symbol] = price
                trailing_level[symbol] = None
                upper_entry_limit[symbol] = None
                lower_entry_limit[symbol] = None
                return

        # ---------- SHORT MANAGEMENT ----------
        elif side == "short":
            # If trendline bar indicates HA low swing (new low), update stop to that bar's HA_high
            if raw_trendline == df["HA_low"].iloc[-1]:
                new_stop = df["HA_low"].iloc[-1]
                # only allow stop to move down (never up) for short
                if new_stop < pos["stop"]:
                    pos["stop"] = new_stop

            # Stop cannot move up for a short position relative to original anchor
            pos["stop"] = min(pos["stop"], pos.get("trendline", pos["stop"]))

            # Exit on price crossing above stop
            if last_close > pos["stop"]:
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
                # clear single slot
                positions[symbol] = None
                last_base_price[symbol] = price
                trailing_level[symbol] = None
                upper_entry_limit[symbol] = None
                lower_entry_limit[symbol] = None
                return

# ================= MAIN LOOP ===============================
def run_live():
    # single position slot per symbol (None or dict)
    positions = {s: None for s in SYMBOLS}
    last_base_price = {s: None for s in SYMBOLS}
    trailing_level = {s: None for s in SYMBOLS}
    upper_entry_limit = {s: None for s in SYMBOLS}
    lower_entry_limit = {s: None for s in SYMBOLS}

    log("🚀 Starting Trendline + HA Trailing Stop Strategy (SINGLE POSITION PER SYMBOL)")

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
