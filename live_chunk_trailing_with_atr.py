import os
import time
import requests
import pandas as pd
from datetime import datetime, timedelta
import pandas_ta as ta
from delta_rest_client import DeltaRestClient, OrderType
from dotenv import load_dotenv

load_dotenv()

# ================= SETTINGS ===============================
SYMBOLS = ["BTCUSD", "ETHUSD"]
ENTRY_MOVE = {"BTCUSD": 100, "ETHUSD": 10}
STOP_LOSS = {"BTCUSD": 300, "ETHUSD": 20}
TRAILING_CHUNK = {"BTCUSD": 50, "ETHUSD": 5}
DEFAULT_CONTRACTS = {"BTCUSD": 10, "ETHUSD": 10}
CONTRACT_SIZE = {"BTCUSD": 0.001, "ETHUSD": 0.01}
TAKER_FEE = 0.0005

# Product IDs mapping - FIXED with correct numeric IDs
PRODUCT_IDS = {"BTCUSD": 27, "ETHUSD": 3136}

# Telegram
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

# Delta Exchange API keys
api_key = os.getenv('DELTA_API_KEY')
api_secret = os.getenv('DELTA_API_SECRET')

client = DeltaRestClient(
    base_url='https://api.india.delta.exchange',
    api_key=api_key,
    api_secret=api_secret
)

# ---------------------------------------
# Helper functions (with alerts on failures)
# ---------------------------------------
def place_order_with_error_handling(client, **kwargs):
    try:
        resp = client.place_order(**kwargs)
        # If response is falsy: alert
        if not resp:
            log(f"⚠️ place_order returned empty response for payload: {kwargs}", alert=True)
            return None
        return resp
    except Exception as e:
        log(f"⚠️ Error placing order: {e} | payload: {kwargs}", alert=True)
        return None

def place_stop_order_with_error_handling(client, **kwargs):
    try:
        resp = client.place_stop_order(**kwargs)
        if not resp:
            log(f"⚠️ place_stop_order returned empty response for payload: {kwargs}", alert=True)
            return None
        return resp
    except Exception as e:
        log(f"⚠️ Error placing stop order: {e} | payload: {kwargs}", alert=True)
        return None

def cancel_order_with_error_handling(client, order_id, product_id):
    try:
        resp = client.cancel_order(order_id, product_id)
        if not resp:
            log(f"⚠️ cancel_order returned empty response for order {order_id} product {product_id}", alert=True)
        return resp
    except Exception as e:
        log(f"⚠️ Error cancelling order {order_id}: {e}", alert=True)
        return None

def edit_stop_order_with_error_handling(client, order_id, product_id, new_stop_price):
    try:
        payload = {"id": order_id, "product_id": product_id, "stop_price": str(new_stop_price)}
        resp = client.request("PUT", "/v2/orders/", payload, auth=True)
        if not resp:
            log(f"⚠️ edit_stop_order returned empty response for {order_id} payload: {payload}", alert=True)
        return resp
    except Exception as e:
        log(f"⚠️ Error editing order {order_id}: {e}", alert=True)
        return None

def get_history_orders_with_error_handling(client, product_id):
    try:
        query = {"product_id": product_id}
        response = client.order_history(query, page_size=10)
        if not response or 'result' not in response:
            log(f"⚠️ Unexpected order_history response for product {product_id}: {response}", alert=True)
            return []
        return response['result']
    except Exception as e:
        log(f"⚠️ Error getting order history: {e}", alert=True)
        return []

# ---------------------------------------
# Telegram Utility
# ---------------------------------------
def send_telegram_message(msg: str) -> bool:
    """Send alert message via Telegram. Returns True if sent successfully."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        # Make sure to surface this as an alert as well
        print("⚠️ Telegram not configured. Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID.")
        return False
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": msg}
        r = requests.post(url, json=payload, timeout=8)
        r.raise_for_status()
        return True
    except Exception as e:
        # fallback: print to console
        print(f"⚠️ Telegram Error: {e} | message was: {msg}")
        return False

def log(msg, alert=False):
    """Print and optionally send Telegram alert. Telegram will receive timestamped message."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    full_msg = f"[{timestamp}] {msg}"
    print(full_msg)
    if alert:
        send_telegram_message(f"📣 {full_msg}")

SAVE_DIR = os.path.join(os.getcwd(), "data", "live_chunk_trailing_with_atr")
os.makedirs(SAVE_DIR, exist_ok=True)
TRADE_CSV = os.path.join(SAVE_DIR, "live_trades.csv")

