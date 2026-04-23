import os
import time
import pandas as pd
import numpy as np
from datetime import datetime
import pandas_ta as ta
from dotenv import load_dotenv
import traceback
import subprocess

from utils import TradingUtils

load_dotenv()

# ================= CONFIG =================

BOT_NAME = "grid_strategy"

SYMBOLS = ["BTCUSD", "ETHUSD"]

CONTRACT_SIZE = {"BTCUSD": 0.001, "ETHUSD": 0.01}
DEFAULT_CONTRACTS = {"BTCUSD": 100, "ETHUSD": 100}

STOPLOSS = {"BTCUSD": 500, "ETHUSD": 50}
TAKER_FEE = 0.0005

TIMEFRAME = "5m"
DAYS = 5

MIN_BALANCE = 5000
BALANCE = 500

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

        res = subprocess.run("git push origin main", shell=True)

        if res.returncode == 0:
            utils.log("✅ Git Push Done", tg=True)

        last_git_push = time.time()

    except Exception as e:
        utils.log(f"Git Error: {e}")

BASE_DIR = os.getcwd()
SAVE_DIR = os.path.join(BASE_DIR, "data", "grid_strategy")
os.makedirs(SAVE_DIR, exist_ok=True)

# ================= ✅ UPGRADED LOGGER =================

def save_processed_data(data, symbol):
    path = os.path.join(SAVE_DIR, f"{symbol}_processed.csv")

    df = data.copy()

    out = pd.DataFrame({
        "time": df.index,
        "HA_open": df.get("HA_open"),
        "HA_high": df.get("HA_high"),
        "HA_low": df.get("HA_low"),
        "HA_close": df.get("HA_close"),
        "trendline": df.get("Trendline"),
        "structure": df.get("Structure", None),
        "swing_high": df.get("SWING_HIGH", None),
        "swing_low": df.get("SWING_LOW", None),
    })

    # Trend direction
    out["trend"] = np.where(
        out["HA_close"] > out["trendline"], "UP",
        np.where(out["HA_close"] < out["trendline"], "DOWN", "SIDE")
    )

    # Signals (for analysis only)
    out["long_signal"] = (
        (out["HA_close"] > out["trendline"]) &
        (out["HA_close"] > out["HA_open"])
    )

    out["short_signal"] = (
        (out["HA_close"] < out["trendline"]) &
        (out["HA_close"] < out["HA_open"])
    )

    out.fillna("", inplace=True)
    out.to_csv(path, index=False)

# ================= INIT UTILS =================

utils = TradingUtils(
    contract_size=CONTRACT_SIZE,
    taker_fee=TAKER_FEE,
    timeframe=TIMEFRAME,
    days=DAYS,
    telegram_token=os.getenv("testing_strategy_my_aglo_bot"),
    telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID"),
    bot_name=BOT_NAME
)

# ================= PAPER ORDER =================

def place_market_order(symbol, side, qty):
    utils.log(f"📝 PAPER ORDER → {symbol} {side.upper()} {qty}", tg=True)
    return {"success": True}

# ================= TRENDLINE =================

def calculate_structure_trendline(df):

    df = ta.ha(df["Open"], df["High"], df["Low"], df["Close"]).reset_index(drop=True)
    
    order = 7

    df["SWING_HIGH"] = df["HA_high"][
        df["HA_high"] == df["HA_high"].rolling(window=2 * order + 1, center=True).max()
    ]

    df["SWING_LOW"] = df["HA_low"][
        df["HA_low"] == df["HA_low"].rolling(window=2 * order + 1, center=True).min()
    ]

    df["Structure"] = ""
    df["Trendline"] = np.nan

    last_HH = None
    last_LL = None
    last_HL = None
    last_LH = None

    trend = None

    trendline = df.loc[0, "HA_close"]
    df.loc[0, "Trendline"] = trendline

    for i in range(len(df)):

        if not np.isnan(df.loc[i, "SWING_HIGH"]):

            current_high = df.loc[i, "SWING_HIGH"]

            if last_HH is None:
                last_HH = current_high
                df.loc[i, "Structure"] = "H"

            else:
                if current_high > last_HH:
                    df.loc[i, "Structure"] = "HH"
                    trend = "UP"

                    if last_HL is not None:
                        trendline = last_HL

                    last_HH = current_high

                else:
                    df.loc[i, "Structure"] = "LH"
                    last_LH = current_high

        elif not np.isnan(df.loc[i, "SWING_LOW"]):

            current_low = df.loc[i, "SWING_LOW"]

            if last_LL is None:
                last_LL = current_low
                df.loc[i, "Structure"] = "L"

            else:
                if current_low < last_LL:
                    df.loc[i, "Structure"] = "LL"
                    trend = "DOWN"

                    if last_LH is not None:
                        trendline = last_LH

                    last_LL = current_low

                else:
                    df.loc[i, "Structure"] = "HL"
                    last_HL = current_low

        if trend == "UP" and last_HL is not None:
            trendline = last_HL
        elif trend == "DOWN" and last_LH is not None:
            trendline = last_LH

        df.loc[i, "Trendline"] = trendline

    return df

