import os
import time
import requests
import pandas as pd
from datetime import datetime, timedelta
import pandas_ta as ta

# ================= SETTINGS =================
SYMBOLS = ["BTCUSD", "ETHUSD"]

DEFAULT_CONTRACTS = {"BTCUSD": 30, "ETHUSD": 100}
CONTRACT_SIZE = {"BTCUSD": 0.001, "ETHUSD": 0.01}
MAX_SL = {"BTCUSD": 500, "ETHUSD": 30}
TAKER_FEE = 0.0005

EMA_OFFSET = {
    "BTCUSD": 200,
    "ETHUSD": 10
}

BASE_DIR = os.getcwd()
SAVE_DIR = os.path.join(BASE_DIR, "data", "price_ema_strategy")
os.makedirs(SAVE_DIR, exist_ok=True)

TRADE_CSV = os.path.join(SAVE_DIR, "live_trades.csv")

TRADE_COLUMNS = [
    "symbol",
    "side",
    "entry_time",
    "exit_time",
    "qty",
    "entry",
    "exit",
    "gross_pnl",
    "commission",
    "net_pnl"
]

# ================= UTILITIES =================
def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}")

def commission(price, qty, symbol):
    notional = price * CONTRACT_SIZE[symbol] * qty
    return notional * TAKER_FEE

def save_trade_row(trade):
    df = pd.DataFrame([trade])
    df = df.reindex(columns=TRADE_COLUMNS)
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
        "EMA9": ha["EMA9"],
        "EMA_UPPER": ha["EMA_UPPER"],
        "EMA_LOWER": ha["EMA_LOWER"]
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
    except Exception as e:
        log(f"Price fetch error {symbol}: {e}")
        return None

def fetch_candles(symbol, resolution="1h", days=30, tz='Asia/Kolkata'):
    start = int((datetime.now() - timedelta(days=days)).timestamp())
    params = {
        'resolution': resolution,
        'symbol': symbol,
        'start': str(start),
        'end': str(int(time.time())),
    }
    url = 'https://api.india.delta.exchange/v2/history/candles'

    try:
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json().get("result", [])

        if not data:
            log(f"No candle data for {symbol}")
            return None

        df = pd.DataFrame(
            data,
            columns=["time", "open", "high", "low", "close", "volume"]
        )
        df.rename(columns=str.title, inplace=True)

        df["Time"] = pd.to_datetime(df["Time"], unit="s", utc=True)
        df["Time"] = df["Time"].dt.tz_convert(tz)

        df.set_index("Time", inplace=True)
        df.sort_index(inplace=True)
        df = df[~df.index.duplicated(keep="last")]

        for col in ["Open", "High", "Low", "Close", "Volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")

        return df

    except Exception as e:
        log(f"Candle fetch error {symbol}: {e}")
        return None

# ================= EMA + HA =================
def calculate_ema_bands(df, symbol):
    ha = ta.ha(
        open_=df["Open"],
        high=df["High"],
        low=df["Low"],
        close=df["Close"]
    )

    ha["EMA9"] = ta.ema(ha["HA_close"], length=5)
    offset = EMA_OFFSET[symbol]

    ha["EMA_UPPER"] = ha["EMA9"] + offset
    ha["EMA_LOWER"] = ha["EMA9"] - offset

    return ha

# ================= STRATEGY =================
def process_symbol(symbol, df, price, state):
    ha = calculate_ema_bands(df, symbol)

    # Merge EMA values back into df
    df = df.copy()
    df["EMA9"] = ha["EMA9"]
    df["EMA_UPPER"] = ha["EMA_UPPER"]
    df["EMA_LOWER"] = ha["EMA_LOWER"]

    save_processed_data(df, ha, symbol)

    last = df.iloc[-2]
    candle_time = df.index[-1]

    pos = state["position"]

    # ========= ENTRY =========
    if pos is None:

        # LONG
        if last.Close > last.EMA_UPPER:
            state["position"] = {
                "side": "long",
                "entry": price,
                "stop": min(last.EMA_LOWER, price - MAX_SL[symbol]),
                "qty": DEFAULT_CONTRACTS[symbol],
                "entry_time": candle_time
            }
            log(f"{symbol} | LONG ENTRY @ {price}")
            return

        # SHORT
        if last.Close < last.EMA_LOWER:
            state["position"] = {
                "side": "short",
                "entry": price,
                "stop": max(last.EMA_UPPER, price + MAX_SL[symbol]),
                "qty": DEFAULT_CONTRACTS[symbol],
                "entry_time": candle_time
            }
            log(f"{symbol} | SHORT ENTRY @ {price}")
            return

    # ========= MANAGEMENT =========
    if pos:
        side = pos["side"]

        if side == "long":
            pos["stop"] = max(pos["stop"], last.EMA9)
            exit_trade = last.Close < pos["stop"]
            pnl = (price - pos["entry"]) * CONTRACT_SIZE[symbol] * pos["qty"]

        else:
            pos["stop"] = min(pos["stop"], last.EMA9)
            exit_trade = last.Close > pos["stop"]
            pnl = (pos["entry"] - price) * CONTRACT_SIZE[symbol] * pos["qty"]

        if exit_trade:
            fee = commission(price, pos["qty"], symbol)
            net = pnl - fee

            save_trade_row({
                "symbol": symbol,
                "side": side,
                "entry_time": pos["entry_time"],
                "exit_time": datetime.now(),
                "qty": pos["qty"],
                "entry": pos["entry"],
                "exit": price,
                "gross_pnl": round(pnl, 6),
                "commission": round(fee, 6),
                "net_pnl": round(net, 6)
            })

            log(f"{symbol} | {side.upper()} EXIT @ {price} | NET: {round(net, 6)}")
            state["position"] = None

# ================= MAIN =================
def run():
    if not os.path.exists(TRADE_CSV):
        pd.DataFrame(columns=TRADE_COLUMNS).to_csv(TRADE_CSV, index=False)

    state = {s: {"position": None, "last_trade_time": None} for s in SYMBOLS}

    log("🚀 EMA-9 OFFSET STRATEGY STARTED")

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

            time.sleep(30)

        except Exception as e:
            log(f"Runtime error: {e}")
            time.sleep(5)

if __name__ == "__main__":
    run()
