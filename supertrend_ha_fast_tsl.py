import os
import time
import pandas as pd
import numpy as np
from datetime import datetime
import pytz
import pandas_ta as ta
import subprocess
from dotenv import load_dotenv
from utils import TradingUtils

load_dotenv()

# ================= CONFIG =================

BOT_NAME = "supertrend_ha_fast_test"

SYMBOLS = ["BTCUSD"]

CONTRACT_SIZE = {"BTCUSD": 0.001}
QTY = {"BTCUSD": 100}

STOPLOSS = {"BTCUSD": 500}

# ================= TARGET =================

TARGET = {
    "BTCUSD": 300
}

TAKER_FEE = 0.0005
SLEEP_TIME = 5

SAVE_DIR = "data/supertrend_ha_fast_test"
os.makedirs(SAVE_DIR, exist_ok=True)

IST = pytz.timezone("Asia/Kolkata")
last_git_push = time.time()

LOOKBACK = 3

# ================= AUTO GIT =================

def auto_git_push():
    global last_git_push

    if time.time() - last_git_push < 3600:
        return

    try:
        subprocess.run("git add -A", shell=True)

        res = subprocess.run(
            'git diff --cached --quiet || git commit -m "auto update"',
            shell=True
        )

        if res.returncode != 0:
            utils.log("✅ Changes committed")

        subprocess.run("git push origin main", shell=True)

        last_git_push = time.time()

    except Exception as e:
        utils.log(f"Git Error: {e}")

# ================= TIME =================

def get_ist_time():
    return datetime.now(IST)

# ================= NEW CANDLE =================

last_candle_time = {}

def is_new_candle(symbol, df):
    t = df.index[-1]

    if symbol not in last_candle_time:
        last_candle_time[symbol] = t
        return True

    if t != last_candle_time[symbol]:
        last_candle_time[symbol] = t
        return True

    return False

# ================= SAFE FETCH =================

def safe_fetch(fetch_func, *args, retries=3, delay=1):
    for _ in range(retries):
        try:
            result = fetch_func(*args)

            if result is not None:
                return result

        except Exception as e:
            print("Fetch error:", e)

        time.sleep(delay)

    return None

# ================= SAVE =================

def save_processed_data(df, symbol):

    path = os.path.join(SAVE_DIR, f"{symbol}_processed.csv")

    out = pd.DataFrame({
        "time": df.index,
        "ha_open": df["HA_open"],
        "ha_high": df["HA_high"],
        "ha_low": df["HA_low"],
        "ha_close": df["HA_close"],
        "supertrend": df["supertrend"],
        "trend": df["trend"],
        "vwap": df["vwap"],
        "ema200": df["ema200"]
    })

    out.to_csv(path, index=False)

# ================= HEIKIN ASHI =================

def add_heikin_ashi(df):

    ha_close = (
        df["Open"] +
        df["High"] +
        df["Low"] +
        df["Close"]
    ) / 4

    ha_open = np.zeros(len(df))

    ha_open[0] = df["Open"].iloc[0]

    for i in range(1, len(df)):
        ha_open[i] = (
            ha_open[i - 1] +
            ha_close.iloc[i - 1]
        ) / 2

    df["HA_open"] = ha_open

    df["HA_high"] = np.maximum.reduce([
        df["High"],
        ha_open,
        ha_close
    ])

    df["HA_low"] = np.minimum.reduce([
        df["Low"],
        ha_open,
        ha_close
    ])

    df["HA_close"] = ha_close

    return df

# ================= INDICATORS =================

