import os
import time
import numpy as np
import pandas_ta as ta
import traceback

from datetime import datetime
from dotenv import load_dotenv

from utils import TradingUtils
from order_manager import OrderManager

load_dotenv()

BOT_NAME = "price_trend_following_strategy"

SYMBOLS = ["BTCUSD"]

DEFAULT_CONTRACTS = {
    "BTCUSD": 1
}

CONTRACT_SIZE = {
    "BTCUSD": 0.001
}

TP = {
    "BTCUSD": 300
}

TAKER_FEE = 0.0005
TIMEFRAME = "5m"
DAYS = 15
MIN_BALANCE = 1000

DAILY_TARGET = 500
MAX_DAILY_LOSS = None


utils = TradingUtils(
    contract_size=CONTRACT_SIZE,
    taker_fee=TAKER_FEE,
    timeframe=TIMEFRAME,
    days=DAYS,
    telegram_token=os.getenv("price_trend_following_strategy_live_bot"),
    telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID"),
    bot_name=BOT_NAME
)

orders = OrderManager()


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

    ha = ta.ha(
        df["Open"],
        df["High"],
        df["Low"],
        df["Close"]
    ).reset_index(drop=True)

    ha["high_fractal"] = np.nan
    ha["low_fractal"] = np.nan

    for i in range(2, len(ha) - 2):

        if (
            ha.loc[i, "HA_high"] > ha.loc[i-1, "HA_high"]
            and ha.loc[i, "HA_high"] > ha.loc[i-2, "HA_high"]
            and ha.loc[i, "HA_high"] > ha.loc[i+1, "HA_high"]
            and ha.loc[i, "HA_high"] > ha.loc[i+2, "HA_high"]
        ):
            ha.loc[i+2, "high_fractal"] = ha.loc[i, "HA_high"]

        if (
            ha.loc[i, "HA_low"] < ha.loc[i-1, "HA_low"]
            and ha.loc[i, "HA_low"] < ha.loc[i-2, "HA_low"]
            and ha.loc[i, "HA_low"] < ha.loc[i+1, "HA_low"]
            and ha.loc[i, "HA_low"] < ha.loc[i+2, "HA_low"]
        ):
            ha.loc[i+2, "low_fractal"] = ha.loc[i, "HA_low"]

    ha["Trendline"] = np.nan

    trendline = ha.loc[0, "HA_close"]

    last_high_fractal = np.nan
    last_low_fractal = np.nan

    for i in range(1, len(ha)):

        if not np.isnan(ha.loc[i, "high_fractal"]):
            last_high_fractal = ha.loc[i, "high_fractal"]

        if not np.isnan(ha.loc[i, "low_fractal"]):
            last_low_fractal = ha.loc[i, "low_fractal"]

        current_close = ha.loc[i, "HA_close"]
        prev_close = ha.loc[i-1, "HA_close"]

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
        ha.loc[i, "up_Trendline"] = trendline + 50
        ha.loc[i, "down_Trendline"] = trendline - 50

    return ha


def close_position(symbol, pos):

    try:

        # Validate required keys in pos
        required_keys = ["product_id", "side", "qty"]
        for key in required_keys:
            if key not in pos:
                raise ValueError(f"Missing required key in pos: '{key}'")

        product_id = pos["product_id"]

        # Cancel bracket orders if any exist
        if orders.has_open_bracket_order(product_id):

            utils.log(
                f"Cancelling bracket order for {symbol}",
                tg=True
            )

            cancel_res = orders.cancel_bracket_order(
                product_id=product_id
            )

            if not cancel_res or not cancel_res.get("success"):
                utils.log(
                    f"EXIT FAILED: Bracket cancel failed for {symbol}",
                    tg=True
                )
                return {"success": False, "error": "bracket_cancel_failed"}

            # Give exchange 2 seconds to process cancellation
            time.sleep(2)

        # Fetch live position to confirm it still exists
        live_pos = orders.get_position(product_id)

        if live_pos is None:
            # Position already closed (likely by SL/TP on exchange)
            utils.log(
                f"Position already closed on exchange for {symbol}",
                tg=True
            )
            return {"success": True, "already_closed": True}

        # Determine exit side from live position
        pos_side = live_pos["side"]

        if pos_side == "buy":
            exit_side = "sell"
        elif pos_side == "sell":
            exit_side = "buy"
        else:
            raise ValueError(f"Unknown position side: '{pos_side}'")

        # FIX 3: Use "market" not "market_order" — place_order maps "market" → "market_order"
        # Passing "market_order" directly hits the else branch → sends "limit_order" → 400 error
        res = orders.place_order(
            size=live_pos["size"],
            side=exit_side,
            product_id=product_id,
            order_type="market",   # ← was "market_order", caused 400 Bad Request
            reduce_only=True
        )

        if res and res.get("success"):

            utils.log(
                f"EXIT COMPLETE for {symbol}",
                tg=True
            )

            return {"success": True}

        else:

            utils.log(
                f"EXIT FAILED for {symbol} | Response: {res}",
                tg=True
            )

            return {"success": False, "error": "place_order_failed", "response": res}

    except Exception as e:

        utils.log(
            f"EXIT ERROR for {symbol} | {e}",
            tg=True
        )

        return {"success": False, "error": str(e)}


