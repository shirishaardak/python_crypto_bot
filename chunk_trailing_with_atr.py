import os
import time
import requests
import pandas as pd
from datetime import datetime, timedelta
import pandas_ta as ta

# ================= SETTINGS ===============================
SYMBOLS = ["BTCUSD", "ETHUSD"]
ENTRY_MOVE = {"BTCUSD": 100, "ETHUSD": 10}
STOP_LOSS = {"BTCUSD": 200, "ETHUSD": 15}
TRAILING_CHUNK = {"BTCUSD": 50, "ETHUSD": 5}
DEFAULT_CONTRACTS = {"BTCUSD": 30, "ETHUSD": 30}
CONTRACT_SIZE = {"BTCUSD": 0.001, "ETHUSD": 0.01}
TAKER_FEE = 0.0005  # 0.05%

SAVE_DIR = os.path.join(os.getcwd(), "data", "chunk_trailing_with_atr")
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
            "time": df.index,
            "HA_open": df["HA_open"].values,
            "HA_high": df["HA_high"].values,
            "HA_low": df["HA_low"].values,
            "HA_close": df["HA_close"].values,
            "EMA": df["EMA"].values,
            "ATR": df["ATR"].values,
            "ATR_avg": df["ATR_avg"].values,
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

def fetch_candles(symbol, resolution="5m", days=1, tz='Asia/Kolkata'):
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

# ================= TREND STRATEGY =========================
def calculate_ema(df, period=10):
    ha_df = ta.ha(open_=df['Open'], high=df['High'], close=df['Close'], low=df['Low'])

    atr = ta.atr(high=df["High"], low=df["Low"], close=df["Close"], length=14)
    ha_df["ATR"] = atr

    ha_df["ATR_avg"] = ha_df["ATR"].rolling(14).mean()

    ha_df["EMA"] = ha_df["HA_close"].ewm(span=period, adjust=False).mean()

    return ha_df