def add_indicators(df):

    df = df.tail(200)

    df = add_heikin_ashi(df)

    # ================= SUPERTREND =================

    st = ta.supertrend(
        high=df["HA_high"],
        low=df["HA_low"],
        close=df["HA_close"],
        length=10,
        multiplier=3
    )

    supertrend_col = [
        c for c in st.columns
        if "SUPERT_" in c and not c.endswith("d")
    ][0]

    trend_col = [
        c for c in st.columns
        if "SUPERTd_" in c
    ][0]

    df["supertrend"] = st[supertrend_col]
    df["trend"] = st[trend_col]

    # ================= ATR =================

    df["atr"] = ta.atr(
        df["HA_high"],
        df["HA_low"],
        df["HA_close"],
        length=10
    )

    df["atr_ma"] = df["atr"].rolling(20).mean()

    df["trend_strength"] = (
        abs(df["HA_close"] - df["supertrend"])
        / df["atr"]
    )

    # ================= VWAP =================

    df["vwap"] = ta.vwap(
        high=df["High"],
        low=df["Low"],
        close=df["Close"],
        volume=df["Volume"]
    )

    # ================= EMA 200 =================

    df["ema200"] = ta.ema(
        df["HA_close"],
        length=200
    )

    return df.dropna()

# ================= LAST CROSSOVER =================

def get_last_crossover(df, lookback=50):

    trend = df["trend"].values

    for i in range(
        len(trend) - 1,
        max(1, len(trend) - lookback),
        -1
    ):

        if trend[i] != trend[i - 1]:
            return i, trend[i]

    return None, None

# ================= STRATEGY =================

def process_symbol(symbol, df, price, state):

    sym = state["symbols"][symbol]

    positions = sym["positions"]

    qty = QTY[symbol]

    df = add_indicators(df)

    if len(df) < 30:
        return

    curr = df.iloc[-1]
    last = df.iloc[-2]

    # ================= LEVEL INIT =================

    if "level" not in sym:

        sym["level"] = {
            "high": None,
            "low": None,
            "locked": False,
            "side": None,
            "attempted": False,
            "last_cross_idx": None
        }

    level = sym["level"]

    # ================= RECENT CROSSOVER =================

    idx, trend_dir = get_last_crossover(df)

    if idx is not None and idx != level["last_cross_idx"]:

        level["last_cross_idx"] = idx

        if trend_dir == 1:

            level_high = df["HA_high"].iloc[
                idx: idx + LOOKBACK
            ].max()

            level.update({
                "high": level_high,
                "low": None,
                "locked": True,
                "side": "long",
                "attempted": False
            })

        elif trend_dir == -1:

            level_low = df["HA_low"].iloc[
                idx: idx + LOOKBACK
            ].min()

            level.update({
                "low": level_low,
                "high": None,
                "locked": True,
                "side": "short",
                "attempted": False
            })

    close = curr["HA_close"]
    last_close = last["HA_close"]

    # ================= NEW FILTER LOGIC =================

    bullish_filter = (
        close > curr["vwap"] and
        close > curr["ema200"]
    )

    bearish_filter = (
        close < curr["vwap"] and
        close < curr["ema200"] 
    )

    # ================= ENTRY =================

    if level["locked"] and not level["attempted"]:

        # LONG

        if (
            level["side"] == "long"
            and close > level["high"]
            and bullish_filter
        ):

            if not any(
                p["side"] == "long"
                for p in positions
            ):

                sl = price - STOPLOSS[symbol]
                target = price + TARGET[symbol]

                positions.append({
                    "side": "long",
                    "entry": price,
                    "qty": qty,
                    "trail_sl": sl,
                    "target": target,
                    "entry_time": get_ist_time()
                })

                level["attempted"] = True
                level["locked"] = False

                utils.log(
                    f"🚀 {symbol} LONG ENTRY @ {price} | TARGET: {target}",
                    tg=True
                )

        # SHORT

        elif (
            level["side"] == "short"
            and close < level["low"]
            and bearish_filter
        ):

            if not any(
                p["side"] == "short"
                for p in positions
            ):

                sl = price + STOPLOSS[symbol]
                target = price - TARGET[symbol]

                positions.append({
                    "side": "short",
                    "entry": price,
                    "qty": qty,
                    "trail_sl": sl,
                    "target": target,
                    "entry_time": get_ist_time()
                })

                level["attempted"] = True
                level["locked"] = False

                utils.log(
                    f"🔻 {symbol} SHORT ENTRY @ {price} | TARGET: {target}",
                    tg=True
                )

    # ================= TRAILING =================

    for p in positions[:]:

        # LONG

        if p["side"] == "long":

            p["trail_sl"] = max(
                p["trail_sl"],
                curr["supertrend"]
            )

        # SHORT

        else:

            p["trail_sl"] = min(
                p["trail_sl"],
                curr["supertrend"]
            )

