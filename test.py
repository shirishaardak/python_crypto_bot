#!/usr/bin/env python3
"""
CoinDCX futures version of your Delta strategy.
Features:
 - Uses CoinDCX public candlesticks endpoint for futures (pair format like B-BTC_USDT)
 - Uses CoinDCX Futures private endpoints for creating/cancelling orders
 - Heikin-Ashi + EMA single-bar entry logic copied from original
 - Telegram alerts preserved
Caveats: test carefully on small size / paper/demo. Stop-order behaviour on CoinDCX uses stop_limit; adjust as needed.
"""

import os
import time as t
import requests
import pandas as pd
import pandas_ta as ta
import hmac, hashlib, json
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------
# SETTINGS (adjust as required)
# ---------------------------------------
USER_SYMBOLS = ["BTCUSD", "ETHUSD"]   # same user-facing keys (we map to CoinDCX pairs)
ORDER_QTY = int(os.getenv("ORDER_QTY", "30"))

# Telegram
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

# CoinDCX API keys
COINDCX_KEY = os.getenv('COINDCX_API_KEY')
COINDCX_SECRET = os.getenv('COINDCX_API_SECRET')

# Base URLs
PUBLIC_BASE = "https://public.coindcx.com"
API_BASE = "https://api.coindcx.com"