# ================= UTILITIES ==============================
def commission(price, contracts, symbol):
    notional = price * CONTRACT_SIZE.get(symbol, 1.0) * contracts
    return notional * TAKER_FEE

def save_trade_row(trade):
    df_trade = pd.DataFrame([trade])
    if not os.path.exists(TRADE_CSV):
        df_trade.to_csv(TRADE_CSV, index=False)
    else:
        df_trade.to_csv(TRADE_CSV, mode="a", header=False, index=False)

def save_processed_data(df, symbol):
    try:
        save_path = os.path.join(SAVE_DIR, f"{symbol}_processed.csv")
        df_save = pd.DataFrame({
            "time": df.index,
            "HA_open": df["HA_open"].values,
            "HA_high": df["HA_high"].values,
            "HA_low": df["HA_low"].values,
            "HA_close": df["HA_close"].values,
            "EMA": df["EMA"].values,
            "ATR": df["ATR"].values,
            "ATR_avg": df["ATR_avg"].values,
        })
        df_save.to_csv(save_path, index=False)
    except Exception as e:
        log(f"⚠️ Error saving processed data for {symbol}: {e}")

# ================= DATA FETCHING ==========================
def fetch_ticker_price(symbol):
    url = f"https://api.india.delta.exchange/v2/tickers/{symbol}"
    headers = {"Accept": "application/json"}
    try:
        r = requests.get(url, headers=headers, timeout=5)
        r.raise_for_status()
        data = r.json().get("result", {})
        price = float(data.get("mark_price", 0))
        return price if price > 0 else None
    except Exception as e:
        log(f"⚠️ Error fetching ticker price for {symbol}: {e}")
        return None

def fetch_candles(symbol, resolution="5m", days=1, tz='Asia/Kolkata'):
    headers = {'Accept': 'application/json'}
    start = int((datetime.now() - timedelta(days=days)).timestamp())
    params = {
        'resolution': resolution,
        'symbol': symbol,
        'start': str(start),
        'end': str(int(time.time())),
    }
    url = 'https://api.india.delta.exchange/v2/history/candles'
    try:
        r = requests.get(url, params=params, headers=headers, timeout=10)
        r.raise_for_status()
        data = r.json().get("result", [])
        if not data:
            log(f"No data for {symbol}")
            return None

        df = pd.DataFrame(data, columns=["time", "open", "high", "low", "close", "volume"])
        df.rename(columns=str.title, inplace=True)

        first_time = df["Time"].iloc[0]
        if first_time > 1e12:
            df["Time"] = pd.to_datetime(df["Time"], unit="ms", utc=True)
        else:
            df["Time"] = pd.to_datetime(df["Time"], unit="s", utc=True)

        df["Time"] = df["Time"].dt.tz_convert(tz)
        df["time"] = df["Time"]
        df.set_index("Time", inplace=True)
        df.sort_index(inplace=True)
        df = df[~df.index.duplicated(keep='last')]

        for col in ["Open", "High", "Low", "Close", "Volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")

        return df

    except Exception as e:
        log(f"⚠️ Error fetching {symbol}: {e}")
        return None

# ================= TREND STRATEGY =========================
def calculate_ema(df, period=10):
    ha_df = ta.ha(open_=df['Open'], high=df['High'], close=df['Close'], low=df['Low'])

    # ATR using raw OHLC
    atr = ta.atr(high=df["High"], low=df["Low"], close=df["Close"], length=14)
    ha_df["ATR"] = atr

    # ATR 14-period SMA
    ha_df["ATR_avg"] = ha_df["ATR"].rolling(14).mean()

    # EMA of HA close
    ha_df["EMA"] = ha_df["HA_close"].ewm(span=period, adjust=False).mean()

    return ha_df

