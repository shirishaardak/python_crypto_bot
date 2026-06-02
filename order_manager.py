import os
import time
import hmac
import json
import hashlib
import requests

from dotenv import load_dotenv

load_dotenv()


# ================= CONFIG =================

# Production (Delta India). For testnet use:
#   https://cdn-ind.testnet.deltaex.org
BASE_URL = os.getenv("DELTA_BASE_URL", "https://api.india.delta.exchange")

API_KEY = os.getenv("DELTA_API_KEY")
API_SECRET = os.getenv("DELTA_API_SECRET")

# Telegram (reuse your existing bot token + chat id)
TG_TOKEN = os.getenv("TELEGRAM_TOKEN")
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Symbol -> Delta product_id. Orders API requires product_id.
# 27 is BTCUSD on Delta. VERIFY against /v2/products before live trading.
PRODUCT_IDS = {
    "BTCUSD": 27,
}

# How long to wait for the HTTP response. (connect, read) seconds.
# Keep read tight so a hung order fails fast instead of blocking the loop.
ORDER_TIMEOUT = (3, 5)

# Retries on transient network/5xx errors only (never on a clean reject).
MAX_RETRIES = 2


class OrderManager:
    """
    Fast, self-contained order layer for Delta Exchange.

    Public method used by the strategy:
        place_order(size, side, symbol, reduce_only=False)

    Returns a dict:
        {"success": True,  "order_id": ..., "state": ..., "filled": ...,
         "avg_price": ..., "raw": {...}}
        {"success": False, "error": "<code/message>", "raw": {...}}
    """

    def __init__(self):
        # Persistent session = reused TCP/TLS connection = much faster orders.
        self.session = requests.Session()

        # A small connection pool keyed to the host.
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=4,
            pool_maxsize=4,
            max_retries=0,   # we handle retries ourselves, selectively
        )
        self.session.mount("https://", adapter)

        # Static headers; per-request we add timestamp + signature.
        self.session.headers.update({
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "py-trend-bot",  # REQUIRED by Delta or you get 4xx
        })

        if not API_KEY or not API_SECRET:
            self._tg("⚠️ OrderManager: API key/secret missing in env")

    # ================= TELEGRAM =================

    def _tg(self, text):
        """Fire-and-forget Telegram message. Never blocks trading on failure."""
        if not TG_TOKEN or not TG_CHAT_ID:
            print(text)
            return
        try:
            self.session.post(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                json={"chat_id": TG_CHAT_ID, "text": text},
                timeout=(2, 3),
            )
        except Exception:
            # A failed alert must never crash or slow the order path.
            print(text)

    # ================= SIGNING =================

    def _signature(self, method, path, query, body, timestamp):
        # Delta prehash: method + timestamp + path + query + body
        message = method + timestamp + path + query + body
        return hmac.new(
            API_SECRET.encode("utf-8"),
            message.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _signed_headers(self, method, path, query, body):
        # Timestamp must be fresh: Delta rejects signatures older than 5s.
        timestamp = str(int(time.time()))
        sig = self._signature(method, path, query, body, timestamp)
        return {
            "api-key": API_KEY,
            "timestamp": timestamp,
            "signature": sig,
        }

    # ================= CORE REQUEST =================

    def _post(self, path, payload):
        """
        Signed POST with selective retry. Returns parsed JSON dict, or
        {"success": False, "error": "..."} on hard failure.
        """
        body = json.dumps(payload, separators=(",", ":"))  # compact, stable
        url = BASE_URL + path

        last_err = None

        for attempt in range(MAX_RETRIES + 1):
            # Re-sign every attempt: the timestamp must stay within 5s.
            headers = self._signed_headers("POST", path, "", body)
            try:
                resp = self.session.post(
                    url,
                    data=body,
                    headers=headers,
                    timeout=ORDER_TIMEOUT,
                )
            except requests.exceptions.RequestException as e:
                # Network-level failure: worth a retry.
                last_err = f"network: {e}"
                time.sleep(0.2 * (attempt + 1))
                continue

            # Try to parse JSON regardless of status code.
            try:
                data = resp.json()
            except ValueError:
                last_err = f"bad_json http={resp.status_code} body={resp.text[:200]}"
                # 5xx with no JSON: retry. 4xx: don't.
                if 500 <= resp.status_code < 600:
                    time.sleep(0.2 * (attempt + 1))
                    continue
                return {"success": False, "error": last_err}

            if data.get("success"):
                return data

            # Clean rejection from Delta (e.g. insufficient_margin). Don't retry.
            err = data.get("error", {})
            code = err.get("code") if isinstance(err, dict) else err
            ctx = err.get("context") if isinstance(err, dict) else None

            # Rate limit (429) is the one reject worth a brief retry.
            if resp.status_code == 429:
                last_err = f"rate_limited {code}"
                time.sleep(0.3 * (attempt + 1))
                continue

            return {
                "success": False,
                "error": code or "unknown_error",
                "context": ctx,
                "raw": data,
            }

        return {"success": False, "error": last_err or "max_retries_exhausted"}

    # ================= PLACE ORDER =================

    def place_order(self, size, side, symbol, reduce_only=False):
        """
        Place a MARKET order and confirm it actually went through.

        size        : integer number of contracts
        side        : "buy" or "sell"
        symbol      : e.g. "BTCUSD"
        reduce_only : True for exits (won't flip into a new position)
        """
        product_id = PRODUCT_IDS.get(symbol)
        if product_id is None:
            msg = f"❌ No product_id mapped for {symbol}"
            self._tg(msg)
            return {"success": False, "error": "no_product_id"}

        payload = {
            "product_id": product_id,
            "size": int(size),
            "side": side,                 # "buy" / "sell"
            "order_type": "market_order",
            "time_in_force": "ioc",       # fill now or cancel; no resting order
            "reduce_only": bool(reduce_only),
        }

        t0 = time.time()
        result = self._post("/v2/orders", payload)
        elapsed_ms = (time.time() - t0) * 1000

        tag = "EXIT" if reduce_only else "ENTRY"

        if not result.get("success"):
            err = result.get("error")
            ctx = result.get("context")
            self._tg(
                f"🚨 ORDER FAILED [{tag}] {symbol} {side.upper()} {size}\n"
                f"reason: {err}" + (f"\nctx: {ctx}" if ctx else "")
            )
            return {"success": False, "error": err, "raw": result}

        # ---- Confirm the fill from the returned order object ----
        order = result.get("result", {}) or {}
        order_id = order.get("id")
        state = order.get("state")
        size_req = int(order.get("size", size) or size)
        unfilled = int(order.get("unfilled_size", 0) or 0)
        filled = size_req - unfilled

        # avg fill price field name can vary; try common ones.
        avg_price = (
            order.get("average_fill_price")
            or order.get("avg_fill_price")
            or order.get("limit_price")
        )

        # A market/IOC order should be closed (fully filled) or partially filled.
        if state == "closed" and unfilled == 0:
            self._tg(
                f"✅ {tag} FILLED {symbol} {side.upper()} {filled} "
                f"@ {avg_price} | id={order_id} | {elapsed_ms:.0f}ms"
            )
        elif filled > 0:
            self._tg(
                f"⚠️ {tag} PARTIAL {symbol} {side.upper()} "
                f"{filled}/{size_req} @ {avg_price} | id={order_id} "
                f"| state={state} | {elapsed_ms:.0f}ms"
            )
        else:
            # Accepted but nothing filled (rare for IOC market — flag it loudly).
            self._tg(
                f"❗ {tag} NOT FILLED {symbol} {side.upper()} {size_req} "
                f"| state={state} | id={order_id} | {elapsed_ms:.0f}ms"
            )
            return {
                "success": False,
                "error": f"not_filled state={state}",
                "order_id": order_id,
                "raw": result,
            }

        return {
            "success": True,
            "order_id": order_id,
            "state": state,
            "filled": filled,
            "avg_price": float(avg_price) if avg_price else None,
            "elapsed_ms": elapsed_ms,
            "raw": result,
        }