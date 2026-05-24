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

BOT_NAME = "btc_breakout_pro"

SYMBOLS = ["BTCUSD"]

TIMEFRAME = "5m"

DAYS = 20

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

SWING_LOOKBACK = 3

RISK_REWARD = 3.0

USE_TREND_FILTER = True

USE_TRAILING_STOP = True

TRAIL_AT_R = 1.0

ATR_BREAKOUT_FILTER = 0.2

# =========================================================
# DAILY SETTINGS
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
    telegram_token=os.getenv("testmyaglostrategy_bot"),
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
# SWING DETECTION
# =========================================================

def detect_swings(df):

    df["swing_high"] = np.nan

    df["swing_low"] = np.nan

    for i in range(
        SWING_LOOKBACK,
        len(df) - SWING_LOOKBACK
    ):

        current_high = df["High"].iloc[i]

        current_low = df["Low"].iloc[i]

        left_highs = df[
            "High"
        ].iloc[
            i - SWING_LOOKBACK:i
        ]

        right_highs = df[
            "High"
        ].iloc[
            i + 1:i + SWING_LOOKBACK + 1
        ]

        left_lows = df[
            "Low"
        ].iloc[
            i - SWING_LOOKBACK:i
        ]

        right_lows = df[
            "Low"
        ].iloc[
            i + 1:i + SWING_LOOKBACK + 1
        ]

        # SWING HIGH
        if (
            current_high > left_highs.max()
            and
            current_high > right_highs.max()
        ):

            df.loc[
                df.index[i],
                "swing_high"
            ] = current_high

        # SWING LOW
        if (
            current_low < left_lows.min()
            and
            current_low < right_lows.min()
        ):

            df.loc[
                df.index[i],
                "swing_low"
            ] = current_low

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

    last_swing_low = np.nan

    last_swing_high = np.nan

    for i in range(50, len(df)):

        # =====================================================
        # UPDATE SWINGS
        # =====================================================

        if not np.isnan(df["swing_low"].iloc[i - 2]):

            last_swing_low = (
                df["swing_low"].iloc[i - 2]
            )

        if not np.isnan(df["swing_high"].iloc[i - 2]):

            last_swing_high = (
                df["swing_high"].iloc[i - 2]
            )

        confirm_close = df["Close"].iloc[i - 1]

        atr = df["ATR"].iloc[i - 1]

        ema = df["EMA"].iloc[i - 1]

        # =====================================================
        # LONG BREAKOUT
        # =====================================================

        if (
            not np.isnan(last_swing_high)
            and
            not np.isnan(last_swing_low)
        ):

            breakout_buy = (

                confirm_close
                >
                last_swing_high
                + (atr * ATR_BREAKOUT_FILTER)
            )

            bullish_trend = (
                confirm_close > ema
            )

            if not USE_TREND_FILTER:

                bullish_trend = True

            if (
                breakout_buy
                and bullish_trend
            ):

                entry = confirm_close

                sl = last_swing_low

                risk = abs(entry - sl)

                if risk > 0:

                    tp = (
                        entry
                        +
                        (risk * RISK_REWARD)
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

        # =====================================================
        # SHORT BREAKOUT
        # =====================================================

        if (
            not np.isnan(last_swing_low)
            and
            not np.isnan(last_swing_high)
        ):

            breakout_sell = (

                confirm_close
                <
                last_swing_low
                - (atr * ATR_BREAKOUT_FILTER)
            )

            bearish_trend = (
                confirm_close < ema
            )

            if not USE_TREND_FILTER:

                bearish_trend = True

            if (
                breakout_sell
                and bearish_trend
            ):

                entry = confirm_close

                sl = last_swing_high

                risk = abs(sl - entry)

                if risk > 0:

                    tp = (
                        entry
                        -
                        (risk * RISK_REWARD)
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

    df = detect_swings(df)

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
    # MANAGE OPEN POSITION
    # =====================================================

    if pos:

        if pos["side"] == "long":

            current_profit = (
                price - pos["entry"]
            )

            if (
                USE_TRAILING_STOP
                and
                current_profit >= pos["initial_risk"] * TRAIL_AT_R
            ):

                pos["sl"] = max(
                    pos["sl"],
                    pos["entry"]
                )

            pnl = (
                (price - pos["entry"])
                * CONTRACT_SIZE[symbol]
                * pos["qty"]
            )

            exit_trade = (
                price <= pos["sl"]
                or
                price >= pos["tp"]
            )

        else:

            current_profit = (
                pos["entry"] - price
            )

            if (
                USE_TRAILING_STOP
                and
                current_profit >= pos["initial_risk"] * TRAIL_AT_R
            ):

                pos["sl"] = min(
                    pos["sl"],
                    pos["entry"]
                )

            pnl = (
                (pos["entry"] - price)
                * CONTRACT_SIZE[symbol]
                * pos["qty"]
            )

            exit_trade = (
                price >= pos["sl"]
                or
                price <= pos["tp"]
            )

        # =================================================
        # EXIT POSITION
        # =================================================

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

        if (
            state["daily_pnl"]
            >= DAILY_TARGET_MAX
        ):

            return

        if (
            state["daily_pnl"]
            <= MAX_DAILY_LOSS
        ):

            return

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

            entry = price

            qty = calculate_position_size(
                state["balance"],
                RISK_PER_TRADE,
                entry,
                sl,
                symbol
            )

            if qty > 0:

                state["position"] = {

                    "side": "long",

                    "entry": entry,

                    "qty": qty,

                    "entry_time": now,

                    "sl": sl,

                    "tp": tp,

                    "initial_risk": abs(
                        entry - sl
                    )

                }

                state["trade_count"] += 1

                utils.log(
                    f"🟢 LONG {symbol}\n"
                    f"ENTRY: {round(entry, 2)}\n"
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

            entry = price

            qty = calculate_position_size(
                state["balance"],
                RISK_PER_TRADE,
                entry,
                sl,
                symbol
            )

            if qty > 0:

                state["position"] = {

                    "side": "short",

                    "entry": entry,

                    "qty": qty,

                    "entry_time": now,

                    "sl": sl,

                    "tp": tp,

                    "initial_risk": abs(
                        sl - entry
                    )

                }

                state["trade_count"] += 1

                utils.log(
                    f"🔴 SHORT {symbol}\n"
                    f"ENTRY: {round(entry, 2)}\n"
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
        "🚀 BTC BREAKOUT BOT STARTED",
        tg=True
    )

    while True:

        try:

            for symbol in SYMBOLS:

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

                price = utils.fetch_price(symbol)

                if price is None:

                    continue

                process_symbol(
                    symbol,
                    df,
                    price,
                    state[symbol],
                    is_new_candle
                )

            time.sleep(2)

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