def process_price_trend(symbol, price, positions, last_base_price, trailing_level, ema_value, last_close, df, product_id=None, ORDER_QTY=None):

    contracts = DEFAULT_CONTRACTS[symbol]
    contract_size = CONTRACT_SIZE[symbol]
    entry_move = ENTRY_MOVE[symbol]
    sl_points = STOP_LOSS[symbol]
    chunk = TRAILING_CHUNK[symbol]

    pos = positions.get(symbol)

    if ema_value is None:
        return

    # ===== ATR FILTER =====
    atr_val = df["ATR"].iloc[-1]
    atr_avg = df["ATR_avg"].iloc[-1]

    if pd.isna(atr_val) or pd.isna(atr_avg):
        return

    atr_condition = atr_val > atr_avg

    if last_base_price[symbol] is None:
        last_base_price[symbol] = price
        trailing_level[symbol] = None
        return

    # ================= ENTRY CONDITIONS WITH ATR ===================
    if pos is None:

        # ---- LONG ENTRY ----
        if atr_condition and price >= last_base_price[symbol] + entry_move and last_close > ema_value:
            entry_price = last_base_price[symbol] + entry_move

            # 🔵 Place LIVE MARKET BUY ORDER for LONG
            try:
                buy_order = place_order_with_error_handling(
                    client,
                    product_id=product_id,
                    order_type=OrderType.MARKET,
                    side='buy',
                    size=contracts  # Simplified - removed redundant ORDER_QTY logic
                )
                if buy_order:
                    send_telegram_message(
                        f"🟢 LONG BUY EXECUTED\n"
                        f"Symbol: {symbol}\n"
                        f"Order Size: {contracts}\n"
                        f"Strategy Entry Price (target): {entry_price}\n"
                        f"Market Price: {price}"
                    )
                else:
                    send_telegram_message(
                        f"⚠️ LONG BUY ATTEMPT FAILED\nSymbol: {symbol}\nOrder Size: {contracts}\nTarget Entry: {entry_price}\nMarket Price: {price}"
                    )
            except Exception as e:
                log(f"⚠️ Exception placing long buy: {e}", alert=True)

            positions[symbol] = {
                "side": "long",
                "entry": entry_price,
                "stop": entry_price - sl_points,
                "contracts": contracts,
                "contract_size": contract_size,
                "entry_time": datetime.now(),
                "trailing_level": entry_price
            }
            trailing_level[symbol] = entry_price
            log(f"{symbol} | LONG ENTRY | ATR>ATR_avg & Close>EMA | Entry: {entry_price}")
            return

        # ---- SHORT ENTRY ----
        elif atr_condition and price <= last_base_price[symbol] - entry_move and last_close < ema_value:
            entry_price = last_base_price[symbol] - entry_move

            # 🔴 Place LIVE MARKET SELL ORDER for SHORT
            try:
                sell_order = place_order_with_error_handling(
                    client,
                    product_id=product_id,
                    order_type=OrderType.MARKET,
                    side='sell',
                    size=contracts  # Simplified - removed redundant ORDER_QTY logic
                )
                if sell_order:
                    send_telegram_message(
                        f"🔻 SHORT SELL EXECUTED\n"
                        f"Symbol: {symbol}\n"
                        f"Order Size: {contracts}\n"
                        f"Strategy Entry Price (target): {entry_price}\n"
                        f"Market Price: {price}"
                    )
                else:
                    send_telegram_message(
                        f"⚠️ SHORT SELL ATTEMPT FAILED\nSymbol: {symbol}\nOrder Size: {contracts}\nTarget Entry: {entry_price}\nMarket Price: {price}"
                    )
            except Exception as e:
                log(f"⚠️ Exception placing short sell: {e}", alert=True)

            positions[symbol] = {
                "side": "short",
                "entry": entry_price,
                "stop": entry_price + sl_points,
                "contracts": contracts,
                "contract_size": contract_size,
                "entry_time": datetime.now(),
                "trailing_level": entry_price
            }
            trailing_level[symbol] = entry_price
            log(f"{symbol} | SHORT ENTRY | ATR>ATR_avg & Close<EMA | Entry: {entry_price}")
            return

    # ================= POSITION MANAGEMENT ===================
    if pos is not None:
        # ---------- LONG POSITION MANAGEMENT & EXIT ----------
        if pos["side"] == "long":
            chunks_up = int((price - pos["trailing_level"]) / chunk) if pos["trailing_level"] is not None else 0
            if chunks_up > 0:
                pos["trailing_level"] += chunks_up * chunk
                pos["stop"] = max(pos["stop"], pos["trailing_level"] - sl_points)

            if price <= pos["stop"]:
                pnl = (price - pos["entry"]) * contract_size * contracts
                exit_fee = commission(price, contracts, symbol)
                net_pnl = pnl - exit_fee

                save_trade_row({
                    "symbol": symbol,
                    "side": "long",
                    "entry_time": pos["entry_time"],
                    "exit_time": datetime.now(),
                    "qty": contracts,
                    "entry": pos["entry"],
                    "exit": price,
                    "gross_pnl": round(pnl, 6),
                    "commission": round(exit_fee, 6),
                    "net_pnl": round(net_pnl, 6)
                })

                log(f"{symbol} | LONG EXIT | Exit: {price} | Net PnL: {net_pnl}")

                # 🔵 Place MARKET SELL to exit LONG
                try:
                    exit_order = place_order_with_error_handling(
                        client,
                        product_id=product_id,
                        order_type=OrderType.MARKET,
                        side='sell',
                        size=pos["contracts"]
                    )
                    if exit_order:
                        send_telegram_message(
                            f"📤 LONG EXIT EXECUTED\n"
                            f"Symbol: {symbol}\n"
                            f"Entry: {pos['entry']}\n"
                            f"Exit: {price}\n"
                            f"Net PnL: {net_pnl}"
                        )
                    else:
                        send_telegram_message(
                            f"⚠️ LONG EXIT ORDER FAILED\nSymbol: {symbol}\nExit Price: {price}\nNet PnL (calc): {net_pnl}"
                        )
                except Exception as e:
                    log(f"⚠️ Exception placing long exit sell order: {e}", alert=True)

                positions[symbol] = None
                last_base_price[symbol] = price
                trailing_level[symbol] = None

        # ---------- SHORT POSITION MANAGEMENT & EXIT ----------
        elif pos["side"] == "short":
            chunks_down = int((pos["trailing_level"] - price) / chunk) if pos["trailing_level"] is not None else 0
            if chunks_down > 0:
                pos["trailing_level"] -= chunks_down * chunk
                pos["stop"] = min(pos["stop"], pos["trailing_level"] + sl_points)

            if price >= pos["stop"]:
                pnl = (pos["entry"] - price) * contract_size * contracts
                exit_fee = commission(price, contracts, symbol)
                net_pnl = pnl - exit_fee

                save_trade_row({
                    "symbol": symbol,
                    "side": "short",
                    "entry_time": pos["entry_time"],
                    "exit_time": datetime.now(),
                    "qty": contracts,
                    "entry": pos["entry"],
                    "exit": price,
                    "gross_pnl": round(pnl, 6),
                    "commission": round(exit_fee, 6),
                    "net_pnl": round(net_pnl, 6)
                })

                log(f"{symbol} | SHORT EXIT | Exit: {price} | Net PnL: {net_pnl}")

                # 🔴 Place MARKET BUY to exit SHORT
                try:
                    exit_order = place_order_with_error_handling(
                        client,
                        product_id=product_id,
                        order_type=OrderType.MARKET,
                        side='buy',
                        size=pos["contracts"]
                    )
                    if exit_order:
                        send_telegram_message(
                            f"📤 SHORT EXIT EXECUTED\n"
                            f"Symbol: {symbol}\n"
                            f"Entry: {pos['entry']}\n"
                            f"Exit: {price}\n"
                            f"Net PnL: {net_pnl}"
                        )
                    else:
                        send_telegram_message(
                            f"⚠️ SHORT EXIT ORDER FAILED\nSymbol: {symbol}\nExit Price: {price}\nNet PnL (calc): {net_pnl}"
                        )
                except Exception as e:
                    log(f"⚠️ Exception placing short exit buy order: {e}", alert=True)

                positions[symbol] = None
                last_base_price[symbol] = price
                trailing_level[symbol] = None

