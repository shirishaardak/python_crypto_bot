import os
import time
import pandas as pd
import numpy as np

from datetime import datetime

import pandas_ta as ta

from dotenv import load_dotenv

import traceback

from utils import TradingUtils

load_dotenv()

# ================= CONFIG =================

BOT_NAME = "price_trend_strategy"

SYMBOLS = ["BTCUSD"]

DEFAULT_CONTRACTS = {
    "BTCUSD": 1000
}

CONTRACT_SIZE = {
    "BTCUSD": 0.001
}

TAKER_FEE = 0.0005

TIMEFRAME = "5m"

DAYS = 15

MIN_BALANCE = 1000

# ================= TARGET / LOSS =================

TP = {
    "BTCUSD": 500
}

TRAIL_TRIGGER = {
    "BTCUSD": 200   # Trail activates only after 200 points in profit
}

TRAIL_DISTANCE = {
    "BTCUSD": 100   # SL moves in 100-point steps
}

DAILY_TARGET = 1000


# ================= INIT UTILS =================

utils = TradingUtils(
    contract_size=CONTRACT_SIZE,
    taker_fee=TAKER_FEE,
    timeframe=TIMEFRAME,
    days=DAYS,
    telegram_token=os.getenv("testmyaglostrategy_bot"),
    telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID"),
    bot_name=BOT_NAME
)

# ================= DAILY RESET =================

def reset_daily_state(state):

    today = datetime.now().date()

    if state["last_reset_day"] != today:

        state["daily_pnl"] = 0
        state["trading_enabled"] = True
        state["last_reset_day"] = today

        utils.log(
            "🌞 New trading day started",
            tg=True
        )


# ================= TRENDLINE =================

def calculate_trendline(df):

    ha = ta.ha(
        df["Open"],
        df["High"],
        df["Low"],
        df["Close"]
    ).reset_index(drop=True)

    ha["high_fractal"] = np.nan
    ha["low_fractal"] = np.nan

    for i in range(2, len(ha) - 2):

        is_high = (
            ha.loc[i, "HA_high"] > ha.loc[i - 1, "HA_high"]
            and ha.loc[i, "HA_high"] > ha.loc[i - 2, "HA_high"]
            and ha.loc[i, "HA_high"] > ha.loc[i + 1, "HA_high"]
            and ha.loc[i, "HA_high"] > ha.loc[i + 2, "HA_high"]
        )

        is_low = (
            ha.loc[i, "HA_low"] < ha.loc[i - 1, "HA_low"]
            and ha.loc[i, "HA_low"] < ha.loc[i - 2, "HA_low"]
            and ha.loc[i, "HA_low"] < ha.loc[i + 1, "HA_low"]
            and ha.loc[i, "HA_low"] < ha.loc[i + 2, "HA_low"]
        )

        if is_high:
            ha.loc[i + 2, "high_fractal"] = ha.loc[i, "HA_high"]

        if is_low:
            ha.loc[i + 2, "low_fractal"] = ha.loc[i, "HA_low"]

    ha["Trendline"] = np.nan

    last_high_fractal = np.nan
    last_low_fractal = np.nan

    trendline = ha.loc[0, "HA_close"]

    for i in range(1, len(ha)):

        if not np.isnan(ha.loc[i, "high_fractal"]):
            last_high_fractal = ha.loc[i, "high_fractal"]

        if not np.isnan(ha.loc[i, "low_fractal"]):
            last_low_fractal = ha.loc[i, "low_fractal"]

        current_close = ha.loc[i, "HA_close"]
        prev_close = ha.loc[i - 1, "HA_close"]

        if (
            not np.isnan(last_high_fractal)
            and prev_close <= last_high_fractal
            and current_close > last_high_fractal
            and current_close > trendline
            and not np.isnan(last_low_fractal)
        ):
            trendline = last_low_fractal

        elif (
            not np.isnan(last_low_fractal)
            and prev_close >= last_low_fractal
            and current_close < last_low_fractal
            and current_close < trendline
            and not np.isnan(last_high_fractal)
        ):
            trendline = last_high_fractal

        ha.loc[i, "Trendline"] = trendline
        ha.loc[i, "up_Trendline"] = trendline + 100
        ha.loc[i, "down_Trendline"] = trendline - 100

    return ha


# ================= STRATEGY =================

