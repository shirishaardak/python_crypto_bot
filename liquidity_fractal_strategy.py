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

BOT_NAME = "liquidity_displacement_strategy"

SYMBOLS = ["BTCUSD"]

TIMEFRAME = "5m"

DAYS = 15

# =========================================================
# ACCOUNT SETTINGS
# =========================================================

START_BALANCE = 10000

RISK_PER_TRADE = 0.01

MAX_TRADES_PER_DAY = 10

# =========================================================
# STRATEGY SETTINGS
# =========================================================

EMA_LENGTH = 200

ATR_LENGTH = 14

ATR_BUFFER = 0.2

ATR_TP_MULTIPLIER = 2.0

MIN_SWEEP_ATR = 0.3

STRONG_BODY_THRESHOLD = 0.7

# =========================================================
# DAILY TARGET
# =========================================================

DAILY_TARGET_MAX = 1000

MAX_DAILY_LOSS = -1000

# =========================================================
# CONTRACT SETTINGS
# =========================================================

CONTRACT_SIZE = {
    "BTCUSD": 0.001
}

TAKER_FEE = 0.0005

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
# DAILY RESET
# =========================================================

def reset_daily_state(state):

    today = datetime.now().date()

    if state["last_reset_day"] != today:

        state["daily_pnl"] = 0

        state["trade_count"] = 0

        state["last_reset_day"] = today

        utils.log(
            "🌞 New Trading Day Started",
            tg=True
        )

# =========================================================
# FRACTALS
# =========================================================

def detect_fractals(df):

    df["high_fractal"] = np.nan

    df["low_fractal"] = np.nan

    for i in range(2, len(df) - 2):

        # HIGH FRACTAL
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

        # LOW FRACTAL
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

        ema = df["EMA"].iloc[i]

        atr = df["ATR"].iloc[i]

        # =================================================
        # PREVIOUS CANDLE
        # =================================================

        prev_open = df["Open"].iloc[i - 2]

        prev_close = df["Close"].iloc[i - 2]

        prev_low = df["Low"].iloc[i - 2]

        prev_high = df["High"].iloc[i - 2]

        # =================================================
        # CURRENT CANDLE
        # =================================================

        open_price = df["Open"].iloc[i - 1]

        close = df["Close"].iloc[i -1]

        low = df["Low"].iloc[i -1]

        high = df["High"].iloc[i-1]

        # =================================================
        # LONG SETUP
        # =================================================

        if not np.isnan(last_low_fractal):

            # SWEEP
            sweep = (
                prev_low < last_low_fractal
            )

            # RECLAIM
            reclaim = (
                prev_close > last_low_fractal
            )

            # SWEEP SIZE
            sweep_size = (
                abs(last_low_fractal - prev_low)
            )

            valid_sweep = (
                sweep_size > atr * MIN_SWEEP_ATR
            )

            # STRONG BULLISH CANDLE
            bullish_candle = (
                close > open_price
            )

            bull_strength = (
                (close - low)
                /
                max((high - low), 0.0001)
            )

            strong_bull = (
                bull_strength
                > STRONG_BODY_THRESHOLD
            )

            # TREND
            bullish_trend = (
                close > ema
            )

            # ENTRY
            if (
                sweep
                and reclaim
                and valid_sweep
                and bullish_candle
                and strong_bull
                and bullish_trend
            ):

                sl = (
                    prev_low
                    - (atr * ATR_BUFFER)
                )

                tp = (
                    close
                    + (
                        atr
                        * ATR_TP_MULTIPLIER
                    )
                )

                df.loc[
                    df.index[i],
                    "long_signal"
                ] = True

                df.loc[
                    df.index[i],
                    "long_sl"
                ] = sl

                df.loc[
                    df.index[i],
                    "long_tp"
                ] = tp

        # =================================================
        # SHORT SETUP
        # =================================================

        if not np.isnan(last_high_fractal):

            # SWEEP
            sweep = (
                prev_high > last_high_fractal
            )

            # RECLAIM
            reclaim = (
                prev_close < last_high_fractal
            )

            # SWEEP SIZE
            sweep_size = (
                abs(prev_high - last_high_fractal)
            )

            valid_sweep = (
                sweep_size > atr * MIN_SWEEP_ATR
            )

            # STRONG BEARISH CANDLE
            bearish_candle = (
                close < open_price
            )

            bear_strength = (
                (high - close)
                /
                max((high - low), 0.0001)
            )

            strong_bear = (
                bear_strength
                > STRONG_BODY_THRESHOLD
            )

            # TREND
            bearish_trend = (
                close < ema
            )

            # ENTRY
            if (
                sweep
                and reclaim
                and valid_sweep
                and bearish_candle
                and strong_bear
                and bearish_trend
            ):

                sl = (
                    prev_high
                    + (atr * ATR_BUFFER)
                )

                tp = (
                    close
                    - (
                        atr
                        * ATR_TP_MULTIPLIER
                    )
                )

                df.loc[
                    df.index[i],
                    "short_signal"
                ] = True

                df.loc[
                    df.index[i],
                    "short_sl"
                ] = sl

                df.loc[
                    df.index[i],
                    "short_tp"
                ] = tp

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

    risk_amount = (
        balance * risk_percent
    )

    stop_distance = abs(
        entry - stop
    )

    if stop_distance <= 0:
        return 0

    qty = (
        risk_amount
        /
        (
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

    last = df.iloc[-2]

    pos = state["position"]

    now = datetime.now()

    # =====================================================
    # EXIT
    # =====================================================

    if pos:

        exit_trade = False

        # LONG
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

        # SHORT
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

        # CLOSE POSITION
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
                f"BALANCE: {round(state['balance'], 2)}",
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

        # DAILY TARGET
        if (
            state["daily_pnl"]
            >= DAILY_TARGET_MAX
        ):

            utils.log(
                "🎯 Daily Target Hit",
                tg=True
            )

            return

        # MAX LOSS
        if (
            state["daily_pnl"]
            <= MAX_DAILY_LOSS
        ):

            utils.log(
                "🛑 Max Daily Loss Hit",
                tg=True
            )

            return

        # MAX TRADES
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
                    f"ENTRY: {round(price, 2)}\n"
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
                    f"ENTRY: {round(price, 2)}\n"
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
            )

        }

        for s in SYMBOLS
    }

    utils.log(
        "🚀 Liquidity Displacement Strategy Started",
        tg=True
    )

    while True:

        try:

            for symbol in SYMBOLS:

                # FETCH DATA
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

                # FETCH PRICE
                price = utils.fetch_price(symbol)

                if price is None:
                    continue

                # PROCESS
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