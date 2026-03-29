import os
import time
import requests
from delta_rest_client import DeltaRestClient
from dotenv import load_dotenv

load_dotenv()


class OrderManager:
    def __init__(self):

        # ==============================
        # API CONFIG
        # ==============================
        api_key = os.getenv("DELTA_API_KEY")
        api_secret = os.getenv("DELTA_API_SECRET")

        if not api_key or not api_secret:
            raise ValueError("Missing API credentials")

        self.client = DeltaRestClient(
            base_url="https://api.india.delta.exchange",
            api_key=api_key,
            api_secret=api_secret
        )

        # ==============================
        # TELEGRAM CONFIG
        # ==============================
        self.tg_token = os.getenv("BOT_TOKEN")
        self.tg_chat_id = os.getenv("CHAT_ID")

        self._last_tg = {}

    # ==============================
    # TELEGRAM
    # ==============================
    def send_telegram(self, msg, key=None, cooldown=20):
        try:
            if not self.tg_token or not self.tg_chat_id:
                return

            now = time.time()

            if key:
                if key in self._last_tg and now - self._last_tg[key] < cooldown:
                    return
                self._last_tg[key] = now

            url = f"https://api.telegram.org/bot{self.tg_token}/sendMessage"

            requests.post(
                url,
                json={"chat_id": self.tg_chat_id, "text": msg},
                timeout=5
            )

        except Exception as e:
            print(f"[Telegram Error] {e}")

    # ==============================
    # INTERNAL SAFE REQUEST
    # ==============================
    def _request(self, method, endpoint, payload=None):
        try:
            res = self.client.request(
                method,
                endpoint,
                payload,
                auth=True
            )

            if not res:
                raise Exception("Empty response")

            if not res.get("success"):
                raise Exception(res)

            return res

        except Exception as e:
            print(f"[API ERROR] {method} {endpoint} → {e}")
            return None

    # ==============================
    # PLACE ORDER
    # ==============================
    def place_order(self, product_id, size, side,
                    order_type="market",
                    limit_price=None,
                    reduce_only=False):

        try:
            payload = {
                "product_id": product_id,
                "size": size,
                "side": side,
                "order_type": "market" if order_type == "market" else "limit",
                "reduce_only": reduce_only
            }

            if payload["order_type"] == "limit":
                if limit_price is None:
                    raise ValueError("limit_price required for limit order")
                payload["limit_price"] = str(limit_price)

            res = self._request("POST", "/v2/orders", payload)

            if not res:
                return None

            self.send_telegram(f"✅ ORDER: {side.upper()} {size}", key="order")
            return res

        except Exception as e:
            msg = f"❌ ORDER ERROR: {e}"
            print(msg)
            self.send_telegram(msg, key="order_error")
            return None

    # ==============================
    # STOP LOSS ORDER
    # ==============================
    def place_stop_order(self, product_id, size, side, stop_price):

        try:
            payload = {
                "product_id": product_id,
                "size": size,
                "side": side,
                "order_type": "market",
                "stop_order_type": "stop_loss_order",
                "stop_price": str(stop_price),
                "reduce_only": True
            }

            res = self._request("POST", "/v2/orders", payload)

            if not res:
                return None

            self.send_telegram(f"🛑 SL PLACED @ {stop_price}", key="sl")
            return res

        except Exception as e:
            msg = f"❌ SL ERROR: {e}"
            print(msg)
            self.send_telegram(msg, key="sl_error")
            return None

    # ==============================
    # CANCEL ORDER
    # ==============================
    def cancel_order(self, order_id, product_id):

        try:
            payload = {
                "id": order_id,
                "product_id": product_id
            }

            res = self._request("DELETE", "/v2/orders", payload)

            if res:
                self.send_telegram(f"⚠️ CANCELLED: {order_id}", key="cancel")

            return res

        except Exception as e:
            msg = f"❌ CANCEL ERROR: {e}"
            print(msg)
            self.send_telegram(msg, key="cancel_error")
            return None

    # ==============================
    # GET POSITIONS (FIXED)
    # ==============================
    def get_positions(self, product_id):

        try:
            payload = {"product_id": product_id}  # 🔥 FIX

            res = self._request("GET", "/v2/positions", payload)

            if not res:
                return []

            return res.get("result", [])

        except Exception as e:
            print(f"Position fetch error: {e}")
            return []

    # ==============================
    # CHECK POSITION
    # ==============================
    def has_open_position(self, product_id):

        try:
            positions = self.get_positions(product_id)

            for p in positions:
                if float(p.get("size", 0)) != 0:
                    return True

            return False

        except Exception as e:
            print(f"Position check error: {e}")
            return False

    # ==============================
    # GET LIVE ORDERS
    # ==============================
    def get_live_orders(self):

        try:
            res = self._request("GET", "/v2/orders")

            if not res:
                return []

            return res.get("result", [])

        except Exception as e:
            print(f"Live orders error: {e}")
            return []