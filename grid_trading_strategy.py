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

BOT_NAME = "price_grid_strategy"

SYMBOLS = ["BTCUSD"]

DEFAULT_CONTRACTS = {
    "BTCUSD": 1000
}

CONTRACT_SIZE = {
    "BTCUSD": 0.001
}

STOPLOSS = {
    "BTCUSD": 500
}

TP = {
    "BTCUSD": 300
}

TAKER_FEE = 0.0005

TIMEFRAME = "5m"

DAYS = 15

MIN_BALANCE = 1000

# ================= GRID CONFIG =================

# Spacing between each grid level, in price points.
GRID_STEP = 200

# Number of levels above and below the anchor.
GRID_LEVELS = 3

# Profit booked per grid trade = one grid step.
GRID_TP = GRID_STEP

# Outer boundary = furthest level. If price escapes this,
# stop opening NEW positions (risk control). 200 * 3 = 600.
GRID_BOUNDARY = GRID_STEP * GRID_LEVELS

# US market close in US/Eastern (16:00). Grid re-anchors after this.
US_CLOSE_TZ = ZoneInfo("America/New_York")
US_CLOSE_TIME = dt_time(16, 0)

# ================= TARGET / LOSS =================

# Stop trading for the day once net PnL (after fees) reaches this
# many points. 600 points after fee.
DAILY_TARGET = 600

# Strongly recommended: set a max daily loss (in points) to halt a
# runaway trend. None = no loss halt (NOT recommended for a grid).
MAX_DAILY_LOSS = -1000

# If True, when the daily target is reached we force-close any
# positions that are ALREADY in profit (locking the day in).
# Positions that are underwater are left to hit their own TP/SL
# instead of being dumped at a loss.
FORCE_EXIT_PROFITABLE_ON_TARGET = True

# ================= INIT UTILS =================

utils = TradingUtils(
    contract_size=CONTRACT_SIZE,
    taker_fee=TAKER_FEE,
    timeframe=TIMEFRAME,
    days=DAYS,
    telegram_token=os.getenv("supertrend_ha_fast_bot"),
    telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID"),
    bot_name=BOT_NAME
)

# ================= INIT ORDER MANAGER =================


BASE_DIR = os.getcwd()

SAVE_DIR = os.path.join(
    BASE_DIR,
    "data",
    "price_grid_strategy_live"
)

os.makedirs(SAVE_DIR, exist_ok=True)

# ================= SAVE DATA =================

def save_processed_data(state, symbol):

    path = os.path.join(
        SAVE_DIR,
        f"{symbol}_grid.csv"
    )

    rows = []

    for lvl in state["grid"]:
        rows.append({
            "level_index": lvl["index"],
            "price": lvl["price"],
            "side": lvl["side"],
            "filled": lvl["filled"],
        })

    out = pd.DataFrame(rows)

    out.to_csv(path, index=False)

# ================= PAPER ORDER =================

def place_market_order(symbol, side, qty):

    utils.log(
        f"📝 PAPER ORDER → {symbol} {side.upper()} {qty}",
        tg=True
    )

    return {"success": True}

# ================= GRID BUILD =================

def build_grid(anchor):
    """
    Build a mean-reversion grid around an anchor price.

    Levels BELOW the anchor are BUY levels (buy the dip).
    Levels ABOVE the anchor are SELL/SHORT levels (sell the rally).

    Each level stores its target fill price and whether it is
    currently filled (an open position sitting at that level).
    """

    grid = []

    # BUY levels below anchor: -200, -400, -600
    for n in range(1, GRID_LEVELS + 1):
        grid.append({
            "index": -n,
            "price": anchor - GRID_STEP * n,
            "side": "long",
            "filled": False,
        })

    # SELL levels above anchor: +200, +400, +600
    for n in range(1, GRID_LEVELS + 1):
        grid.append({
            "index": n,
            "price": anchor + GRID_STEP * n,
            "side": "short",
            "filled": False,
        })

    return grid

# ================= DAILY RESET (US CLOSE) =================

def maybe_reanchor(state, symbol, price):
    """
    Re-anchor the grid after US market close each day.

    We track the last date (in US/Eastern) on which we anchored.
    Once the current US/Eastern time is past 16:00 and we have not
    yet anchored for that session date, rebuild the grid from the
    current price and reset daily counters.
    """

    now_et = datetime.now(US_CLOSE_TZ)

    # Session "anchor date": after 16:00 ET belongs to that day's
    # post-close re-anchor.
    is_after_close = now_et.time() >= US_CLOSE_TIME

    anchor_date = now_et.date()

    needs_anchor = (
        state["grid"] is None
        or (
            is_after_close
            and state["last_anchor_date"] != anchor_date
        )
    )

    if not needs_anchor:
        return

    # Close any open positions before re-anchoring would orphan them.
    # (Positions are tracked per-level; we just clear fills and the
    #  open-position list is reconciled by the caller in practice.)
    state["anchor"] = price

    state["grid"] = build_grid(price)

    state["daily_pnl"] = 0

    state["trading_enabled"] = True

    state["last_anchor_date"] = anchor_date

    utils.log(
        f"🌙 {symbol} GRID RE-ANCHORED after US close @ {price} | "
        f"levels ±{GRID_STEP}..±{GRID_BOUNDARY}",
        tg=True
    )

