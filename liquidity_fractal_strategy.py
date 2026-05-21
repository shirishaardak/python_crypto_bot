import os
import time
import traceback
import numpy as np
import pandas as pd
import pandas_ta as ta

from datetime import datetime
from dotenv import load_dotenv

from utils import TradingUtils

load_dotenv()

# =========================================================
# CONFIG
# =========================================================

BOT_NAME = "liquidity_fractal_strategy"

SYMBOLS = ["BTCUSD"]

TIMEFRAME = "5m"

DAYS = 15

# =========================================================
# ACCOUNT SETTINGS
# =========================================================

START_BALANCE = 10000

MIN_BALANCE = 1000

RISK_PER_TRADE = 0.01  # 1%

MAX_TRADES_PER_DAY = 10

# =========================================================
# STRATEGY SETTINGS
# =========================================================

EMA_LENGTH = 200

ATR_LENGTH = 14

ATR_BUFFER = 0.2

MIN_SWEEP_ATR = 0.2

REJECTION_THRESHOLD = 0.6

LOOKBACK_FRAC = 100

# =========================================================
# CONTRACT SETTINGS
# =========================================================

DEFAULT_CONTRACTS = {
    "BTCUSD": 1000
}

CONTRACT_SIZE = {
    "BTCUSD": 0.001
}

TAKER_FEE = 0.0005

# =========================================================
# DAILY TARGET
# =========================================================

DAILY_TARGET_MIN = 200

DAILY_TARGET_MAX = 500

MAX_DAILY_LOSS = -3000

# =========================================================
# SAVE PATHS
# =========================================================

BASE_DIR = os.getcwd()

SAVE_DIR = os.path.join(
    BASE_DIR,
    "data",
    BOT_NAME
)

os.makedirs(SAVE_DIR, exist_ok=True)

# =========================================================
# UTILS
# =========================================================

utils = TradingUtils(
    contract_size=CONTRACT_SIZE,
    taker_fee=TAKER_FEE,
    timeframe=TIMEFRAME,
    days=DAYS,
    telegram_token=os.getenv("testmyaglostrateg"),
    telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID"),
    bot_name=BOT_NAME
)

# =========================================================
# SAVE PROCESSED DATA
# =========================================================

def save_processed_data(df, symbol):

    path = os.path.join(
        SAVE_DIR,
        f"{symbol}_processed.csv"
    )

    out = pd.DataFrame({

        "time": df.index,

        "Open": df["Open"],
        "High": df["High"],
        "Low": df["Low"],
        "Close": df["Close"],

        "EMA": df["EMA"],
        "ATR": df["ATR"],

        "high_fractal": df["high_fractal"],
        "low_fractal": df["low_fractal"],

        "long_signal": df["long_signal"],
        "short_signal": df["short_signal"],

        "long_sl": df["long_sl"],
        "short_sl": df["short_sl"],

        "long_tp": df["long_tp"],
        "short_tp": df["short_tp"],

    })

    out.to_csv(path, index=False)

# =========================================================
# DAILY RESET
# =========================================================

def reset_daily_state(state):

    today = datetime.now().date()

    if state["last_reset_day"] != today:

        state["daily_pnl"] = 0

        state["trade_count"] = 0

        state["trading_enabled"] = True

        state["last_reset_day"] = today

        utils.log(
            "🌞 New trading day started",
            tg=True
        )

# =========================================================
# FRACTALS
# =========================================================

def detect_fractals(df):

    df["high_fractal"] = np.nan

    df["low_fractal"] = np.nan

    for i in range(2, len(df) - 2):

        # =================================================
        # HIGH FRACTAL
        # =================================================

        if (
            df["High"].iloc[i] > df["High"].iloc[i - 1]
            and
            df["High"].iloc[i] > df["High"].iloc[i - 2]
            and
            df["High"].iloc[i] > df["High"].iloc[i + 1]
            and
            df["High"].iloc[i] > df["High"].iloc[i + 2]
        ):

            df.loc[
                df.index[i + 2],
                "high_fractal"
            ] = df["High"].iloc[i]

        # =================================================
        # LOW FRACTAL
        # =================================================

        if (
            df["Low"].iloc[i] < df["Low"].iloc[i - 1]
            and
            df["Low"].iloc[i] < df["Low"].iloc[i - 2]
            and
            df["Low"].iloc[i] < df["Low"].iloc[i + 1]
            and
            df["Low"].iloc[i] < df["Low"].iloc[i + 2]
        ):

            df.loc[
                df.index[i + 2],
                "low_fractal"
            ] = df["Low"].iloc[i]

    return df

# =========================================================
# INDICATORS
# =========================================================

def apply_indicators(df):

    df["EMA"] = ta.ema(
        df["Close"],
        length=EMA_LENGTH
    )

    df["ATR"] = ta.atr(
        df["High"],
        df["Low"],
        df["Close"],
        length=ATR_LENGTH
    )

    return df

# =========================================================
# RESISTANCE
# =========================================================

