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

DEFAULT_CONTRACTS = {"BTCUSD": 100, "ETHUSD": 100}
CONTRACT_SIZE = {"BTCUSD": 0.001, "ETHUSD": 0.01}
TAKER_FEE = 0.0005

TIMEFRAME = "15m"
DAYS = 5

# ===== RISK SETTINGS =====
TAKE_PROFIT = {"BTCUSD": 300, "ETHUSD": 30}
STOP_LOSS = {"BTCUSD": 300, "ETHUSD": 20}
TRAIL_STEP = {"BTCUSD": 100, "ETHUSD": 10}

BASE_DIR = os.getcwd()
SAVE_DIR = os.path.join(BASE_DIR, "data", "price_trend_following_strategy")
os.makedirs(SAVE_DIR, exist_ok=True)

TRADE_CSV = os.path.join(SAVE_DIR, "live_trad.csv")


# ================= UTILITIES =================
def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}")


def commission(price, qty, symbol):
    notional = price * CONTRACT_SIZE[symbol] * qty
    return notional * TAKER_FEE


# ===== INTEGRAL CALCULATOR HANDLE =====
def calc_integral(series, method="trapz"):
    x = np.arange(len(series))
    y = np.nan_to_num(series.values)

    if method == "trapz":
        return np.trapz(y, x)

    raise ValueError("Unknown integration method")


def save_trade(trade):
    trade = trade.copy()

    for col in ["entry_time", "exit_time"]:
        if col in trade and isinstance(trade[col], datetime):
            trade[col] = trade[col].strftime("%Y-%m-%d %H:%M:%S")

    cols = ["entry_time","exit_time","symbol","side",
            "entry_price","exit_price","qty","net_pnl"]

    pd.DataFrame([trade])[cols].to_csv(
        TRADE_CSV,
        mode="a",
        header=not os.path.exists(TRADE_CSV),
        index=False
    )