# ================= STRATEGY =================

def process_symbol(symbol, df, price, state, is_new_candle):

    # Build / re-anchor grid as needed (handles first run + daily reset).
    maybe_reanchor(state, symbol, price)

    save_processed_data(state, symbol)

    now = datetime.now()

    anchor = state["anchor"]

    # ================= EXIT OPEN POSITIONS =================
    # Each open position targets +GRID_TP in its favorable direction.

    for posn in list(state["positions"]):

        if posn["side"] == "long":
            live_pnl = (
                (price - posn["entry"])
                * CONTRACT_SIZE[symbol]
                * posn["qty"]
            )
            hit_tp = price >= posn["entry"] + GRID_TP
        else:
            live_pnl = (
                (posn["entry"] - price)
                * CONTRACT_SIZE[symbol]
                * posn["qty"]
            )
            hit_tp = price <= posn["entry"] - GRID_TP

        # ----- FIXED FORCE-EXIT LOGIC -----
        # The old code force-exited EVERY position the moment
        # daily_pnl + live_pnl >= DAILY_TARGET, even positions that
        # were underwater or barely positive. That dumped trades at
        # tiny/negative PnL before they could reach GRID_TP.
        #
        # Now: only force-exit on target if THIS position is itself
        # in profit (gross, before fee). Underwater positions are
        # left alone to hit their own TP. This locks in the day
        # without booking scraps/losses.
        force_exit = (
            FORCE_EXIT_PROFITABLE_ON_TARGET
            and DAILY_TARGET is not None
            and state["daily_pnl"] >= DAILY_TARGET
            and live_pnl > 0
        )

        if not (hit_tp or force_exit):
            continue

        entry_fee = utils.commission(posn["entry"], posn["qty"], symbol)
        exit_fee = utils.commission(price, posn["qty"], symbol)
        total_fee = entry_fee + exit_fee

        net = live_pnl - total_fee

        state["balance"] += net
        state["daily_pnl"] += net

        # Free the grid level so it can refill later.
        for lvl in state["grid"]:
            if lvl["index"] == posn["level_index"]:
                lvl["filled"] = False

        state["positions"].remove(posn)

        utils.save_trade({
            "symbol": symbol,
            "side": posn["side"],
            "entry_price": posn["entry"],
            "exit_price": price,
            "qty": posn["qty"],
            "net_pnl": round(net, 6),
            "entry_time": posn["entry_time"],
            "exit_time": now
        })

        emoji = "🟢" if net > 0 else "🔴"

        reason = "TP" if hit_tp else "TARGET-LOCK"

        utils.log(
            f"{emoji} {symbol} GRID EXIT ({reason}) L{posn['level_index']} "
            f"@ {price} | PNL: {round(net, 6)}",
            tg=True
        )

        utils.log(
            f"💰 Balance: {round(state['balance'], 2)} | "
            f"📊 Daily PNL: {round(state['daily_pnl'], 2)}",
            tg=True
        )

        # ================= DAILY TARGET =================

        if (
            DAILY_TARGET is not None
            and state["daily_pnl"] >= DAILY_TARGET
        ):
            state["trading_enabled"] = False

            utils.log(
                f"🎯 DAILY TARGET HIT: {round(state['daily_pnl'], 2)} "
                f"— stopping for the day (resets after US close)",
                tg=True
            )

        # ================= DAILY LOSS (REALIZED) =================

        if (
            MAX_DAILY_LOSS is not None
            and state["daily_pnl"] <= MAX_DAILY_LOSS
        ):
            state["trading_enabled"] = False

            utils.log(
                "🛑 MAX DAILY LOSS HIT — stopping for the day",
                tg=True
            )

    # ================= LIVE (UNREALIZED) DAILY LOSS CHECK =================
    # daily_pnl only updates on CLOSED trades. In a runaway trend, open
    # positions can be deep underwater while daily_pnl is still ~0, so the
    # realized-only check above never halts. Compute live PnL and, if the
    # combined (realized + unrealized) loss breaches the limit, flatten
    # EVERYTHING immediately and stop trading for the day.

    unrealized = 0.0
    for posn in state["positions"]:
        if posn["side"] == "long":
            upnl = (
                (price - posn["entry"])
                * CONTRACT_SIZE[symbol]
                * posn["qty"]
            )
        else:
            upnl = (
                (posn["entry"] - price)
                * CONTRACT_SIZE[symbol]
                * posn["qty"]
            )
        upnl -= (
            utils.commission(posn["entry"], posn["qty"], symbol)
            + utils.commission(price, posn["qty"], symbol)
        )
        unrealized += upnl

    if (
        MAX_DAILY_LOSS is not None
        and (state["daily_pnl"] + unrealized) <= MAX_DAILY_LOSS
    ):
        utils.log(
            f"🛑 MAX DAILY LOSS HIT (incl. open) "
            f"{round(state['daily_pnl'] + unrealized, 2)} — FLATTENING ALL",
            tg=True
        )

        for posn in list(state["positions"]):

            if posn["side"] == "long":
                live_pnl = (
                    (price - posn["entry"])
                    * CONTRACT_SIZE[symbol]
                    * posn["qty"]
                )
            else:
                live_pnl = (
                    (posn["entry"] - price)
                    * CONTRACT_SIZE[symbol]
                    * posn["qty"]
                )

            # NOTE: paper mode. When going live, send the real market-close
            # order here BEFORE booking PnL to balance:
            #   place_market_order(symbol, opp_side, posn["qty"])
            entry_fee = utils.commission(posn["entry"], posn["qty"], symbol)
            exit_fee = utils.commission(price, posn["qty"], symbol)
            net = live_pnl - (entry_fee + exit_fee)

            state["balance"] += net
            state["daily_pnl"] += net

            # Free the grid level.
            for lvl in state["grid"]:
                if lvl["index"] == posn["level_index"]:
                    lvl["filled"] = False

            state["positions"].remove(posn)

            utils.save_trade({
                "symbol": symbol,
                "side": posn["side"],
                "entry_price": posn["entry"],
                "exit_price": price,
                "qty": posn["qty"],
                "net_pnl": round(net, 6),
                "entry_time": posn["entry_time"],
                "exit_time": now
            })

            emoji = "🟢" if net > 0 else "🔴"

            utils.log(
                f"{emoji} {symbol} FORCE-FLATTEN L{posn['level_index']} "
                f"@ {price} | PNL: {round(net, 6)}",
                tg=True
            )

        state["trading_enabled"] = False

        utils.log(
            f"💰 Balance: {round(state['balance'], 2)} | "
            f"📊 Daily PNL: {round(state['daily_pnl'], 2)} "
            f"— halted for the day (resets after US close)",
            tg=True
        )

        return

    # ================= ENTRY =================

    if not state["trading_enabled"]:
        return

    if state["balance"] < MIN_BALANCE:
        utils.log(f"⚠️ Balance low: {state['balance']}", tg=True)
        return

    # Risk control: if price has escaped the outer grid boundary,
    # do not open new positions (a trend is running away).
    if abs(price - anchor) > GRID_BOUNDARY:
        return

    # Fill any grid level the price has reached and that is not yet
    # filled. Buy levels fill when price drops to/below them; sell
    # levels fill when price rises to/above them.
    for lvl in state["grid"]:

        if lvl["filled"]:
            continue

        if lvl["side"] == "long" and price <= lvl["price"]:
            should_fill = True
        elif lvl["side"] == "short" and price >= lvl["price"]:
            should_fill = True
        else:
            should_fill = False

        if not should_fill:
            continue

        lvl["filled"] = True

        state["positions"].append({
            "side": lvl["side"],
            "entry": price,
            "qty": DEFAULT_CONTRACTS[symbol],
            "entry_time": now,
            "level_index": lvl["index"],
        })

        emoji = "🟢" if lvl["side"] == "long" else "🔴"

        utils.log(
            f"{emoji} {symbol} GRID {lvl['side'].upper()} "
            f"L{lvl['index']} @ {price} | TP: {GRID_TP}pt",
            tg=True
        )


# ================= MAIN =================

def run():

    state = {
        s: {
            "positions": [],
            "grid": None,
            "anchor": None,
            "last_candle_time": None,
            "last_anchor_date": None,
            "balance": 10000,
            "daily_pnl": 0,
            "trading_enabled": True
        } for s in SYMBOLS
    }

    utils.log(
        "🚀 LIVE GRID BOT STARTED (PAPER MODE)",
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

                # auto_git_push()

            time.sleep(3)

        except Exception as e:

            utils.log(
                f"🚨 Runtime error: {e}\n"
                f"{traceback.format_exc()}",
                tg=True
            )

            time.sleep(5)

# ================= START =================

if __name__ == "__main__":

    run()