def process_symbol(
    symbol,
    df,
    price,
    state,
    is_new_candle
):

    reset_daily_state(state)

    ha = calculate_trendline(df)

    last = ha.iloc[-2]
    prev = ha.iloc[-3]

    pos = state["position"]

    now = datetime.now()

    if pos:

        exit_trade = False

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
            exit_trade = True

        elif (
            pos["side"] == "long"
            and (
                price < last.down_Trendline
                or price >= pos["entry"] + TP[symbol]
            )
        ):
            exit_trade = True

        elif (
            pos["side"] == "short"
            and (
                price > last.up_Trendline
                or price <= pos["entry"] - TP[symbol]
            )
        ):
            exit_trade = True

        if exit_trade:

            # FIX 2: Only clear state if exit succeeded
            # Previously state was always cleared even on failed exits
            # causing orphaned positions with no management
            result = close_position(symbol, pos)

            if result.get("success"):

                state["daily_pnl"] += live_pnl
                state["position"] = None
                state["last_exit_candle"] = df.index[-1]

                utils.log(
                    f"📊 Daily PnL: {state['daily_pnl']:.2f}",
                    tg=True
                )

            else:

                utils.log(
                    f"⚠️ Exit failed for {symbol}, will retry next loop | "
                    f"Error: {result.get('error')}",
                    tg=True
                )

            # Always return — never enter a new trade while exit is pending
            return

    if not pos and is_new_candle:

        if not state["trading_enabled"]:
            return

        if state["balance"] < MIN_BALANCE:
            return

        if state.get("last_exit_candle") == df.index[-1]:
            return

        product_id = orders.get_product_id(symbol)

        if not product_id:
            return

        if (prev.HA_close <= prev.up_Trendline
            and last.HA_close > last.up_Trendline):

            entry = orders.place_order(
                size=DEFAULT_CONTRACTS[symbol],
                side="buy",
                product_id=product_id
            )

            if entry and entry.get("success"):

                orders.place_bracket_order(
                    product_id=product_id,
                    stop_loss_price=last.down_Trendline,
                    take_profit_price=price + TP[symbol]
                )

                state["position"] = {
                    "side": "long",
                    "entry": price,
                    "qty": DEFAULT_CONTRACTS[symbol],
                    "entry_time": now,
                    "product_id": product_id
                }

                utils.log(
                    f"✅ LONG ENTRY → {symbol} @ {price}",
                    tg=True
                )

        elif (prev.HA_close >= prev.down_Trendline
              and last.HA_close < last.down_Trendline):

            entry = orders.place_order(
                size=DEFAULT_CONTRACTS[symbol],
                side="sell",
                product_id=product_id
            )

            if entry and entry.get("success"):

                orders.place_bracket_order(
                    product_id=product_id,
                    stop_loss_price=last.up_Trendline,
                    take_profit_price=price - TP[symbol]
                )

                state["position"] = {
                    "side": "short",
                    "entry": price,
                    "qty": DEFAULT_CONTRACTS[symbol],
                    "entry_time": now,
                    "product_id": product_id
                }

                utils.log(
                    f"✅ SHORT ENTRY → {symbol} @ {price}",
                    tg=True
                )


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
        }
        for s in SYMBOLS
    }

    utils.log(
        "🚀 LIVE BOT STARTED",
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

            time.sleep(3)

        except Exception as e:

            utils.log(
                f"🚨 Runtime error: {e}\n{traceback.format_exc()}",
                tg=True
            )

            time.sleep(5)


if __name__ == "__main__":
    run()