# ================= STRATEGY =================

def process_symbol(symbol, df, price, state, is_new_candle):

    ha = calculate_structure_trendline(df)

    # # ✅ Save only on new candle
    # if is_new_candle:
    #     save_processed_data(ha, symbol)

    last = ha.iloc[-2]
    prev = ha.iloc[-3]

    pos = state["position"]
    now = datetime.now()

    # ================= EXIT =================
    if pos:

        exit_trade = False
        pnl = 0

        if pos["side"] == "long" and price < last.Trendline:

            pnl = (price - pos["entry"]) * CONTRACT_SIZE[symbol] * pos["qty"]
            exit_trade = True

        elif pos["side"] == "short" and price > last.Trendline:

            pnl = (pos["entry"] - price) * CONTRACT_SIZE[symbol] * pos["qty"]
            exit_trade = True

        if exit_trade:

            entry_fee = utils.commission(pos["entry"], pos["qty"], symbol)
            exit_fee  = utils.commission(price, pos["qty"], symbol)

            total_fee = entry_fee + exit_fee
            net = pnl - total_fee

            state["balance"] += net

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
            utils.log(f"{emoji} {symbol} EXIT @ {price} | PNL: {round(net,6)}", tg=True)
            utils.log(f"💰 Balance: {round(state['balance'],2)}", tg=True)

            state["position"] = None
            state["last_exit_candle"] = df.index[-1]

            return

    # ================= ENTRY =================
    if not pos and is_new_candle:

        if state.get("last_exit_candle") == df.index[-1]:
            return

        balance = state["balance"]

        if balance < BALANCE:
            utils.log(f"⚠️ Balance low: {balance}, required: {MIN_BALANCE}")
            return

        if last.HA_close > last.Trendline and last.HA_close > prev.HA_close and last.HA_close > prev.HA_open:

            state["position"] = {
                "side": "long",
                "entry": price,
                "qty": DEFAULT_CONTRACTS[symbol],
                "entry_time": now,
                "sl": price - STOPLOSS[symbol]
            }

            utils.log(f"🟢 {symbol} LONG @ {price} | SL: {price - STOPLOSS[symbol]}", tg=True)

        elif last.HA_close < last.Trendline and last.HA_close < prev.HA_close and last.HA_close < prev.HA_open:

            state["position"] = {
                "side": "short",
                "entry": price,
                "qty": DEFAULT_CONTRACTS[symbol],
                "entry_time": now,
                "sl": price + STOPLOSS[symbol]
            }

            utils.log(f"🔴 {symbol} SHORT @ {price} | SL: {price + STOPLOSS[symbol]}", tg=True)

# ================= MAIN =================

def run():

    state = {
        s: {
            "position": None,
            "last_candle_time": None,
            "last_exit_candle": None,
            "balance": 5000
        } for s in SYMBOLS
    }

    utils.log("🚀 LIVE BOT STARTED (PAPER MODE)", tg=True)

    while True:

        try:
            for symbol in SYMBOLS:

                df = utils.fetch_candles(symbol)

                if df is None or len(df) < 100:
                    continue

                latest_candle_time = df.index[-2]

                is_new_candle = state[symbol]["last_candle_time"] != latest_candle_time

                if is_new_candle:
                    state[symbol]["last_candle_time"] = latest_candle_time

                price = utils.fetch_price(symbol)

                if price is None:
                    continue

                process_symbol(symbol, df, price, state[symbol], is_new_candle)

            time.sleep(3)

        except Exception as e:
            utils.log(f"🚨 Runtime error: {e}", tg=True)
            time.sleep(5)

# ================= START =================

if __name__ == "__main__":
    run()