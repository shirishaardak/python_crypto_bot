import os
import time
import requests
import pandas as pd
import pandas_ta as ta
from datetime import datetime, timedelta
from dotenv import load_dotenv
import traceback

load_dotenv()

# ================= TELEGRAM =================
TELEGRAM_TOKEN = os.getenv("BOT_TOK")
TELEGRAM_CHAT_ID = os.getenv("CHAT_")

_last_tg = {}

def send_telegram(msg, key=None, cooldown=30):

    try:
        if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
            return

        now = time.time()

        if key:
            if key in _last_tg and now - _last_tg[key] < cooldown:
                return
            _last_tg[key] = now

        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

        requests.post(
            url,
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": msg,
                "parse_mode": "Markdown"
            },
            timeout=5
        )

    except:
        print("Telegram Error")


# ================= SETTINGS =================
SYMBOLS = ["BTCUSD", "ETHUSD"]

DEFAULT_CONTRACTS = {"BTCUSD": 100, "ETHUSD": 100}

CONTRACT_SIZE = {"BTCUSD": 0.001, "ETHUSD": 0.01}

TAKER_FEE = 0.0005

TIMEFRAME = "1m"
DAYS = 5

STOP_LOSS = {"BTCUSD": 150, "ETHUSD": 10}
TRAIL_STEP = {"BTCUSD": 1, "ETHUSD": 1}

BASE_DIR = os.getcwd()

SAVE_DIR = os.path.join(BASE_DIR, "data", "supertrend_pro_bot")
os.makedirs(SAVE_DIR, exist_ok=True)

TRADE_CSV = os.path.join(SAVE_DIR, "live_trades.csv")


# ================= UTIL =================
def log(msg):
    print(f"[{datetime.now()}] {msg}")


def commission(price, qty, symbol):
    return price * CONTRACT_SIZE[symbol] * qty * TAKER_FEE


def save_trade(trade):

    df = pd.DataFrame([trade])

    df.to_csv(
        TRADE_CSV,
        mode="a",
        header=not os.path.exists(TRADE_CSV),
        index=False
    )


# ================= DATA =================
def fetch_price(symbol):

    r = requests.get(
        f"https://api.india.delta.exchange/v2/tickers/{symbol}",
        timeout=5
    )

    return float(r.json()["result"]["mark_price"])


def fetch_candles(symbol, resolution="1m", days=5):

    start = int((datetime.now() - timedelta(days=days)).timestamp())

    r = requests.get(
        "https://api.india.delta.exchange/v2/history/candles",
        params={
            "resolution": resolution,
            "symbol": symbol,
            "start": start,
            "end": int(time.time())
        },
        timeout=10
    )

    df = pd.DataFrame(
        r.json()["result"],
        columns=["time", "open", "high", "low", "close", "volume"]
    )

    df["time"] = pd.to_datetime(df["time"], unit="s")

    df.set_index("time", inplace=True)

    df = df.astype(float)

    df = df.sort_index()

    return df


# ================= TREND CACHE =================
trend_cache = {}

def fetch_trend(symbol):

    now = time.time()

    if symbol in trend_cache:
        if now - trend_cache[symbol]["time"] < 300:
            return trend_cache[symbol]["value"]

    df = fetch_candles(symbol, "5m", 2)

    st = ta.supertrend(
        df["high"],
        df["low"],
        df["close"],
        length=10,
        multiplier=3
    )

    st_dir_col = [c for c in st.columns if "SUPERTd" in c][0]

    trend = st[st_dir_col].iloc[-1]

    trend_cache[symbol] = {
        "value": trend,
        "time": now
    }

    return trend


# ================= INDICATORS =================
def calculate_indicators(df):

    data = df.copy()

    data = data.sort_index()

    st = ta.supertrend(
        data["high"],
        data["low"],
        data["close"],
        length=10,
        multiplier=3
    )

    st_dir_col = [c for c in st.columns if "SUPERTd" in c][0]

    data["ST_DIR"] = st[st_dir_col]

    adx = ta.adx(data["high"], data["low"], data["close"])

    data["ADX"] = adx["ADX_14"]

    data["RSI"] = ta.rsi(data["close"], length=14)

    data["VWAP"] = ta.vwap(
        data["high"],
        data["low"],
        data["close"],
        data["volume"]
    )

    return data


