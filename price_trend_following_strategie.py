import os
import time
import requests
import pandas as pd
from datetime import datetime, timedelta
import pandas_ta as ta
from scipy.signal import argrelextrema
import numpy as np
from delta_rest_client import DeltaRestClient, OrderType
from dotenv import load_dotenv

load_dotenv()

# =================== SETTINGS =====================
SYMBOLS = ["BTCUSD", "ETHUSD"]
PRODUCT_IDS = {"BTCUSD": 27, "ETHUSD": 3136}
DEFAULT_CONTRACTS = {"BTCUSD": 30, "ETHUSD": 30}
CONTRACT_SIZE = {"BTCUSD": 0.001, "ETHUSD": 0.01}
TAKER_FEE = 0.0005

SAVE_DIR = os.path.join(os.getcwd(), "data", "price_trend_following_strategie")
os.makedirs(SAVE_DIR, exist_ok=True)
TRADE_CSV = os.path.join(SAVE_DIR, "live_trades.csv")

# =================== TELEGRAM =====================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

def send_telegram(msg: str):
    """Send Telegram notification (errors, order events only)."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ Telegram not configured.")
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg}, timeout=5)
    except Exception:
        pass


# =================== EXCHANGE CLIENT =====================
client = DeltaRestClient(
    base_url="https://api.india.delta.exchange",
    api_key=os.getenv("DELTA_API_KEY"),
    api_secret=os.getenv("DELTA_API_SECRET")
)


# =================== ORDER WRAPPER =====================
def place_order(product_id, side, size):
    try:
        resp = client.place_order(
            product_id=product_id,
            order_type=OrderType.MARKET,
            side=side,
            size=size
        )
        if not resp:
            send_telegram(f"❌ ORDER FAILED\nSide: {side}\nProduct: {product_id}\nSize: {size}")
            return None

        send_telegram(f"✅ ORDER EXECUTED\nSide: {side}\nProduct: {product_id}\nSize: {size}")
        return resp

    except Exception as e:
        send_telegram(f"❌ ORDER ERROR\nSide: {side}\nProduct: {product_id}\n{str(e)}")
        return None


# =================== MISC UTILS =====================
def log(msg: str):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}")


def save_trade_row(row):
    df_trade = pd.DataFrame([row])
    if not os.path.exists(TRADE_CSV):
        df_trade.to_csv(TRADE_CSV, index=False)
    else:
        df_trade.to_csv(TRADE_CSV, mode="a", header=False, index=False)


def commission(price, contracts, symbol):
    notional = price * CONTRACT_SIZE[symbol] * contracts
    return notional * TAKER_FEE


# =================== DATA FETCHING =====================
def fetch_ticker_price(symbol):
    try:
        url = f"https://api.india.delta.exchange/v2/tickers/{symbol}"
        r = requests.get(url, timeout=5)
        r.raise_for_status()

        result = r.json().get("result", {})
        price = float(result.get("mark_price", 0))

        return price if price > 0 else None

    except Exception as e:
        send_telegram(f"⚠️ PRICE FETCH ERROR ({symbol})\n{str(e)}")
        return None


def fetch_candles(symbol, resolution="15m", days=1):
    try:
        start = int((datetime.now() - timedelta(days=days)).timestamp())
        params = {
            "resolution": resolution,
            "symbol": symbol,
            "start": str(start),
            "end": str(int(time.time()))
        }
        url = "https://api.india.delta.exchange/v2/history/candles"

        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()

        data = r.json().get("result", [])
        if not data:
            return None

        df = pd.DataFrame(data, columns=["time", "open", "high", "low", "close", "volume"])
        df.rename(columns=str.title, inplace=True)

        first_time = df["Time"].iloc[0]
        unit = "ms" if first_time > 1e12 else "s"
        df["Time"] = pd.to_datetime(df["Time"], unit=unit, utc=True).dt.tz_convert("Asia/Kolkata")

        df.set_index("Time", inplace=True)
        df.sort_index(inplace=True)
        df = df[~df.index.duplicated(keep="last")]

        return df

    except Exception as e:
        send_telegram(f"⚠️ CANDLE FETCH ERROR ({symbol})\n{str(e)}")
        return None


# =================== TRENDLINE CALCULATION =====================
def calculate_trendline(df):
    ha = ta.ha(
        open_=df["Open"],
        high=df["High"],
        low=df["Low"],
        close=df["Close"]
    )
    ha = ha.reset_index(drop=True)

    max_idx = argrelextrema(ha["HA_high"].values, np.greater_equal, order=21)[0]
    min_idx = argrelextrema(ha["HA_low"].values, np.less_equal, order=21)[0]

    ha["max_high"] = np.nan
    ha["max_low"] = np.nan
    ha.loc[max_idx, "max_high"] = ha.loc[max_idx, "HA_high"]
    ha.loc[min_idx, "max_low"] = ha.loc[min_idx, "HA_low"]

    ha.loc[:, "max_high"] = ha["max_high"].ffill()
    ha.loc[:, "max_low"]  = ha["max_low"].ffill()

    ha["Trendline"] = np.nan
    trendline = ha["max_high"].iloc[0]

    ha.loc[0, "Trendline"] = trendline

    for i in range(1, len(ha)):
        if ha.loc[i, "max_high"] == ha.loc[i, "HA_high"]:
            trendline = ha.loc[i-1, "HA_low"]
        elif ha.loc[i, "max_low"] == ha.loc[i, "HA_low"]:
            trendline = ha.loc[i-1, "HA_high"]

        ha.loc[i, "Trendline"] = trendline

    ha = ha.drop(columns=["max_high", "max_low"])
    # ADX on actual high/low/close (window=14)
    adx = ta.adx(
        high=ha["HA_high"],
        low=ha["HA_low"],
        close=ha["HA_close"],
        window=14
    )
    # ta.adx returns columns like "ADX_14", ensure existence
    if "ADX_14" in adx.columns:
        ha["ADX"] = adx["ADX_14"]
    else:
        ha["ADX"] = adx.iloc[:, 0]  # fallback if naming differs

    ha["ADX_avg"] = ha["ADX"].rolling(7, min_periods=1).mean()
    return ha


# =================== TRADING LOGIC =====================
def process_price_trend(symbol, price, positions, prev_close, last_close, ADX, ADX_Avg, df):
    raw_trendline = df["Trendline"].iloc[-1]
    pos = positions.get(symbol)

    size = DEFAULT_CONTRACTS[symbol]
    contract_size = CONTRACT_SIZE[symbol]
    pid = PRODUCT_IDS[symbol]

    # ENTRY: LONG
    if pos is None and last_close > raw_trendline and last_close > prev_close and ADX > ADX_Avg and datetime.now().minute % 15 == 0:
        resp = place_order(pid, "buy", size)
        if resp:
            send_telegram(f"🟢 LONG ENTRY {symbol}\nPrice: {price}")

        positions[symbol] = {
            "side": "long",
            "entry": price,
            "stop": raw_trendline,
            "contracts": size,
            "entry_time": datetime.now()
        }
        return

    # ENTRY: SHORT
    if pos is None and last_close < raw_trendline and last_close < prev_close and ADX > ADX_Avg and datetime.now().minute % 15 == 0:
        resp = place_order(pid, "sell", size)
        if resp:
            send_telegram(f"🔻 SHORT ENTRY {symbol}\nPrice: {price}")

        positions[symbol] = {
            "side": "short",
            "entry": price,
            "stop": raw_trendline,
            "contracts": size,
            "entry_time": datetime.now()
        }
        return

    # EXIT: LONG
    if pos and pos["side"] == "long" and last_close < raw_trendline:
        pnl = (price - pos["entry"]) * contract_size * pos["contracts"]
        fee = commission(price, pos["contracts"], symbol)
        net = pnl - fee

        place_order(pid, "sell", size)
        send_telegram(f"📤 LONG EXIT {symbol}\nNet PnL: {net:.4f}")

        save_trade_row({
            "symbol": symbol,
            "side": "long",
            "entry_time": pos["entry_time"],
            "exit_time": datetime.now(),
            "qty": pos["contracts"],
            "entry": pos["entry"],
            "exit": price,
            "gross_pnl": pnl,
            "commission": fee,
            "net_pnl": net
        })

        positions[symbol] = None

    # EXIT: SHORT
    if pos and pos["side"] == "short" and last_close > raw_trendline:
        pnl = (pos["entry"] - price) * contract_size * pos["contracts"]
        fee = commission(price, pos["contracts"], symbol)
        net = pnl - fee

        place_order(pid, "buy", size)
        send_telegram(f"📤 SHORT EXIT {symbol}\nNet PnL: {net:.4f}")

        save_trade_row({
            "symbol": symbol,
            "side": "short",
            "entry_time": pos["entry_time"],
            "exit_time": datetime.now(),
            "qty": pos["contracts"],
            "entry": pos["entry"],
            "exit": price,
            "gross_pnl": pnl,
            "commission": fee,
            "net_pnl": net
        })

        positions[symbol] = None


# =================== MAIN LOOP =====================
def run_live():
    positions = {s: None for s in SYMBOLS}

    log("🚀 Starting Clean Trendline Live Strategy")

    while True:
        try:
            for symbol in SYMBOLS:
                df = fetch_candles(symbol)
                if df is None or len(df) < 50:
                    continue

                ha_df = calculate_trendline(df)
                if len(ha_df) < 3:
                    continue

                last_close = ha_df["HA_close"].iloc[-1]
                ADX = ha_df["ADX"].iloc[-1]
                ADX_Avg = ha_df["ADX_avg"].iloc[-1]
                prev_close = ha_df["HA_open"].iloc[-2]

                price = fetch_ticker_price(symbol)
                if price is None:
                    continue

                process_price_trend(symbol, price, positions, prev_close, last_close, ADX, ADX_Avg, ha_df)

            time.sleep(20)

        except Exception as e:
            send_telegram(f"⚠️ MAIN LOOP ERROR\n{str(e)}")
            time.sleep(5)


# =================== ENTRY POINT =====================
if __name__ == "__main__":
    run_live()