# ============= UPDATED PROCESS PRICE TREND (FULL FIXED VERSION) =============
def process_price_trend(symbol, price, positions, last_base_price, trailing_level, ema_value, last_close, prev_close, df):

    contracts = DEFAULT_CONTRACTS[symbol]
    contract_size = CONTRACT_SIZE[symbol]
    entry_move = ENTRY_MOVE[symbol]
    sl_points = STOP_LOSS[symbol]
    chunk = TRAILING_CHUNK[symbol]

    pos = positions.get(symbol)

    if ema_value is None:
        return

    atr_val = df["ATR"].iloc[-2]
    atr_avg = df["ATR_avg"].iloc[-2]

    if pd.isna(atr_val) or pd.isna(atr_avg):
        return

    atr_condition = atr_val > atr_avg

    if last_base_price[symbol] is None:
        last_base_price[symbol] = price
        trailing_level[symbol] = None
        return

    # ================= ENTRY =================
    if pos is None:

        # LONG
        if atr_condition and price >= last_base_price[symbol] + entry_move and last_close > ema_value and last_close > prev_close:

            entry_price = price
            entry_fee = commission(entry_price, contracts, symbol)

            positions[symbol] = {
                "side": "long",
                "entry": entry_price,
                "stop": entry_price - sl_points,
                "contracts": contracts,
                "contract_size": contract_size,
                "entry_time": datetime.now(),
                "trailing_level": entry_price,
                "entry_fee": entry_fee
            }
            trailing_level[symbol] = entry_price
            log(f"{symbol} | LONG ENTRY | Entry: {entry_price}")
            return

        # SHORT
        elif atr_condition and price <= last_base_price[symbol] - entry_move and last_close < ema_value and last_close < prev_close:

            entry_price = price
            entry_fee = commission(entry_price, contracts, symbol)

            positions[symbol] = {
                "side": "short",
                "entry": entry_price,
                "stop": entry_price + sl_points,
                "contracts": contracts,
                "contract_size": contract_size,
                "entry_time": datetime.now(),
                "trailing_level": entry_price,
                "entry_fee": entry_fee
            }
            trailing_level[symbol] = entry_price
            log(f"{symbol} | SHORT ENTRY | Entry: {entry_price}")
            return

    # ================= POSITION MANAGEMENT =================
    if pos is not None:

        # LONG POSITION
        if pos["side"] == "long":

            chunks_up = int((price - pos["trailing_level"]) / chunk)
            if chunks_up > 0:
                pos["trailing_level"] += chunks_up * chunk
                pos["stop"] = max(pos["stop"], pos["trailing_level"] - sl_points)

            if price <= pos["stop"]:

                pnl = (price - pos["entry"]) * contract_size * contracts
                exit_fee = commission(price, contracts, symbol)
                entry_fee = pos.get("entry_fee", 0)
                net_pnl = pnl - entry_fee - exit_fee

                save_trade_row({
                    "symbol": symbol,
                    "side": "long",
                    "entry_time": pos["entry_time"],
                    "exit_time": datetime.now(),
                    "qty": contracts,
                    "entry": pos["entry"],
                    "exit": price,
                    "gross_pnl": round(pnl, 6),
                    "entry_fee": round(entry_fee, 6),
                    "commission_exit": round(exit_fee, 6),
                    "net_pnl": round(net_pnl, 6)
                })

                log(f"{symbol} | LONG EXIT | Exit: {price} | Net PnL: {net_pnl}")

                positions[symbol] = None
                last_base_price[symbol] = price
                trailing_level[symbol] = None

        # SHORT POSITION
        elif pos["side"] == "short":

            chunks_down = int((pos["trailing_level"] - price) / chunk)
            if chunks_down > 0:
                pos["trailing_level"] -= chunks_down * chunk
                pos["stop"] = min(pos["stop"], pos["trailing_level"] + sl_points)

            if price >= pos["stop"]:

                pnl = (pos["entry"] - price) * contract_size * contracts
                exit_fee = commission(price, contracts, symbol)
                entry_fee = pos.get("entry_fee", 0)
                net_pnl = pnl - entry_fee - exit_fee

                save_trade_row({
                    "symbol": symbol,
                    "side": "short",
                    "entry_time": pos["entry_time"],
                    "exit_time": datetime.now(),
                    "qty": contracts,
                    "entry": pos["entry"],
                    "exit": price,
                    "gross_pnl": round(pnl, 6),
                    "entry_fee": round(entry_fee, 6),
                    "commission_exit": round(exit_fee, 6),
                    "net_pnl": round(net_pnl, 6)
                })

                log(f"{symbol} | SHORT EXIT | Exit: {price} | Net PnL: {net_pnl}")

                positions[symbol] = None
                last_base_price[symbol] = price
                trailing_level[symbol] = None

# ================= MAIN LOOP ==============================
def run_live():
    positions = {s: None for s in SYMBOLS}
    last_base_price = {s: None for s in SYMBOLS}
    trailing_level = {s: None for s in SYMBOLS}

    log("🚀 Starting live Chunk-based Trailing Stop strategy with REAL entry price + entry+exit fee PnL")

    while True:
        try:
            for symbol in SYMBOLS:
                df = fetch_candles(symbol, resolution="5m", days=1)
                if df is None or len(df) < 20:
                    continue

                ha_df = calculate_ema(df, period=5)
                ema_value = ha_df["EMA"].iloc[-1]
                last_close = df["Close"].iloc[-1]
                prev_close = df["Close"].iloc[-2]
 
                save_processed_data(ha_df, symbol)

                price = fetch_ticker_price(symbol)
                if price is None:
                    continue

                process_price_trend(
                    symbol,
                    price,
                    positions,
                    last_base_price,
                    trailing_level,
                    ema_value,
                    last_close,
                    prev_close,
                    ha_df
                )

            time.sleep(60)

        except Exception as e:
            log(f"⚠️ Main loop error: {e}")
            time.sleep(10)

# ================= MAIN ENTRY ============================
if __name__ == "__main__":
    run_live()
