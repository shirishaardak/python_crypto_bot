import os
import time
import pandas as pd
import numpy as np

from datetime import datetime

from dotenv import load_dotenv

import traceback

from utils import TradingUtils

load_dotenv()

# ================= CONFIG =================

BOT_NAME = "liquidity_sweep_strategy"

SYMBOLS = ["BTCUSD", "ETHUSD"]

DEFAULT_CONTRACTS = {
    "BTCUSD": 1000,
    "ETHUSD": 1000
}

CONTRACT_SIZE = {
    "BTCUSD": 0.001,
    "ETHUSD": 0.01
}

TAKER_FEE = 0.0005

TIMEFRAME = "5m"

DAYS = 15

MIN_BALANCE = 1000

# ================= STRATEGY PARAMS =================

# Bars on each side that define an "obvious" swing high/low (where stops cluster).
SWING_LB = {
    "BTCUSD": 10,
    "ETHUSD": 10
}

# Minimum reward:risk required to take a trade. This is the main quality filter
# and the reason we trade rarely — most candles produce no qualifying setup.
MIN_RR = {
    "BTCUSD": 2.0,
    "ETHUSD": 2.0
}

# ================= TARGET / LOSS =================
# NOTE: With the sweep strategy, the STOP and TARGET come from the setup itself
# (stop = beyond the sweep wick, target = opposing liquidity). TP below is a
# hard safety cap in points; the structural target usually triggers first.

TP = {
    "BTCUSD": 500,
    "ETHUSD": 40
}

TRAIL_TRIGGER = {
    "BTCUSD": 200,   # Trail activates only after this many points in profit
    "ETHUSD": 15
}

TRAIL_DISTANCE = {
    "BTCUSD": 100,   # SL moves in these point steps
    "ETHUSD": 8
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


# ================= SWING LEVELS =================
# Replaces the Heikin-Ashi trendline. Finds the most extreme confirmed swing
# high and swing low in the lookback window — the levels the crowd watches and
# where stop orders pile up. Operates on raw OHLC from df (same df you already
# pass in). Expects columns: "High", "Low" (matching your existing code).

def swing_levels(df, lb):
    """Return (swing_high, swing_low) using a fractal-style pivot:
    a bar is a swing high if its High is the max of the window [i-lb, i+lb]
    and strictly exceeds the window edges (a real pivot, not a flat shelf).
    We take the most extreme pivot, since that is the most 'obvious' level."""

    highs = df["High"].values
    lows = df["Low"].values
    n = len(df)

    swing_high = np.nan
    swing_low = np.nan

    found_highs = []
    found_lows = []

    for i in range(lb, n - lb):

        win_high = highs[i - lb: i + lb + 1]
        win_low = lows[i - lb: i + lb + 1]

        h = highs[i]
        l = lows[i]

        if h >= win_high.max() and h > win_high.min():
            found_highs.append(h)

        if l <= win_low.min() and l < win_low.max():
            found_lows.append(l)

    if found_highs:
        swing_high = max(found_highs)

    if found_lows:
        swing_low = min(found_lows)

    return swing_high, swing_low


# ================= SIGNAL =================
# Detects a sweep + rejection on the just-CLOSED candle.
#
# SHORT: the closed candle's HIGH pokes ABOVE the swing high (sweeping buy-stops
#        and trapping breakout longs) but the candle CLOSES back BELOW it.
# LONG:  the closed candle's LOW pokes BELOW the swing low (sweeping sell-stops
#        and trapping breakout shorts) but the candle CLOSES back ABOVE it.
#
# Levels are computed from history EXCLUDING the trigger candle, so the trigger
# can't define its own level. Returns a dict or None.

def detect_signal(df, lb, min_rr):

    if len(df) < 2 * lb + 3:
        return None

    # df.iloc[-1] is the forming candle in a live feed; we trade on the just-
    # CLOSED candle = iloc[-2], exactly like your trendline code used iloc[-2].
    trigger = df.iloc[-2]
    history = df.iloc[:-2]   # levels from confirmed history only

    swing_high, swing_low = swing_levels(history, lb)

    if np.isnan(swing_high) or np.isnan(swing_low):
        return None

    # ---- SHORT setup ----
    if trigger["High"] > swing_high and trigger["Close"] < swing_high:

        entry = float(trigger["Close"])
        sl = float(trigger["High"])          # just beyond the wick that trapped longs
        target = float(swing_low)            # opposing liquidity pool
        risk = sl - entry
        reward = entry - target

        if risk > 0 and reward / risk >= min_rr:
            return {
                "side": "short",
                "entry": entry,
                "sl": sl,
                "target": target,
                "rr": reward / risk
            }

    # ---- LONG setup ----
    if trigger["Low"] < swing_low and trigger["Close"] > swing_low:

        entry = float(trigger["Close"])
        sl = float(trigger["Low"])
        target = float(swing_high)
        risk = entry - sl
        reward = target - entry

        if risk > 0 and reward / risk >= min_rr:
            return {
                "side": "long",
                "entry": entry,
                "sl": sl,
                "target": target,
                "rr": reward / risk
            }

    return None


# ================= STRATEGY =================

def process_symbol(symbol, df, price, state, is_new_candle):

    reset_daily_state(state)

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

        # 1) Daily target hit
        if (
            DAILY_TARGET is not None
            and state["daily_pnl"] + live_pnl >= DAILY_TARGET
        ):

            pnl = live_pnl
            exit_trade = True

        # 2) LONG exits: stop, structural target, or hard TP cap
        elif (
            pos["side"] == "long"
            and (
                price <= pos["sl"]
                or price >= pos["target"]
                or price >= pos["entry"] + TP[symbol]
            )
        ):

            pnl = live_pnl
            exit_trade = True

        # 3) SHORT exits: stop, structural target, or hard TP cap
        elif (
            pos["side"] == "short"
            and (
                price >= pos["sl"]
                or price <= pos["target"]
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

        # Only evaluate once per newly-closed candle (avoids re-firing intra-candle)
        if not is_new_candle:
            return

        if state.get("last_exit_candle") == df.index[-1]:
            return

        if state["balance"] < MIN_BALANCE:
            return

        sig = detect_signal(df, SWING_LB[symbol], MIN_RR[symbol])

        if sig is None:
            return

        if sig["side"] == "long":

            state["position"] = {
                "side": "long",
                "entry": price,                       # fill at current price (paper)
                "qty": DEFAULT_CONTRACTS[symbol],
                "entry_time": now,
                "sl": sig["sl"],
                "target": sig["target"]
            }

            utils.log(
                f"🟢 {symbol} LONG @ {price} | SL: {round(sig['sl'], 2)} "
                f"| TGT: {round(sig['target'], 2)} | RR: {round(sig['rr'], 2)}",
                tg=True
            )

        elif sig["side"] == "short":

            state["position"] = {
                "side": "short",
                "entry": price,
                "qty": DEFAULT_CONTRACTS[symbol],
                "entry_time": now,
                "sl": sig["sl"],
                "target": sig["target"]
            }

            utils.log(
                f"🔴 {symbol} SHORT @ {price} | SL: {round(sig['sl'], 2)} "
                f"| TGT: {round(sig['target'], 2)} | RR: {round(sig['rr'], 2)}",
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
        "🚀 LIVE BOT STARTED (PAPER MODE) — Liquidity Sweep",
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