# ================= UTILS =================

utils = TradingUtils(
    contract_size=CONTRACT_SIZE,
    taker_fee=TAKER_FEE,
    timeframe="5m",
    days=5,
    telegram_token=os.getenv("testmyaglostrategy_bot"),
    telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID"),
    bot_name=BOT_NAME
)

# ================= MAIN =================

def run():

    state = {
        "balance": 10000,
        "symbols": {s: {"positions": []} for s in SYMBOLS}
    }

    utils.log("🚀 BOT STARTED", tg=True)

    while True:

        try:

            for symbol in SYMBOLS:

                # ================= FETCH LIVE PRICE =================

                price = safe_fetch(
                    utils.fetch_price,
                    symbol
                )

                if price is None:
                    continue

                sym = state["symbols"][symbol]

                positions = sym["positions"]

                # ================= FAST EXIT =================

                for p in positions[:]:

                    # LONG EXIT

                    if p["side"] == "long":

                        # STOPLOSS EXIT
                        if price <= p["trail_sl"]:

                            exit_reason = "SL"

                            pnl = (
                                (price - p["entry"])
                                * CONTRACT_SIZE[symbol]
                                * p["qty"]
                            )

                        # TARGET EXIT
                        elif price >= p["target"]:

                            exit_reason = "TARGET"

                            pnl = (
                                (price - p["entry"])
                                * CONTRACT_SIZE[symbol]
                                * p["qty"]
                            )

                        else:
                            continue

                    # SHORT EXIT

                    else:

                        # STOPLOSS EXIT
                        if price >= p["trail_sl"]:

                            exit_reason = "SL"

                            pnl = (
                                (p["entry"] - price)
                                * CONTRACT_SIZE[symbol]
                                * p["qty"]
                            )

                        # TARGET EXIT
                        elif price <= p["target"]:

                            exit_reason = "TARGET"

                            pnl = (
                                (p["entry"] - price)
                                * CONTRACT_SIZE[symbol]
                                * p["qty"]
                            )

                        else:
                            continue

                    fee = (
                        utils.commission(
                            p["entry"],
                            p["qty"],
                            symbol
                        )
                        +
                        utils.commission(
                            price,
                            p["qty"],
                            symbol
                        )
                    )

                    net = pnl - fee

                    state["balance"] += net

                    now = get_ist_time()

                    emoji = "🟢" if net > 0 else "🔴"

                    utils.log(
                        f"{emoji} {symbol} EXIT ({exit_reason}) @ {price} | PNL: {round(net,6)}",
                        tg=True
                    )

                    utils.save_trade({
                        "symbol": symbol,
                        "side": p["side"],
                        "entry_price": p["entry"],
                        "exit_price": price,
                        "qty": p["qty"],
                        "net_pnl": round(net, 6),
                        "entry_time": p.get("entry_time"),
                        "exit_time": now
                    })

                    positions.remove(p)

                # ================= CANDLE LOGIC =================

                df = safe_fetch(
                    utils.fetch_candles,
                    symbol,
                    "5m"
                )

                if df is None or df.empty:
                    continue

                if not is_new_candle(symbol, df):
                    continue

                process_symbol(
                    symbol,
                    df,
                    price,
                    state
                )

                # auto_git_push()

            time.sleep(SLEEP_TIME)

        except Exception as e:

            print("ERROR:", e)

            time.sleep(5)

# ================= START =================

if __name__ == "__main__":
    run()