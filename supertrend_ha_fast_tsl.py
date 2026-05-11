import os
import time
import pandas as pd
import numpy as np
from datetime import datetime
import pytz
import pandas_ta as ta
from dotenv import load_dotenv
from utils import TradingUtils

load_dotenv()

# ================= CONFIG =================

BOT_NAME = "supertrend_ha_fast_tsl"

SYMBOLS = ["BTCUSD", "ETHUSD"]

CONTRACT_SIZE = {
    "BTCUSD": 0.001,
    "ETHUSD": 0.01
}

QTY = {
    "BTCUSD": 100,
    "ETHUSD": 100
}

STOPLOSS = {
    "BTCUSD": 200,
    "ETHUSD": 10
}

TARGET_PROFIT = {
    "BTCUSD": 300,
    "ETHUSD": 10
}

TAKER_FEE = 0.0005
SLEEP_TIME = 5

SAVE_DIR = "data/supertrend_ha_fast_tsl"
os.makedirs(SAVE_DIR, exist_ok=True)

IST = pytz.timezone("Asia/Kolkata")

LOOKBACK = 3

# ================= ADX FILTER =================

ADX_LENGTH = 14
ADX_THRESHOLD = 30

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

    path = os.path.join(
        SAVE_DIR,
        f"{symbol}_processed.csv"
    )

    out = pd.DataFrame({
        "time": df.index,
        "ha_open": df["HA_open"],
        "ha_high": df["HA_high"],
        "ha_low": df["HA_low"],
        "ha_close": df["HA_close"],
        "supertrend": df["supertrend"],
        "trend": df["trend"],
        "adx": df["adx"]
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

    # ================= ADX =================

    adx = ta.adx(
        high=df["HA_high"],
        low=df["HA_low"],
        close=df["HA_close"],
        length=ADX_LENGTH
    )

    adx_col = [
        c for c in adx.columns
        if f"ADX_{ADX_LENGTH}" in c
    ][0]

    df["adx"] = adx[adx_col]

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

    adx_value = curr["adx"]

    # ================= LEVEL INIT =================

    if "level" not in sym:

        sym["level"] = {
            "high": None,
            "low": None,
            "locked": False,
            "side": None,
            "used": False,
            "last_cross_idx": None
        }

    level = sym["level"]

    # ================= RECENT CROSSOVER =================

    idx, trend_dir = get_last_crossover(df)

    if idx is not None and idx != level["last_cross_idx"]:

        level["last_cross_idx"] = idx

        # ================= LONG LEVEL =================

        if trend_dir == 1:

            level_high = df["HA_high"].iloc[
                idx: idx + LOOKBACK
            ].max()

            level.update({
                "high": level_high,
                "low": None,
                "locked": True,
                "side": "long",
                "used": False
            })

            print(f"{symbol} NEW LONG LEVEL => {level_high}")

        # ================= SHORT LEVEL =================

        elif trend_dir == -1:

            level_low = df["HA_low"].iloc[
                idx: idx + LOOKBACK
            ].min()

            level.update({
                "low": level_low,
                "high": None,
                "locked": True,
                "side": "short",
                "used": False
            })

            print(f"{symbol} NEW SHORT LEVEL => {level_low}")

    close = curr["HA_close"]

    # ================= ENTRY =================

    if level["locked"] and not level["used"]:

        # ================= ADX FILTER =================

        # if adx_value < ADX_THRESHOLD:

        #     print(
        #         f"{symbol} ADX LOW => {round(adx_value,2)}"
        #     )

        #     return

        # ================= LONG ENTRY =================

        if (
            level["side"] == "long"
            and close > level["high"]
        ):

            if not any(
                p["side"] == "long"
                for p in positions
            ):

                sl = price - STOPLOSS[symbol]
                tp = price + TARGET_PROFIT[symbol]

                positions.append({
                    "side": "long",
                    "entry": price,
                    "qty": qty,
                    "trail_sl": sl,
                    "target": tp,
                    "entry_time": get_ist_time()
                })

                # ONLY ONE ENTRY PER LEVEL
                level["used"] = True
                level["locked"] = False

                utils.log(
                    f"🚀 {symbol} LONG ENTRY @ {price} | TP: {tp} | ADX: {round(adx_value,2)}",
                    tg=True
                )

        # ================= SHORT ENTRY =================

        elif (
            level["side"] == "short"
            and close < level["low"]
        ):

            if not any(
                p["side"] == "short"
                for p in positions
            ):

                sl = price + STOPLOSS[symbol]
                tp = price - TARGET_PROFIT[symbol]

                positions.append({
                    "side": "short",
                    "entry": price,
                    "qty": qty,
                    "trail_sl": sl,
                    "target": tp,
                    "entry_time": get_ist_time()
                })

                # ONLY ONE ENTRY PER LEVEL
                level["used"] = True
                level["locked"] = False

                utils.log(
                    f"🔻 {symbol} SHORT ENTRY @ {price} | TP: {tp} | ADX: {round(adx_value,2)}",
                    tg=True
                )

    # ================= TRAILING STOP =================

    for p in positions[:]:

        # ================= LONG =================

        if p["side"] == "long":

            p["trail_sl"] = max(
                p["trail_sl"],
                curr["supertrend"]
            )

        # ================= SHORT =================

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
        "symbols": {
            s: {"positions": []}
            for s in SYMBOLS
        }
    }

    utils.log("🚀 BOT STARTED", tg=True)

    while True:

        try:

            for symbol in SYMBOLS:

                # ================= LIVE PRICE =================

                price = safe_fetch(
                    utils.fetch_price,
                    symbol
                )

                if price is None:
                    continue

                sym = state["symbols"][symbol]

                positions = sym["positions"]

                # ================= EXIT LOGIC =================

                for p in positions[:]:

                    exit_reason = None

                    # ================= LONG EXIT =================

                    if p["side"] == "long":

                        if price >= p["target"]:

                            pnl = (
                                (p["target"] - p["entry"])
                                * CONTRACT_SIZE[symbol]
                                * p["qty"]
                            )

                            exit_price = p["target"]
                            exit_reason = "TARGET"

                        elif price <= p["trail_sl"]:

                            pnl = (
                                (price - p["entry"])
                                * CONTRACT_SIZE[symbol]
                                * p["qty"]
                            )

                            exit_price = price
                            exit_reason = "SL"

                        else:
                            continue

                    # ================= SHORT EXIT =================

                    else:

                        if price <= p["target"]:

                            pnl = (
                                (p["entry"] - p["target"])
                                * CONTRACT_SIZE[symbol]
                                * p["qty"]
                            )

                            exit_price = p["target"]
                            exit_reason = "TARGET"

                        elif price >= p["trail_sl"]:

                            pnl = (
                                (p["entry"] - price)
                                * CONTRACT_SIZE[symbol]
                                * p["qty"]
                            )

                            exit_price = price
                            exit_reason = "SL"

                        else:
                            continue

                    # ================= FEES =================

                    fee = (
                        utils.commission(
                            p["entry"],
                            p["qty"],
                            symbol
                        )
                        +
                        utils.commission(
                            exit_price,
                            p["qty"],
                            symbol
                        )
                    )

                    net = pnl - fee

                    state["balance"] += net

                    now = get_ist_time()

                    emoji = "🟢" if net > 0 else "🔴"

                    utils.log(
                        f"{emoji} {symbol} EXIT ({exit_reason}) @ {exit_price} | PNL: {round(net,6)}",
                        tg=True
                    )

                    utils.save_trade({
                        "symbol": symbol,
                        "side": p["side"],
                        "entry_price": p["entry"],
                        "exit_price": exit_price,
                        "qty": p["qty"],
                        "net_pnl": round(net, 6),
                        "entry_time": p.get("entry_time"),
                        "exit_time": now
                    })

                    positions.remove(p)

                # ================= FETCH CANDLES =================

                df = safe_fetch(
                    utils.fetch_candles,
                    symbol,
                    "5m"
                )

                if df is None or df.empty:
                    continue

                # ================= NEW CANDLE ONLY =================

                if not is_new_candle(symbol, df):
                    continue

                # ================= STRATEGY =================

                process_symbol(
                    symbol,
                    df,
                    price,
                    state
                )

            time.sleep(SLEEP_TIME)

        except Exception as e:

            print("ERROR:", e)

            time.sleep(5)

# ================= START =================

if __name__ == "__main__":
    run()