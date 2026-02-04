# =========================
# IMPORTS
# =========================
import requests, time
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from ta.volatility import AverageTrueRange

# =========================
# DATA FETCH
# =========================
def fetch_candles(symbol, interval="15m", years=5):
    url = "https://fapi.binance.com/fapi/v1/klines"
    limit = 1500

    end_time = int(datetime.utcnow().timestamp() * 1000)
    start_time = int((datetime.utcnow() - timedelta(days=365 * years)).timestamp() * 1000)

    dfs = []

    while end_time > start_time:
        params = {
            "symbol": symbol,
            "interval": interval,
            "limit": limit,
            "endTime": end_time
        }

        r = requests.get(url, params=params)
        r.raise_for_status()
        data = r.json()
        if not data:
            break

        df = pd.DataFrame(data, columns=[
            "Time","Open","High","Low","Close","Volume",
            "CloseTime","qv","n","tbb","tbq","ignore"
        ])

        df = df[["Time","Open","High","Low","Close","Volume"]]
        df["Time"] = pd.to_datetime(df["Time"], unit="ms", utc=True)
        df.set_index("Time", inplace=True)
        df = df.astype(float)

        dfs.append(df)
        end_time = int(df.index.min().timestamp() * 1000) - 1
        time.sleep(0.15)

    df = pd.concat(dfs).sort_index()
    return df[~df.index.duplicated()]

# =========================
# LOAD BTC DATA
# =========================
df = fetch_candles("BTCUSDT", "15m", 5)

# =========================
# INDICATORS
# =========================
ATR_PERIOD = 10
ATR_MA_PERIOD = 20
ST_MULT = 3

atr = AverageTrueRange(df["High"], df["Low"], df["Close"], ATR_PERIOD)
df["ATR"] = atr.average_true_range()
df["ATR_MA"] = df["ATR"].rolling(ATR_MA_PERIOD).mean()

hl2 = (df["High"] + df["Low"]) / 2
df["upper"] = hl2 + ST_MULT * df["ATR"]
df["lower"] = hl2 - ST_MULT * df["ATR"]

df["trend"] = 1
df["supertrend"] = np.nan

for i in range(1, len(df)):
    if df["Close"].iloc[i] > df["upper"].iloc[i - 1]:
        df.iloc[i, df.columns.get_loc("trend")] = 1
    elif df["Close"].iloc[i] < df["lower"].iloc[i - 1]:
        df.iloc[i, df.columns.get_loc("trend")] = -1
    else:
        df.iloc[i, df.columns.get_loc("trend")] = df["trend"].iloc[i - 1]
        if df["trend"].iloc[i] == 1:
            df.iloc[i, df.columns.get_loc("lower")] = max(
                df["lower"].iloc[i], df["lower"].iloc[i - 1]
            )
        else:
            df.iloc[i, df.columns.get_loc("upper")] = min(
                df["upper"].iloc[i], df["upper"].iloc[i - 1]
            )

    df.iloc[i, df.columns.get_loc("supertrend")] = (
        df["lower"].iloc[i] if df["trend"].iloc[i] == 1 else df["upper"].iloc[i]
    )

# =========================
# BACKTEST (SL = Supertrend)
# =========================
position = 0
entry = 0
sl = 0
trades = []

for i in range(1, len(df)):
    close = df["Close"].iloc[i]
    st = df["supertrend"].iloc[i]
    atr_ok = df["ATR"].iloc[i] > df["ATR_MA"].iloc[i]

    # ===== ENTRY =====
    if position == 0 and atr_ok:
        if close > st:
            position = 1
            entry = close
            sl = st

        elif close < st:
            position = -1
            entry = close
            sl = st

    # ===== LONG MANAGEMENT =====
    elif position == 1:
        sl = max(sl, st)  # 🔥 TRAILING SL

        if close < sl:
            pnl = (close - entry) / entry
            trades.append(pnl)
            position = 0

    # ===== SHORT MANAGEMENT =====
    elif position == -1:
        sl = min(sl, st)  # 🔥 TRAILING SL

        if close > sl:
            pnl = (entry - close) / entry
            trades.append(pnl)
            position = 0

# =========================
# PERFORMANCE METRICS
# =========================
trades = pd.Series(trades)
equity = (1 + trades).cumprod()
drawdown = equity / equity.cummax() - 1

print("\n===== PERFORMANCE =====")
print("Total Trades:", len(trades))
print("Win Rate %:", round((trades > 0).mean() * 100, 2))
print("Avg Profit:", round(trades[trades > 0].mean(), 4))
print("Avg Loss:", round(trades[trades < 0].mean(), 4))
print("Max Profit:", round(trades.max(), 4))
print("Max Loss:", round(trades.min(), 4))
print("Max Drawdown:", round(drawdown.min(), 4))
print("Expectancy:",
      round((trades.mean()), 4))