# ---------------------------------------
# Utilities: Telegram + logging
# ---------------------------------------
def send_telegram_message(msg: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ Telegram not configured. Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID.")
        return False
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": msg}
        r = requests.post(url, json=payload, timeout=8)
        r.raise_for_status()
        return True
    except Exception as e:
        print(f"⚠️ Telegram Error: {e} | message was: {msg}")
        return False

def log(msg, alert=False):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    full_msg = f"[{timestamp}] {msg}"
    print(full_msg)
    if alert:
        send_telegram_message(f"📣 {full_msg}")

# ---------------------------------------
# CoinDCX auth helper (Futures API signing)
# (docs show JSON body -> HMAC-SHA256(hex) using secret)
# ---------------------------------------
def sign_payload(body: dict):
    """
    CoinDCX Futures signing (from CoinDCX PDF examples).
    JSON must be compact: separators=(',',':')
    """
    if COINDCX_SECRET is None:
        raise ValueError("Missing COINDCX_API_SECRET env var")
    json_body = json.dumps(body, separators=(',', ':'))
    secret_bytes = bytes(COINDCX_SECRET, encoding='utf-8')
    signature = hmac.new(secret_bytes, json_body.encode(), hashlib.sha256).hexdigest()
    return json_body, signature

def auth_post(path: str, body: dict, timeout=12):
    url = API_BASE + path
    json_body, signature = sign_payload(body)
    headers = {
        "Content-Type": "application/json",
        "X-AUTH-APIKEY": COINDCX_KEY,
        "X-AUTH-SIGNATURE": signature
    }
    try:
        r = requests.post(url, data=json_body, headers=headers, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log(f"⚠️ Auth POST error {path}: {e} | body: {body}", alert=True)
        return None

# ---------------------------------------
# Map user symbols to CoinDCX futures instrument pairs
# We'll try to auto-detect active instrument pairs; fallback to common mapping.
# CoinDCX instruments use format like 'B-BTC_USDT' for Binance-based BTC/USDT perp.
# ---------------------------------------
def get_active_futures_instruments():
    try:
        url = f"{API_BASE}/exchange/v1/derivatives/futures/data/active_instruments"
        r = requests.get(url, timeout=8)
        r.raise_for_status()
        return r.json()  # list of instrument strings like "B-BTC_USDT"
    except Exception as e:
        log(f"Error fetching active futures instruments: {e}", alert=True)
        return []

def find_pair_for_symbol(user_symbol: str, instruments: list):
    """
    user_symbol: e.g. 'BTCUSD' -> try to match 'B-BTC_USDT' or similar.
    heuristic: look for underlying token name (BTC, ETH) in instrument names.
    """
    token = None
    if user_symbol.upper().startswith("BTC"):
        token = "BTC"
    elif user_symbol.upper().startswith("ETH"):
        token = "ETH"
    else:
        token = user_symbol.upper().replace("USD", "")  # fallback

    # prefer *_USDT perpetual
    candidates = [p for p in instruments if f"_{token}_USDT" in p or f"-{token}_" in p]
    # prefer those starting with 'B-' (third party prefix per docs)
    if candidates:
        # pick first candidate that contains token and USDT
        for p in candidates:
            if p.endswith("_USDT"):
                return p
        return candidates[0]
    # fallback mapping (explicit)
    fallback = {
        "BTCUSD": "B-BTC_USDT",
        "ETHUSD": "B-ETH_USDT"
    }
    return fallback.get(user_symbol, None)

# ---------------------------------------
# Candles (CoinDCX public candlesticks for futures)
# endpoint: GET https://public.coindcx.com/market_data/candlesticks
# params: pair, from, to, resolution, pcode=f
# note: times in seconds (pdf shows examples)
# ---------------------------------------
def fetch_coindcx_candles(pair: str, resolution='5', days=7, tz='Asia/Kolkata'):
    """
    resolution: '1', '5', '60', '1D' based on docs. Using string '5' for 5-minute.
    returns pandas DataFrame with index tz-aware
    """
    try:
        to_ts = int(time_millis() / 1000)
        from_ts = int((datetime.now() - timedelta(days=days)).timestamp())
        url = f"{PUBLIC_BASE}/market_data/candlesticks"
        params = {
            "pair": pair,
            "from": from_ts,
            "to": to_ts,
            "resolution": resolution,
            "pcode": "f"
        }
        r = requests.get(url, params=params, timeout=12)
        r.raise_for_status()
        data = r.json()
        # example response: {"s":"ok","data":[{"open":..., "high":..., "low":..., "close":..., "volume":..., "time":1704153600000}, ...]}
        if not data or 'data' not in data or not data['data']:
            log(f"No candle data returned for {pair}", alert=True)
            return None
        df = pd.DataFrame(data['data'])
        # CoinDCX returns milliseconds timestamps in "time"
        df['time'] = pd.to_datetime(df['time'], unit='ms', utc=True).dt.tz_convert(tz)
        df = df.set_index('time').sort_index()
        # ensure numeric
        for col in ['open', 'high', 'low', 'close', 'volume']:
            df[col] = pd.to_numeric(df[col], errors='coerce')
        return df
    except Exception as e:
        log(f"Error fetching candles for {pair}: {e}", alert=True)
        return None

def time_millis():
    return int(round(t.time() * 1000))

# ---------------------------------------
# Futures order helpers (create market, create stop_limit, cancel, list positions)
# Based on CoinDCX Futures PDF:
# - create order: POST /exchange/v1/derivatives/futures/orders/create  (body: {"timestamp":ts,"order":{...}})
# - cancel order: POST /exchange/v1/derivatives/futures/orders/cancel  (body: {"timestamp":ts,"id": order_id})
# See docs for more fields.
# ---------------------------------------
def create_market_order(pair, side, qty, leverage=10, notification="no_notification"):
    ts = time_millis()
    body = {
        "timestamp": ts,
        "order": {
            "side": side,  # "buy" or "sell"
            "pair": pair,
            "order_type": "market_order",
            "total_quantity": float(qty),
            "leverage": leverage,
            "notification": notification,
        }
    }
    return auth_post("/exchange/v1/derivatives/futures/orders/create", body)

def create_stop_limit_order(pair, side, qty, stop_price, limit_price=None, leverage=10, notification="no_notification", tif="good_till_cancel"):
    """
    Create a stop-limit order to act as stop loss.
    stop_price: trigger
    limit_price: price to place the limit (if None, we use stop_price as limit)
    """
    if limit_price is None:
        limit_price = stop_price
    ts = time_millis()
    body = {
        "timestamp": ts,
        "order": {
            "side": side,
            "pair": pair,
            "order_type": "stop_limit",
            "stop_price": str(stop_price),
            "price": str(limit_price),
            "total_quantity": float(qty),
            "leverage": leverage,
            "notification": notification,
            "time_in_force": tif
        }
    }
    return auth_post("/exchange/v1/derivatives/futures/orders/create", body)

def cancel_order(order_id):
    ts = time_millis()
    body = {"timestamp": ts, "id": order_id}
    return auth_post("/exchange/v1/derivatives/futures/orders/cancel", body)

def list_positions(page=1, size=20):
    ts = time_millis()
    body = {"timestamp": ts, "page": page, "size": size}
    return auth_post("/exchange/v1/derivatives/futures/positions", body)

def list_orders_for_pair(pair, page=1, size=50):
    ts = time_millis()
    body = {"timestamp": ts, "pair": pair, "page": page, "size": size}
    # There are endpoints for order history / open orders; try positions or orders endpoints depending on responses
    return auth_post("/exchange/v1/derivatives/futures/orders/list", body)  # best-effort; some installations may use different path

# ---------------------------------------
# Process symbol: replicate Heikin-Ashi + EMA logic from original script
# ---------------------------------------
def process_symbol_coin(symbol_pair_map, renko_param, pair, user_symbol, ha_save_dir="./data/coindcx_futures_strategy"):
    df = fetch_coindcx_candles(pair, resolution='5', days=7)
    if df is None or df.empty:
        return renko_param

    df = df.sort_index()
    # Heikin Ashi (pandas_ta ha)
    df_ha = ta.ha(open_=df['open'], high=df['high'], close=df['close'], low=df['low'])
    df_ha['EMA_21'] = ta.ema(df_ha['HA_close'], length=5)

    # offsets (use same logic: big offset for BTC)
    offset = 300 if user_symbol == "BTCUSD" else 30
    df_ha['EMA_21_UP'] = df_ha['EMA_21'] + offset
    df_ha['EMA_21_DN'] = df_ha['EMA_21'] - offset

    df_ha['single'] = 0
    df_ha.loc[(df_ha['HA_close'] > df_ha['EMA_21_UP']) & (df_ha['HA_close'] > df_ha['HA_close'].shift(1)) & (df_ha['HA_close'] > df_ha['HA_open']), 'single'] = 1
    df_ha.loc[(df_ha['HA_close'] < df_ha['EMA_21_DN']) & (df_ha['HA_close'] < df_ha['HA_close'].shift(1)) & (df_ha['HA_close'] < df_ha['HA_open']), 'single'] = -1

    os.makedirs(ha_save_dir, exist_ok=True)
    try:
        df_ha.to_csv(f"{ha_save_dir}/supertrend_live_{user_symbol}.csv")
    except Exception as e:
        log(f"Error saving CSV for {user_symbol}: {e}", alert=True)

    if len(df_ha) < 2:
        log(f"Not enough data for {user_symbol}", alert=True)
        return renko_param

    last_row = df_ha.iloc[-1]
    renko_param[user_symbol].update({
        'Date': last_row.name,
        'close': last_row['HA_close'],
        'single': int(last_row['single']),
        'EMA_21_UP': float(last_row['EMA_21_UP']),
        'EMA_21_DN': float(last_row['EMA_21_DN']),
        'EMA_21': float(last_row['EMA_21']),
        'pair': pair
    })

    return renko_param

# ---------------------------------------
# Main: build instrument mapping (user symbol -> coindcx pair)
# ---------------------------------------
def build_symbol_pair_map(user_symbols):
    instruments = get_active_futures_instruments()
    if not instruments:
        log("Warning: could not fetch active instruments; using fallback mappings", alert=True)
    symbol_pair_map = {}
    for us in user_symbols:
        p = find_pair_for_symbol(us, instruments)
        if not p:
            log(f"Could not find instrument pair for {us}", alert=True)
            raise ValueError(f"Missing instrument mapping for {us}")
        symbol_pair_map[us] = p
    return symbol_pair_map

# ---------------------------------------
# Initialize renko_param (state container) — similar to your original
# ---------------------------------------
def init_state_map(symbols):
    return {
        s: {
            'Date': '',
            'close': '',
            'option': 0,
            'single': 0,
            'EMA_21': None,
            'EMA_21_UP': None,
            'EMA_21_DN': None,
            'stop_order_id': None,
            'main_order_id': None,
            'entry_price': None,
            'exit_price': None,
            'pnl': 0.0,
            'pair': None
        } for s in symbols
    }

# ---------------------------------------
# Start main loop (keeps much of your original logic)
# Note: for editing stop order we cancel the existing stop order and recreate at new stop price.
# ---------------------------------------
def main():
    if COINDCX_KEY is None or COINDCX_SECRET is None:
        raise ValueError("Please set COINDCX_API_KEY and COINDCX_API_SECRET in env")

    symbol_pair_map = build_symbol_pair_map(USER_SYMBOLS)
    print("Loaded instrument pairs:", symbol_pair_map)

    renko_state = init_state_map(USER_SYMBOLS)

    print("Starting CoinDCX futures strategy for:", ", ".join(USER_SYMBOLS))
    try:
        while True:
            now = datetime.now()
            # run every 5-min aligned, at second 10 (same as original)
            if now.second == 10 and now.minute % 5 == 0:
                log(f"\nRunning cycle...", alert=False)
                # fetch indicators for each symbol
                for user_sym, pair in symbol_pair_map.items():
                    renko_state = process_symbol_coin(symbol_pair_map, renko_state, pair, user_sym)

                # Trading logic (entry & management)
                for user_sym, pair in symbol_pair_map.items():
                    state = renko_state[user_sym]
                    price = state['close']
                    EMA_21 = state['EMA_21']
                    single = state['single']
                    option = state['option']  # 0=no position, 1=long, 2=short

                    # BUY ENTRY
                    if single == 1 and option == 0:
                        log(f"🟢 BUY signal for {user_sym} (pair {pair}) at {price}", alert=True)
                        resp = create_market_order(pair=pair, side="buy", qty=ORDER_QTY)
                        if resp:
                            # response example is a list with order info
                            # pick first id if available
                            try:
                                order_id = resp[0].get("id") if isinstance(resp, list) else resp.get("id")
                                state['main_order_id'] = order_id
                                # if immediate filled, update option. If not, keep watching
                                order_status = resp[0].get("status") if isinstance(resp, list) else resp.get("status")
                                if order_status in ("filled", "closed", "initial"):
                                    # treat as filled (best-effort)
                                    state.update({'option': 1, 'entry_price': price})
                                    log(f"✅ BUY executed on {user_sym} — Entry: {price} | OrderID: {order_id}", alert=True)
                                    # place stop_limit as stop loss: side sell
                                    stop_resp = create_stop_limit_order(pair=pair, side="sell", qty=ORDER_QTY, stop_price=EMA_21, limit_price=EMA_21)
                                    if stop_resp:
                                        stop_id = stop_resp[0].get("id") if isinstance(stop_resp, list) else stop_resp.get("id")
                                        state['stop_order_id'] = stop_id
                                        log(f"🔒 Stop Loss placed for BUY {user_sym} at {EMA_21} | StopOrderID: {stop_id}", alert=True)
                                    else:
                                        log(f"⚠️ Failed to place stop loss for BUY {user_sym} after entry.", alert=True)
                                else:
                                    log(f"⚠️ BUY placed for {user_sym} but not filled. State: {order_status} | Resp: {resp}", alert=True)
                            except Exception as e:
                                log(f"⚠️ Unexpected create market order response: {e} | resp: {resp}", alert=True)
                        else:
                            log(f"⚠️ BUY order failed to be placed for {user_sym}", alert=True)

                    # BUY MANAGEMENT
                    elif option == 1:
                        stop_id = state['stop_order_id']
                        if stop_id:
                            # edit stop: CoinDCX doesn't show edit API in PDF — we'll cancel + recreate
                            # Cancel existing stop and create a new stop at EMA_21
                            cancel_order(stop_id)
                            new_stop = create_stop_limit_order(pair=pair, side="sell", qty=ORDER_QTY, stop_price=EMA_21, limit_price=EMA_21)
                            if new_stop:
                                new_stop_id = new_stop[0].get("id") if isinstance(new_stop, list) else new_stop.get("id")
                                state['stop_order_id'] = new_stop_id
                                log(f"🔒 Updated Stop Loss for BUY {user_sym} to {EMA_21} | new StopOrderID: {new_stop_id}", alert=True)
                            # Check if position stopped (list positions endpoint or orders history)
                            # We'll poll positions: if active_pos==0 after stop, it means exited — but this depends on exchange response.
                            positions = list_positions()
                            # naive check: when stop triggers the stop order will be closed and position will be zero
                            try:
                                if isinstance(positions, list):
                                    for pos in positions:
                                        if pos.get("pair") == pair:
                                            if float(pos.get("active_pos", 0)) == 0 and state['entry_price'] is not None:
                                                exit_price = EMA_21
                                                entry_price = state['entry_price']
                                                pnl = (exit_price - entry_price) * ORDER_QTY
                                                state.update({'option': 0, 'stop_order_id': None, 'main_order_id': None, 'exit_price': exit_price, 'pnl': pnl})
                                                log(f"🔴 BUY Stop Loss Triggered on {user_sym} — Exit: {exit_price} | PnL: {pnl:.2f}", alert=True)
                            except Exception:
                                pass

                    # SELL ENTRY
                    if single == -1 and option == 0:
                        log(f"🔻 SELL signal for {user_sym} (pair {pair}) at {price}", alert=True)
                        resp = create_market_order(pair=pair, side="sell", qty=ORDER_QTY)
                        if resp:
                            try:
                                order_id = resp[0].get("id") if isinstance(resp, list) else resp.get("id")
                                order_status = resp[0].get("status") if isinstance(resp, list) else resp.get("status")
                                state['main_order_id'] = order_id
                                if order_status in ("filled", "closed", "initial"):
                                    state.update({'option': 2, 'entry_price': price})
                                    log(f"✅ SELL executed on {user_sym} — Entry: {price} | OrderID: {order_id}", alert=True)
                                    stop_resp = create_stop_limit_order(pair=pair, side="buy", qty=ORDER_QTY, stop_price=EMA_21, limit_price=EMA_21)
                                    if stop_resp:
                                        stop_id = stop_resp[0].get("id") if isinstance(stop_resp, list) else stop_resp.get("id")
                                        state['stop_order_id'] = stop_id
                                        log(f"🔒 Stop Loss placed for SELL {user_sym} at {EMA_21} | StopOrderID: {stop_id}", alert=True)
                                    else:
                                        log(f"⚠️ Failed to place stop loss for SELL {user_sym} after entry.", alert=True)
                                else:
                                    log(f"⚠️ SELL placed for {user_sym} but not filled. State: {order_status} | Resp: {resp}", alert=True)
                            except Exception as e:
                                log(f"⚠️ Unexpected create market order response: {e} | resp: {resp}", alert=True)
                        else:
                            log(f"⚠️ SELL order failed to be placed for {user_sym}", alert=True)

                    # SELL MANAGEMENT
                    elif option == 2:
                        stop_id = state['stop_order_id']
                        if stop_id:
                            cancel_order(stop_id)
                            new_stop = create_stop_limit_order(pair=pair, side="buy", qty=ORDER_QTY, stop_price=EMA_21, limit_price=EMA_21)
                            if new_stop:
                                new_stop_id = new_stop[0].get("id") if isinstance(new_stop, list) else new_stop.get("id")
                                state['stop_order_id'] = new_stop_id
                                log(f"🔒 Updated Stop Loss for SELL {user_sym} to {EMA_21} | new StopOrderID: {new_stop_id}", alert=True)
                            positions = list_positions()
                            try:
                                if isinstance(positions, list):
                                    for pos in positions:
                                        if pos.get("pair") == pair:
                                            if float(pos.get("active_pos", 0)) == 0 and state['entry_price'] is not None:
                                                exit_price = EMA_21
                                                entry_price = state['entry_price']
                                                pnl = (entry_price - exit_price) * ORDER_QTY
                                                state.update({'option': 0, 'stop_order_id': None, 'main_order_id': None, 'exit_price': exit_price, 'pnl': pnl})
                                                log(f"🔴 SELL Stop Loss Triggered on {user_sym} — Exit: {exit_price} | PnL: {pnl:.2f}", alert=True)
                            except Exception:
                                pass

                # Print status
                df_status = pd.DataFrame.from_dict(renko_state, orient='index')
                print("\nCurrent Strategy Status:")
                print(df_status[['Date', 'close', 'option', 'single', 'pnl']])

                log("Waiting for next cycle...\n")
                t.sleep(55)

            else:
                # short sleep to avoid busy loop; keep frequent checks but only operate at exact times
                t.sleep(1)

    except KeyboardInterrupt:
        log("🛑 Manual shutdown detected. Exiting gracefully...", alert=True)
        # cancel stop orders
        for s in renko_state:
            sid = renko_state[s]['stop_order_id']
            if sid:
                log(f"❌ Cancelling stop order {sid} for {s}", alert=True)
                cancel_order(sid)
    except Exception as e:
        log(f"[ERROR] {type(e).__name__}: {e}", alert=True)

if __name__ == "__main__":
    main()
