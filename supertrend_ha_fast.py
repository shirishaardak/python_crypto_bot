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

BOT_NAME = "supertrend_ha_fast"

SYMBOLS = ["BTCUSD"]

CONTRACT_SIZE = {"BTCUSD": 0.001}
QTY = {"BTCUSD": 1000}

# ================= RISK SETTINGS =================

# INITIAL STOP LOSS
STOPLOSS = {"BTCUSD": 300}

# TARGET
TP = {"BTCUSD": 300}

# TRAILING STEP SIZE
TRAIL_STEP = 100

TAKER_FEE = 0.0005
SLEEP_TIME = 5

SAVE_DIR = "data/supertrend_ha_fast"
os.makedirs(SAVE_DIR, exist_ok=True)

IST = pytz.timezone("Asia/Kolkata")
last_git_push = time.time()

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
        "trend": df["trend"]
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

    # STOP NEW ENTRIES IF DAILY TARGET HIT

    if not state["trading_enabled"]:
        return

    sym = state["symbols"][symbol]

    positions = sym["positions"]

    qty = QTY[symbol]

    df = add_indicators(df)

    if len(df) < 30:
        return

    curr = df.iloc[-2]

    # ================= LEVEL INIT =================

    if "level" not in sym:

        sym["level"] = {
            "high": None,
            "low": None,
            "side": None,
            "used": False,
            "last_cross_idx": None
        }

    level = sym["level"]

    # ================= NEW CROSSOVER =================

    idx, trend_dir = get_last_crossover(df)

    if idx is not None and idx != level["last_cross_idx"]:

        level["last_cross_idx"] = idx

        # RESET ENTRY FLAG
        level["used"] = False

        # ================= LONG CROSSOVER =================

        if trend_dir == 1:

            crossover_high = df["HA_high"].iloc[idx]

            level.update({
                "high": crossover_high,
                "low": None,
                "side": "long"
            })

            utils.log(
                f"🟢 LONG LEVEL SET -> HIGH: {round(crossover_high, 2)}"
            )

        # ================= SHORT CROSSOVER =================

        elif trend_dir == -1:

            crossover_low = df["HA_low"].iloc[idx]

            level.update({
                "low": crossover_low,
                "high": None,
                "side": "short"
            })

            utils.log(
                f"🔴 SHORT LEVEL SET -> LOW: {round(crossover_low, 2)}"
            )

    close = curr["HA_close"]

    # ==================================================
    # ENTRY LOGIC
    # ==================================================

    if not level["used"]:

        # ================= LONG ENTRY =================

        if (
            level["side"] == "long"
            and level["high"] is not None
            and close > level["high"]
        ):

            if not any(
                p["side"] == "long"
                for p in positions
            ):

                sl = price - STOPLOSS[symbol]

                positions.append({
                    "side": "long",
                    "entry": price,
                    "qty": qty,
                    "trail_sl": sl,
                    "entry_time": get_ist_time()
                })

                level["used"] = True

                utils.log(
                    f"🚀 {symbol} LONG ENTRY @ {price}",
                    tg=True
                )

        # ================= SHORT ENTRY =================

        elif (
            level["side"] == "short"
            and level["low"] is not None
            and close < level["low"]
        ):

            if not any(
                p["side"] == "short"
                for p in positions
            ):

                sl = price + STOPLOSS[symbol]

                positions.append({
                    "side": "short",
                    "entry": price,
                    "qty": qty,
                    "trail_sl": sl,
                    "entry_time": get_ist_time()
                })

                level["used"] = True

                utils.log(
                    f"🔻 {symbol} SHORT ENTRY @ {price}",
                    tg=True
                )

    # ================= STEP TRAILING STOP =================

    for p in positions[:]:

        # ================= LONG =================

        if p["side"] == "long":

            profit_move = price - p["entry"]

            # NUMBER OF 100-POINT STEPS
            steps = int(profit_move // TRAIL_STEP)

            # MOVE STOP LOSS STEP-BY-STEP
            new_sl = (
                p["entry"]
                - STOPLOSS[symbol]
                + (steps * TRAIL_STEP)
            )

            p["trail_sl"] = max(
                p["trail_sl"],
                new_sl
            )

        # ================= SHORT =================

        else:

            profit_move = p["entry"] - price

            # NUMBER OF 100-POINT STEPS
            steps = int(profit_move // TRAIL_STEP)

            # MOVE STOP LOSS STEP-BY-STEP
            new_sl = (
                p["entry"]
                + STOPLOSS[symbol]
                - (steps * TRAIL_STEP)
            )

            p["trail_sl"] = min(
                p["trail_sl"],
                new_sl
            )

# ================= UTILS =================

utils = TradingUtils(

    contract_size=CONTRACT_SIZE,

    taker_fee=TAKER_FEE,

    timeframe="5m",

    days=5,

    telegram_token=os.getenv("supertrend_ha_fast_bot"),

    telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID"),

    bot_name=BOT_NAME
)

# ================= MAIN =================

def run():

    state = {

        "balance": 10000,

        "daily_pnl": 0,

        "last_day": get_ist_time().date(),

        "trading_enabled": True,

        "symbols": {
            s: {"positions": []}
            for s in SYMBOLS
        }
    }

    utils.log("🚀 BOT STARTED", tg=True)

    while True:

        try:

            # ================= RESET DAILY =================

            today = get_ist_time().date()

            if today != state["last_day"]:

                state["daily_pnl"] = 0

                state["last_day"] = today

                state["trading_enabled"] = True

                utils.log(
                    "✅ New Day Started - Trading Enabled",
                    tg=True
                )

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

                    exit_trade = False

                    # ================= LONG =================

                    if p["side"] == "long":

                        # TARGET HIT
                        if price >= p["entry"] + TP[symbol]:

                            pnl = (
                                (price - p["entry"])
                                * CONTRACT_SIZE[symbol]
                                * p["qty"]
                            )

                            exit_trade = True

                        # TRAILING STOP HIT
                        elif price <= p["trail_sl"]:

                            pnl = (
                                (price - p["entry"])
                                * CONTRACT_SIZE[symbol]
                                * p["qty"]
                            )

                            exit_trade = True

                    # ================= SHORT =================

                    else:

                        # TARGET HIT
                        if price <= p["entry"] - TP[symbol]:

                            pnl = (
                                (p["entry"] - price)
                                * CONTRACT_SIZE[symbol]
                                * p["qty"]
                            )

                            exit_trade = True

                        # TRAILING STOP HIT
                        elif price >= p["trail_sl"]:

                            pnl = (
                                (p["entry"] - price)
                                * CONTRACT_SIZE[symbol]
                                * p["qty"]
                            )

                            exit_trade = True

                    if not exit_trade:
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

                    state["daily_pnl"] += net

                    now = get_ist_time()

                    emoji = "🟢" if net > 0 else "🔴"

                    utils.log(
                        f"{emoji} {symbol} EXIT @ {price} | "
                        f"PNL: {round(net,6)} | "
                        f"DAILY: {round(state['daily_pnl'],2)} | "
                        f"SL: {round(p['trail_sl'],2)}",
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

                    # ================= DAILY TARGET =================

                    if state["daily_pnl"] >= 500:

                        state["trading_enabled"] = False

                        utils.log(
                            "🛑 DAILY TARGET HIT ($500). Trading stopped.",
                            tg=True
                        )

                # ================= FETCH CANDLES =================

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

                auto_git_push()

            time.sleep(SLEEP_TIME)

        except Exception as e:

            print("ERROR:", e)

            time.sleep(5)

# ================= START =================

if __name__ == "__main__":

    run()