def save_processed_data(df, data, symbol):
    path = os.path.join(SAVE_DIR, f"{symbol}_processed.csv")

    out = pd.DataFrame({
        "time": df.index,

        # Heikin Ashi
        "HA_open": data["HA_open"],
        "HA_high": data["HA_high"],
        "HA_low": data["HA_low"],
        "HA_close": data["HA_close"],

        "smoothed_high": data["smoothed_high"],
        "smoothed_low": data["smoothed_low"],
        "trendline": data["Trendline"],

        # HA based indicators
        "ATR_HA": data["ATR_HA"],
        "ATR_MA_HA": data["ATR_MA_HA"],

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


def fetch_candles(symbol, resolution=TIMEFRAME, days=DAYS, tz="Asia/Kolkata"):
    start = int((datetime.now() - timedelta(days=days)).timestamp())

    params = {
        "resolution": resolution,
        "symbol": symbol,
        "start": str(start),
        "end": str(int(time.time()))
    }

    try:
        r = requests.get(
            "https://api.india.delta.exchange/v2/history/candles",
            params=params,
            timeout=10
        )
        r.raise_for_status()

        df = pd.DataFrame(
            r.json()["result"],
            columns=["time", "open", "high", "low", "close", "volume"]
        )

        df.rename(columns=str.title, inplace=True)

        df["Time"] = pd.to_datetime(df["Time"], unit="s", utc=True).dt.tz_convert(tz)
        df.set_index("Time", inplace=True)
        df.sort_index(inplace=True)

        for col in ["Open", "High", "Low", "Close", "Volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")

        return df.dropna()

    except Exception as e:
        log(f"{symbol} fetch error: {e}")
        return None


# ================= HEIKIN ASHI TRENDLINE =================
def calculate_trendline(df):
    # ----- Build HA -----
    ha = ta.ha(df["Open"], df["High"], df["Low"], df["Close"]).reset_index(drop=True)

    data = df.copy().reset_index(drop=True)

    data["HA_open"]  = ha["HA_open"]
    data["HA_high"]  = ha["HA_high"]
    data["HA_low"]   = ha["HA_low"]
    data["HA_close"] = ha["HA_close"]

    # ----- Smooth HA -----
    data["high_smooth"] = ta.ema(data["HA_high"], length=5)
    data["low_smooth"]  = ta.ema(data["HA_low"], length=5)

    high_vals = data["high_smooth"].values
    low_vals  = data["low_smooth"].values

    max_idx = argrelextrema(high_vals, np.greater_equal, order=42)[0]
    min_idx = argrelextrema(low_vals,  np.less_equal,    order=42)[0]

    data["smoothed_high"] = np.nan
    data["smoothed_low"]  = np.nan

    data.iloc[max_idx, data.columns.get_loc("smoothed_high")] = data["HA_high"].iloc[max_idx]
    data.iloc[min_idx, data.columns.get_loc("smoothed_low")]  = data["HA_low"].iloc[min_idx]

    data[["smoothed_high","smoothed_low"]] = data[["smoothed_high","smoothed_low"]].ffill()

    # ----- TRENDLINE FROM HA -----
    data["Trendline"] = np.nan
    trendline = data["HA_close"].iloc[0]

    data.iloc[0, data.columns.get_loc("Trendline")] = trendline

    for i in range(1, len(data)):

        if data["HA_high"].iloc[i] == data["smoothed_high"].iloc[i]:
            trendline = data["HA_low"].iloc[i]

        elif data["HA_low"].iloc[i] == data["smoothed_low"].iloc[i]:
            trendline = data["HA_high"].iloc[i]

        data.loc[i, "Trendline"] = trendline

    data.index = df.index
    return data


# ================= STRATEGY =================
def process_symbol(symbol, df, price, state):

    # ----- HA TRENDLINE + BASE -----
    data = calculate_trendline(df)

    # ----- ALL INDICATORS ON HA -----

    # ATR from HA candles
    data["ATR_HA"] = ta.atr(
        data["HA_high"],
        data["HA_low"],
        data["HA_close"],
        length=14
    )

    data["ATR_MA_HA"] = data["ATR_HA"].rolling(21).mean()

    save_processed_data(df, data, symbol)

    last = data.iloc[-2]
    prev = data.iloc[-3]

    pos = state["position"]
    now = datetime.now()

    # ===== ENTRY (HA ONLY) =====
    if (
        now.minute % 15 == 0
        and pos is None
        and last.ATR_HA > prev.ATR_MA_HA
    ):

        entry_time = datetime.now()

        # ----- LONG -----
        if last.HA_close > last.Trendline and last.HA_close > prev.HA_close and last.HA_close > prev.HA_open:

            state["position"] = {
                "side": "long",
                "entry": price,
                "stop": price - STOP_LOSS[symbol],
                "tp": price + TAKE_PROFIT[symbol],
                "trail_base": price,
                "qty": DEFAULT_CONTRACTS[symbol],
                "entry_time": entry_time
            }

            log(f"{symbol} LONG ENTRY @ {price}")
            return

        # ----- SHORT -----
        if last.HA_close < last.Trendline and last.HA_close < prev.HA_close and last.HA_close < prev.HA_open:

            state["position"] = {
                "side": "short",
                "entry": price,
                "stop": price + STOP_LOSS[symbol],
                "tp": price - TAKE_PROFIT[symbol],
                "trail_base": price,
                "qty": DEFAULT_CONTRACTS[symbol],
                "entry_time": entry_time
            }

            log(f"{symbol} SHORT ENTRY @ {price}")
            return

    # ===== MANAGEMENT =====
    if pos:
        side = pos["side"]
        step = TRAIL_STEP[symbol]

        # if side == "long":
        #     move = last.HA_close - pos["trail_base"]
        #     if move >= step:
        #         steps = int(move // step)
        #         pos["stop"] += steps * step
        #         pos["trail_base"] += steps * step

        # if side == "short":
        #     move = pos["trail_base"] - last.HA_close
        #     if move >= step:
        #         steps = int(move // step)
        #         pos["stop"] -= steps * step
        #         pos["trail_base"] -= steps * step

        exit_trade = False
        pnl = 0

        if side == "long" and (price < pos["stop"] or last.HA_close < last.Trendline):
            pnl = (price - pos["entry"]) * CONTRACT_SIZE[symbol] * pos["qty"]
            exit_trade = True

        if side == "short" and (price > pos["stop"] or last.HA_close > last.Trendline):
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

            log(f"{symbol} {side.upper()} EXIT @ {price} | NET {round(net,6)}")
            state["position"] = None


# ================= MAIN =================
def run():
    if not os.path.exists(TRADE_CSV):
        pd.DataFrame(columns=[
            "entry_time","exit_time","symbol","side",
            "entry_price","exit_price","qty","net_pnl"
        ]).to_csv(TRADE_CSV, index=False)

    state = {s: {"position": None} for s in SYMBOLS}

    log("FULL HEIKIN ASHI ENGINE STARTED")

    while True:
        try:
            for symbol in SYMBOLS:

                df = fetch_candles(symbol)
                if df is None or len(df) < 100:
                    continue

                price = fetch_price(symbol)
                if price is None:
                    continue

                process_symbol(symbol, df, price, state[symbol])

            time.sleep(20)

        except Exception as e:
            log(f"Runtime error: {e}")
            time.sleep(5)


if __name__ == "__main__":
    run()
