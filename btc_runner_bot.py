import os
import time
import pandas as pd
import numpy as np

from datetime import datetime, UTC
from datetime import time as dt_time

from zoneinfo import ZoneInfo

import pandas_ta as ta

from dotenv import load_dotenv

import traceback
import subprocess

from utils import TradingUtils

load_dotenv()

# ================= CONFIG =================

BOT_NAME = "btc_runner_bot"

SYMBOLS = ["BTCUSD"]

DEFAULT_CONTRACTS = {
    "BTCUSD": 100
}

CONTRACT_SIZE = {
    "BTCUSD": 0.001
}

STOPLOSS = {
    "BTCUSD": 500
}

TP = {
    "BTCUSD": 300000
}

TAKER_FEE = 0.0005

TIMEFRAME = "15m"

DAYS = 15

MIN_BALANCE = 100

# ================= TARGET / LOSS =================

DAILY_TARGET = None

MAX_DAILY_LOSS = None

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

BASE_DIR = os.getcwd()

SAVE_DIR = os.path.join(
    BASE_DIR,
    "data",
    "btc_runner_bot"
)

os.makedirs(SAVE_DIR, exist_ok=True)

# ================= SAVE DATA =================

def save_processed_data(data, symbol):

    path = os.path.join(
        SAVE_DIR,
        f"{symbol}_processed.csv"
    )

    out = pd.DataFrame({
        "time": data.index,
        "HA_open": data["HA_open"],
        "HA_high": data["HA_high"],
        "HA_low": data["HA_low"],
        "HA_close": data["HA_close"],
        "trendline": data["Trendline"],
    })

    out.to_csv(path, index=False)

# ================= PAPER ORDER =================

def place_market_order(symbol, side, qty):

    utils.log(
        f"📝 PAPER ORDER → {symbol} {side.upper()} {qty}",
        tg=True
    )

    return {"success": True}

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


def calculate_trendline(df):

    # ================= HEIKIN ASHI =================

    ha = ta.ha(
        df["Open"],
        df["High"],
        df["Low"],
        df["Close"]
    ).reset_index(drop=True)

    # ================= FRACTALS =================

    ha["high_fractal"] = np.nan
    ha["low_fractal"] = np.nan

    # NON-REPAINTING FRACTALS

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

        # CONFIRM AFTER 2 CANDLES

        if is_high:
            ha.loc[i + 2, "high_fractal"] = ha.loc[i, "HA_high"]

        if is_low:
            ha.loc[i + 2, "low_fractal"] = ha.loc[i, "HA_low"]

    # ================= TRENDLINE =================

    ha["Trendline"] = np.nan

    last_high_fractal = np.nan
    last_low_fractal = np.nan

    trendline = ha.loc[0, "HA_close"]

    for i in range(1, len(ha)):

        # UPDATE FRACTALS

        if not np.isnan(ha.loc[i, "high_fractal"]):
            last_high_fractal = ha.loc[i, "high_fractal"]

        if not np.isnan(ha.loc[i, "low_fractal"]):
            last_low_fractal = ha.loc[i, "low_fractal"]

        current_close = ha.loc[i, "HA_close"]

        prev_close = ha.loc[i - 1, "HA_close"]

        # ================= BULLISH BREAK =================

        if (
            not np.isnan(last_high_fractal)
            and prev_close <= last_high_fractal
            and current_close > last_high_fractal
            and current_close > trendline
            and not np.isnan(last_low_fractal)
        ):

            trendline = last_low_fractal

        # ================= BEARISH BREAK =================

        elif (
            not np.isnan(last_low_fractal)
            and prev_close >= last_low_fractal
            and current_close < last_low_fractal
            and current_close < trendline
            and not np.isnan(last_high_fractal)
        ):

            trendline = last_high_fractal

        ha.loc[i, "Trendline"] = trendline

    return ha

# ================= STRATEGY =================

def process_symbol(symbol, df, price, state, is_new_candle):

    reset_daily_state(state)

    ha = calculate_trendline(df)
    # save_processed_data(ha, symbol)

    current = ha.iloc[-1]
    last = ha.iloc[-2]
    prev = ha.iloc[-3]

    pos = state["position"]

    now = datetime.now()

    # ================= EXIT =================

    if pos:

        exit_trade = False
        pnl = 0

        if pos["side"] == "long":

            live_pnl = (
                (price - pos["entry"])
                * CONTRACT_SIZE[symbol]
                * pos["qty"]
            )

        else:

            live_pnl = (
                (pos["entry"] - price)
                * CONTRACT_SIZE[symbol]
                * pos["qty"]
            )

        if (
            DAILY_TARGET is not None
            and state["daily_pnl"] + live_pnl >= DAILY_TARGET
        ):

            pnl = live_pnl
            exit_trade = True

            utils.log(
                "🎯 DAILY TARGET REACHED DURING LIVE TRADE",
                tg=True
            )

        elif (
            pos["side"] == "long"
            and (
                price < last.Trendline
                or price >= pos["entry"] + TP[symbol]
            )
        ):

            pnl = live_pnl
            exit_trade = True

        elif (
            pos["side"] == "short"
            and (
                price > last.Trendline
                or price <= pos["entry"] - TP[symbol]
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

            total_fee = entry_fee + exit_fee

            net = pnl - total_fee

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

            utils.log(
                f"💰 Balance: {round(state['balance'], 2)}",
                tg=True
            )

            utils.log(
                f"📊 Daily PNL: {round(state['daily_pnl'], 2)}",
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

        balance = state["balance"]

        if balance < MIN_BALANCE:

            utils.log(
                f"⚠️ Balance low: {balance}",
                tg=True
            )

            return

        # ================= LONG ENTRY =================

        if (
            prev.HA_close <= prev.Trendline
            and last.HA_close > last.Trendline
        ):

            state["position"] = {
                "side": "long",
                "entry": price,
                "qty": DEFAULT_CONTRACTS[symbol],
                "entry_time": now,
                "sl": last.Trendline
            }

            utils.log(
                f"🟢 {symbol} LONG @ {price} | "
                f"SL: {last.Trendline}",
                tg=True
            )

        # ================= SHORT ENTRY =================

        elif (
            prev.HA_close >= prev.Trendline
            and last.HA_close < last.Trendline
        ):

            state["position"] = {
                "side": "short",
                "entry": price,
                "qty": DEFAULT_CONTRACTS[symbol],
                "entry_time": now,
                "sl": last.Trendline
            }

            utils.log(
                f"🔴 {symbol} SHORT @ {price} | "
                f"SL: {last.Trendline}",
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

    # ================= SESSION STATE =================

    session_state = {
        "uk_started": False,
        "uk_ended": False,
        "us_started": False,
        "us_ended": False
    }

    utils.log(
        "🚀 LIVE BOT STARTED (PAPER MODE)",
        tg=True
    )

    while True:

        try:

            # ================= SESSION ALERTS =================

            # monitor_sessions(session_state)

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
                f"🚨 Runtime error: {e}\n"
                f"{traceback.format_exc()}",
                tg=True
            )

            time.sleep(2)

# ================= START =================

if __name__ == "__main__":

    run()