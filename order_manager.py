import os
import time
import requests
from delta_rest_client import DeltaRestClient
from dotenv import load_dotenv  # adjust if needed

load_dotenv()

class OrderManager:
    def __init__(self):
        # ==============================
        # API CONFIG
        # ==============================
        api_key = os.getenv('DELTA_API_KEY')
        api_secret = os.getenv('DELTA_API_SECRET')

        if not api_key or not api_secret:
            raise ValueError("Missing API credentials in environment variables")

        self.client = DeltaRestClient(
            base_url='https://api.india.delta.exchange',
            api_key=api_key,
            api_secret=api_secret
        )

        # ==============================
        # TELEGRAM CONFIG
        # ==============================
        self.tg_token = os.getenv("BOT_TOKEN")
        self.tg_chat_id = os.getenv("CHAT_ID")

        self._last_tg = {}  # cooldown tracking

    # ==============================
    # TELEGRAM FUNCTION (BOT LEVEL)
    # ==============================
    def send_telegram(self, msg, key=None, cooldown=30):
        try:
            if not self.tg_token or not self.tg_chat_id:
                return

            now = time.time()

            # Prevent spam (cooldown per key)
            if key:
                if key in self._last_tg and now - self._last_tg[key] < cooldown:
                    return
                self._last_tg[key] = now

            url = f"https://api.telegram.org/bot{self.tg_token}/sendMessage"

            payload = {
                "chat_id": self.tg_chat_id,
                "text": msg
            }

            requests.post(url, json=payload, timeout=5)

        except Exception as e:
            print(f"[Telegram Error]: {e}")

    # -----------------------------
    # Place Order
    # -----------------------------
    def place_order(self, **kwargs):
        try:
            res = self.client.place_order(**kwargs)
            self.send_telegram(f"✅ Order Placed: {kwargs}", key="order")
            return res
        except Exception as e:
            msg = f"❌ Error placing order: {e}"
            print(msg)
            self.send_telegram(msg, key="place_error")
            return None

    # -----------------------------
    # Place Stop Order
    # -----------------------------
    def place_stop_order(self, **kwargs):
        try:
            res = self.client.place_stop_order(**kwargs)
            self.send_telegram(f"🛑 Stop Order Placed: {kwargs}", key="stop_order")
            return res
        except Exception as e:
            msg = f"❌ Error placing stop order: {e}"
            print(msg)
            self.send_telegram(msg, key="stop_error")
            return None

    # -----------------------------
    # Cancel Order
    # -----------------------------
    def cancel_order(self, order_id, product_id):
        try:
            res = self.client.cancel_order(order_id, product_id)
            self.send_telegram(f"⚠️ Order Cancelled: {order_id}", key="cancel")
            return res
        except Exception as e:
            msg = f"❌ Error cancelling order {order_id}: {e}"
            print(msg)
            self.send_telegram(msg, key="cancel_error")
            return None

    # -----------------------------
    # Edit Stop Order
    # -----------------------------
    def edit_stop_order(self, order_id, product_id, new_stop_price):
        try:
            endpoint = f"/v2/orders/{order_id}"
            payload = {
                "product_id": product_id,
                "stop_price": str(new_stop_price)
            }

            res = self.client.request("PUT", endpoint, payload, auth=True)

            self.send_telegram(
                f"🔁 SL Updated: {order_id} → {new_stop_price}",
                key="edit_sl"
            )
            return res

        except Exception as e:
            msg = f"❌ Error editing order {order_id}: {e}"
            print(msg)
            self.send_telegram(msg, key="edit_error")
            return None

    # -----------------------------
    # Get Order History
    # -----------------------------
    def get_order_history(self, product_id, page_size=10):
        try:
            query = {"product_id": product_id}
            response = self.client.order_history(query, page_size=page_size)
            return response.get("result", [])
        except Exception as e:
            msg = f"❌ Error getting order history: {e}"
            print(msg)
            self.send_telegram(msg, key="history_error")
            return []

    # -----------------------------
    # Get Live Orders
    # -----------------------------
    def get_live_orders(self):
        try:
            return self.client.get_live_orders()
        except Exception as e:
            msg = f"❌ Error getting live orders: {e}"
            print(msg)
            self.send_telegram(msg, key="live_error")
            return []