# ================= STRATEGY =================
def process_symbol(symbol, df, price, state):

    data = calculate_indicators(df)

    if len(data) < 50:
        return

    last = data.iloc[-2]
    prev = data.iloc[-3]

    candle_time = data.index[-2]

    pos = state["position"]

    st_now = last.ST_DIR
    st_prev = prev.ST_DIR

    adx = last.ADX
    rsi = last.RSI
    vwap = last.VWAP

    trend5m = fetch_trend(symbol)

    if pos is None and state["last_candle"] != candle_time:

        side = None

        if (
            trend5m == 1
            and st_now == 1
            and price > vwap
            and rsi < 55
            and adx > 20
        ):
            side = "long"

        if (
            trend5m == -1
            and st_now == -1
            and price < vwap
            and rsi > 45
            and adx > 20
        ):
            side = "short"

        if side:

            sl = price - STOP_LOSS[symbol] if side == "long" else price + STOP_LOSS[symbol]

            state["position"] = {
                "side": side,
                "entry": price,
                "stop": sl,
                "qty": DEFAULT_CONTRACTS[symbol],
                "entry_time": datetime.now(),
                "last_trail_price": price
            }

            state["last_candle"] = candle_time

            log(f"{symbol} {side} ENTRY @ {price}")

            send_telegram(
                f"📈 *{symbol} {side.upper()} ENTRY*\nPrice:{price}\nSL:{sl}"
            )

    # ================= TRAILING =================
    if pos:

        step = TRAIL_STEP[symbol]

        if pos["side"] == "long":

            moved = price - pos["last_trail_price"]

            if moved >= step:

                steps = int(moved // step)

                pos["stop"] += steps * step
                pos["last_trail_price"] += steps * step

            if price < pos["stop"]:
                exit_trade(symbol, price, pos, state)

        if pos["side"] == "short":

            moved = pos["last_trail_price"] - price

            if moved >= step:

                steps = int(moved // step)

                pos["stop"] -= steps * step
                pos["last_trail_price"] -= steps * step

            if price > pos["stop"]:
                exit_trade(symbol, price, pos, state)


# ================= EXIT =================
def exit_trade(symbol, price, pos, state):

    pnl = ((price - pos["entry"]) if pos["side"] == "long"
           else (pos["entry"] - price))

    pnl *= CONTRACT_SIZE[symbol] * pos["qty"]

    net = pnl - commission(price, pos["qty"], symbol)

    save_trade({
        "entry_time": pos["entry_time"],
        "exit_time": datetime.now(),
        "symbol": symbol,
        "side": pos["side"],
        "entry_price": pos["entry"],
        "exit_price": price,
        "qty": pos["qty"],
        "net_pnl": round(net, 6)
    })

    log(f"{symbol} EXIT PNL {net}")

    send_telegram(
        f"✅ *{symbol} EXIT*\nExit:{price}\nPnL:{round(net,6)}"
    )

    state["position"] = None


# ================= MAIN LOOP =================
def run():

    if not os.path.exists(TRADE_CSV):

        pd.DataFrame(
            columns=[
                "entry_time", "exit_time", "symbol",
                "side", "entry_price", "exit_price",
                "qty", "net_pnl"
            ]
        ).to_csv(TRADE_CSV, index=False)

    state = {s: {"position": None, "last_candle": None} for s in SYMBOLS}

    log("BOT STARTED")

    send_telegram("🚀 Supertrend Pro Bot Started")

    while True:

        try:

            for symbol in SYMBOLS:

                df = fetch_candles(symbol)

                if len(df) < 50:
                    continue

                price = fetch_price(symbol)

                process_symbol(symbol, df, price, state[symbol])

            time.sleep(5)

        except:

            log(traceback.format_exc())

            send_telegram("⚠️ Bot Error")

            time.sleep(5)


if __name__ == "__main__":
    run()