# ================= MAIN LOOP ==============================
def run_live():
    positions = {s: None for s in SYMBOLS}
    last_base_price = {s: None for s in SYMBOLS}
    trailing_level = {s: None for s in SYMBOLS}

    log("🚀 Starting live Chunk-based Trailing Stop strategy with ATR + EMA + 5m Close confirmation")

    while True:
        try:
            for symbol in SYMBOLS:
                df = fetch_candles(symbol, resolution="5m", days=1)
                if df is None or len(df) < 20:
                    continue

                ha_df = calculate_ema(df, period=10)
                ema_value = ha_df["EMA"].iloc[-1]
                last_close = df["Close"].iloc[-1]

                save_processed_data(ha_df, symbol)

                price = fetch_ticker_price(symbol)
                if price is None:
                    continue

                # Get correct product_id for the symbol
                product_id = PRODUCT_IDS.get(symbol, symbol)

                process_price_trend(
                    symbol,
                    price,
                    positions,
                    last_base_price,
                    trailing_level,
                    ema_value,
                    last_close,
                    ha_df,
                    product_id=product_id,
                    ORDER_QTY=None  # Removed redundant parameter
                )


            time.sleep(60)

        except Exception as e:
            log(f"⚠️ Main loop error: {e}")
            time.sleep(10)

# ================= MAIN ENTRY ============================
if __name__ == "__main__":
    run_live()