def process_symbol(symbol, df, price, state, is_new_candle):

    reset_daily_state(state)

    ha = calculate_trendline(df)

    last = ha.iloc[-2]
    prev = ha.iloc[-3]

    pos = state["position"]

    now = datetime.now()

    # ================= EXIT =================

    if pos:

        if pos["side"] == "long":

            live_pnl = (
                (price - pos["entry"])
                * CONTRACT_SIZE[symbol]
                * pos["qty"]
            )

            profit_points = price - pos["entry"]

            if profit_points >= TRAIL_TRIGGER[symbol]:

                # How many 100-point steps beyond the trigger has price moved?
                steps = int((profit_points - TRAIL_TRIGGER[symbol]) / TRAIL_DISTANCE[symbol])
                new_sl = pos["entry"] + (steps * TRAIL_DISTANCE[symbol])
                new_sl = max(pos["entry"], new_sl)

                if new_sl > pos["sl"]:

                    pos["sl"] = new_sl

                    utils.log(
                        f"🔒 {symbol} LONG trailing SL → {round(pos['sl'], 2)}",
                        tg=True
                    )

        else:

            live_pnl = (
                (pos["entry"] - price)
                * CONTRACT_SIZE[symbol]
                * pos["qty"]
            )

            profit_points = pos["entry"] - price

            if profit_points >= TRAIL_TRIGGER[symbol]:

                # How many 100-point steps beyond the trigger has price moved?
                steps = int((profit_points - TRAIL_TRIGGER[symbol]) / TRAIL_DISTANCE[symbol])
                new_sl = pos["entry"] - (steps * TRAIL_DISTANCE[symbol])
                new_sl = min(pos["entry"], new_sl)

                if new_sl < pos["sl"]:

                    pos["sl"] = new_sl

                    utils.log(
                        f"🔒 {symbol} SHORT trailing SL → {round(pos['sl'], 2)}",
                        tg=True
                    )

        exit_trade = False
        pnl = 0

        if (
            DAILY_TARGET is not None
            and state["daily_pnl"] + live_pnl >= DAILY_TARGET
        ):

            pnl = live_pnl
            exit_trade = True

        elif (
            pos["side"] == "long"
            and (
                price <= pos["sl"]
                or price >= pos["entry"] + TP[symbol]
                or price < last.down_Trendline
            )
        ):

            pnl = live_pnl
            exit_trade = True

        elif (
            pos["side"] == "short"
            and (
                price >= pos["sl"]
                or price <= pos["entry"] - TP[symbol]
                or price > last.up_Trendline
            )
        ):

            pnl = live_pnl
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

            net = pnl - (entry_fee + exit_fee)

            state["balance"] += net
            state["daily_pnl"] += net

            utils.save_trade({
                "symbol": symbol,
                "side": pos["side"],
                "entry_price": pos["entry"],
                "exit_price": price,
                "qty": pos["qty"],
                "net_pnl": round(net, 6),
                "entry_time": pos["entry_time"],
                "exit_time": now
            })

            emoji = "🟢" if net > 0 else "🔴"

            utils.log(
                f"{emoji} {symbol} EXIT @ {price} | "
                f"PNL: {round(net, 6)}",
                tg=True
            )

            state["position"] = None
            state["last_exit_candle"] = df.index[-1]

            return

    # ================= ENTRY =================

    if not pos:

        if not state["trading_enabled"]:
            return

        if state.get("last_exit_candle") == df.index[-1]:
            return

        if state["balance"] < MIN_BALANCE:
            return

        if (
            prev.HA_close <= prev.Trendline
            and last.HA_close > last.Trendline
        ):

            state["position"] = {
                "side": "long",
                "entry": price,
                "qty": DEFAULT_CONTRACTS[symbol],
                "entry_time": now,
                "sl": last.down_Trendline
            }

            utils.log(
                f"🟢 {symbol} LONG @ {price} | SL: {last.down_Trendline}",
                tg=True
            )

        elif (
            prev.HA_close >= prev.Trendline
            and last.HA_close < last.Trendline
        ):

            state["position"] = {
                "side": "short",
                "entry": price,
                "qty": DEFAULT_CONTRACTS[symbol],
                "entry_time": now,
                "sl": last.up_Trendline
            }

            utils.log(
                f"🔴 {symbol} SHORT @ {price} | SL: {last.up_Trendline}",
                tg=True
            )


# ================= MAIN =================

def run():

    state = {
        s: {
            "position": None,
            "last_candle_time": None,
            "last_exit_candle": None,
            "balance": 10000,
            "daily_pnl": 0,
            "last_reset_day": datetime.now().date(),
            "trading_enabled": True
        } for s in SYMBOLS
    }

    utils.log(
        "🚀 LIVE BOT STARTED (PAPER MODE)",
        tg=True
    )

    while True:

        try:

            for symbol in SYMBOLS:

                df = utils.fetch_candles(symbol)

                if df is None or len(df) < 100:
                    continue

                latest_candle_time = df.index[-2]

                is_new_candle = (
                    state[symbol]["last_candle_time"]
                    != latest_candle_time
                )

                if is_new_candle:
                    state[symbol]["last_candle_time"] = latest_candle_time

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
                f"🚨 Runtime error: {e}\n{traceback.format_exc()}",
                tg=True
            )

            time.sleep(2)


if __name__ == "__main__":
    run()