def get_nearest_resistance(df, idx):

    current_price = df["Close"].iloc[idx]

    levels = []

    start = max(0, idx - LOOKBACK_FRAC)

    for i in range(start, idx):

        val = df["high_fractal"].iloc[i]

        if not np.isnan(val):

            if val > current_price:

                levels.append(val)

    if len(levels) == 0:
        return None

    return min(levels)

# =========================================================
# SUPPORT
# =========================================================

def get_nearest_support(df, idx):

    current_price = df["Close"].iloc[idx]

    levels = []

    start = max(0, idx - LOOKBACK_FRAC)

    for i in range(start, idx):

        val = df["low_fractal"].iloc[i]

        if not np.isnan(val):

            if val < current_price:

                levels.append(val)

    if len(levels) == 0:
        return None

    return max(levels)

# =========================================================
# SIGNAL GENERATION
# =========================================================

def generate_signals(df):

    df["long_signal"] = False

    df["short_signal"] = False

    df["long_sl"] = np.nan
    df["short_sl"] = np.nan

    df["long_tp"] = np.nan
    df["short_tp"] = np.nan

    last_low_fractal = np.nan

    last_high_fractal = np.nan

    for i in range(50, len(df)):

        # =================================================
        # UPDATE FRACTALS
        # =================================================

        if not np.isnan(df["low_fractal"].iloc[i]):

            last_low_fractal = (
                df["low_fractal"].iloc[i]
            )

        if not np.isnan(df["high_fractal"].iloc[i]):

            last_high_fractal = (
                df["high_fractal"].iloc[i]
            )

        close = df["Close"].iloc[i]

        low = df["Low"].iloc[i]

        high = df["High"].iloc[i]

        ema = df["EMA"].iloc[i]

        atr = df["ATR"].iloc[i]

        # =================================================
        # LONG SETUP
        # =================================================

        if not np.isnan(last_low_fractal):

            sweep = low < last_low_fractal

            reclaim = close > last_low_fractal

            bullish_trend = close > ema

            sweep_size = (
                abs(last_low_fractal - low)
            )

            min_sweep = (
                sweep_size > atr * MIN_SWEEP_ATR
            )

            candle_strength = (
                (close - low)
                / max((high - low), 0.0001)
            )

            strong_rejection = (
                candle_strength > REJECTION_THRESHOLD
            )

            if (
                sweep
                and reclaim
                and bullish_trend
                and min_sweep
                and strong_rejection
            ):

                resistance = (
                    get_nearest_resistance(df, i)
                )

                if resistance is not None:

                    df.loc[
                        df.index[i],
                        "long_signal"
                    ] = True

                    df.loc[
                        df.index[i],
                        "long_sl"
                    ] = (
                        low - (atr * ATR_BUFFER)
                    )

                    df.loc[
                        df.index[i],
                        "long_tp"
                    ] = resistance

        # =================================================
        # SHORT SETUP
        # =================================================

        if not np.isnan(last_high_fractal):

            sweep = high > last_high_fractal

            reclaim = close < last_high_fractal

            bearish_trend = close < ema

            sweep_size = (
                abs(high - last_high_fractal)
            )

            min_sweep = (
                sweep_size > atr * MIN_SWEEP_ATR
            )

            candle_strength = (
                (high - close)
                / max((high - low), 0.0001)
            )

            strong_rejection = (
                candle_strength > REJECTION_THRESHOLD
            )

            if (
                sweep
                and reclaim
                and bearish_trend
                and min_sweep
                and strong_rejection
            ):

                support = (
                    get_nearest_support(df, i)
                )

                if support is not None:

                    df.loc[
                        df.index[i],
                        "short_signal"
                    ] = True

                    df.loc[
                        df.index[i],
                        "short_sl"
                    ] = (
                        high + (atr * ATR_BUFFER)
                    )

                    df.loc[
                        df.index[i],
                        "short_tp"
                    ] = support

    return df

# =========================================================
# PREPARE DATA
# =========================================================

def prepare_dataframe(df):

    df = df.copy()

    df = apply_indicators(df)

    df = detect_fractals(df)

    df = generate_signals(df)

    return df

# =========================================================
# POSITION SIZE
# =========================================================

def calculate_position_size(
    balance,
    risk_percent,
    entry,
    stop,
    symbol
):

    risk_amount = balance * risk_percent

    stop_distance = abs(entry - stop)

    if stop_distance <= 0:
        return 0

    qty = (
        risk_amount
        / (
            stop_distance
            * CONTRACT_SIZE[symbol]
        )
    )

    return max(1, int(qty))

# =========================================================
# PROCESS SYMBOL
# =========================================================

def process_symbol(
    symbol,
    df,
    price,
    state,
    is_new_candle
):

    reset_daily_state(state)

    df = prepare_dataframe(df)

    save_processed_data(df, symbol)

    last = df.iloc[-2]

    pos = state["position"]

    now = datetime.now()

    # =====================================================
    # EXIT
    # =====================================================

    if pos:

        exit_trade = False

        if pos["side"] == "long":

            pnl = (
                (price - pos["entry"])
                * CONTRACT_SIZE[symbol]
                * pos["qty"]
            )

            if (
                price <= pos["sl"]
                or
                price >= pos["tp"]
            ):

                exit_trade = True

        else:

            pnl = (
                (pos["entry"] - price)
                * CONTRACT_SIZE[symbol]
                * pos["qty"]
            )

            if (
                price >= pos["sl"]
                or
                price <= pos["tp"]
            ):

                exit_trade = True

        if exit_trade:

            entry_fee = utils.commission(
                pos["entry"],
                pos["qty"],
                symbol
            )

            exit_fee = utils.commission(
                price,
                pos["qty"],
                symbol
            )

            total_fee = (
                entry_fee + exit_fee
            )

            net = pnl - total_fee

            state["balance"] += net

            state["daily_pnl"] += net

            utils.save_trade({

                "symbol": symbol,

                "side": pos["side"],

                "entry_price": pos["entry"],

                "exit_price": price,

                "qty": pos["qty"],

                "net_pnl": round(net, 2),

                "entry_time": pos["entry_time"],

                "exit_time": now,

                "sl": pos["sl"],

                "tp": pos["tp"]

            })

            emoji = "🟢" if net > 0 else "🔴"

            utils.log(
                f"{emoji} EXIT {symbol} | "
                f"PNL: {round(net, 2)} | "
                f"BAL: {round(state['balance'], 2)}",
                tg=True
            )

            state["position"] = None

            return

    # =====================================================
    # ENTRY
    # =====================================================

    if (
        not pos
        and is_new_candle
    ):

        # =================================================
        # DAILY TARGET
        # =================================================

        if (
            state["daily_pnl"]
            >= DAILY_TARGET_MAX
        ):

            return

        # =================================================
        # MAX LOSS
        # =================================================

        if (
            state["daily_pnl"]
            <= MAX_DAILY_LOSS
        ):

            return

        # =================================================
        # TRADE LIMIT
        # =================================================

        if (
            state["trade_count"]
            >= MAX_TRADES_PER_DAY
        ):

            return

        # =================================================
        # LONG ENTRY
        # =================================================

        if last.long_signal:

            sl = last.long_sl

            tp = last.long_tp

            qty = calculate_position_size(
                state["balance"],
                RISK_PER_TRADE,
                price,
                sl,
                symbol
            )

            if qty > 0:

                state["position"] = {

                    "side": "long",

                    "entry": price,

                    "qty": qty,

                    "entry_time": now,

                    "sl": sl,

                    "tp": tp

                }

                state["trade_count"] += 1

                utils.log(
                    f"🟢 LONG {symbol}\n"
                    f"ENTRY: {price}\n"
                    f"SL: {round(sl, 2)}\n"
                    f"TP: {round(tp, 2)}\n"
                    f"QTY: {qty}",
                    tg=True
                )

        # =================================================
        # SHORT ENTRY
        # =================================================

        elif last.short_signal:

            sl = last.short_sl

            tp = last.short_tp

            qty = calculate_position_size(
                state["balance"],
                RISK_PER_TRADE,
                price,
                sl,
                symbol
            )

            if qty > 0:

                state["position"] = {

                    "side": "short",

                    "entry": price,

                    "qty": qty,

                    "entry_time": now,

                    "sl": sl,

                    "tp": tp

                }

                state["trade_count"] += 1

                utils.log(
                    f"🔴 SHORT {symbol}\n"
                    f"ENTRY: {price}\n"
                    f"SL: {round(sl, 2)}\n"
                    f"TP: {round(tp, 2)}\n"
                    f"QTY: {qty}",
                    tg=True
                )

# =========================================================
# MAIN
# =========================================================

def run():

    state = {

        s: {

            "position": None,

            "last_candle_time": None,

            "balance": START_BALANCE,

            "daily_pnl": 0,

            "trade_count": 0,

            "last_reset_day": (
                datetime.now().date()
            ),

            "trading_enabled": True

        }

        for s in SYMBOLS
    }

    utils.log(
        "🚀 Liquidity Fractal Strategy Started",
        tg=True
    )

    while True:

        try:

            for symbol in SYMBOLS:

                # =============================================
                # FETCH DATA
                # =============================================

                df = utils.fetch_candles(symbol)

                if (
                    df is None
                    or len(df) < 300
                ):

                    continue

                latest_candle_time = (
                    df.index[-2]
                )

                is_new_candle = (

                    state[symbol][
                        "last_candle_time"
                    ]

                    != latest_candle_time
                )

                if is_new_candle:

                    state[symbol][
                        "last_candle_time"
                    ] = latest_candle_time

                # =============================================
                # FETCH PRICE
                # =============================================

                price = utils.fetch_price(symbol)

                if price is None:
                    continue

                # =============================================
                # PROCESS
                # =============================================

                process_symbol(
                    symbol,
                    df,
                    price,
                    state[symbol],
                    is_new_candle
                )

            time.sleep(3)

        except Exception as e:

            utils.log(
                f"🚨 ERROR: {e}\n"
                f"{traceback.format_exc()}",
                tg=True
            )

            time.sleep(5)

# =========================================================
# START
# =========================================================

if __name__ == "__main__":

    run()