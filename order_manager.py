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
        api_key = os.getenv('DELTA_API_KEY')
        api_secret = os.getenv('DELTA_API_SECRET')

        if not api_key or not api_secret:
            raise ValueError("Missing API credentials")

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
    # INTERNAL NORMALIZER
    # ==============================
    def _normalize_order(self, kwargs):
        k = kwargs.copy()

        # ---------- SIDE ----------
        if "side" not in k:
            raise ValueError("Missing side")

        k["side"] = k["side"].lower()
        if k["side"] not in ["buy", "sell"]:
            raise ValueError("Invalid side")

        # ---------- ORDER TYPE ----------
        if "order_type" not in k:
            raise ValueError("Missing order_type")

        ot = k["order_type"].lower()

        if ot in ["market", "market_order"]:
            k["order_type"] = "market_order"
        elif ot in ["limit", "limit_order"]:
            k["order_type"] = "limit_order"
        else:
            raise ValueError("Invalid order_type")

        # ---------- PRICE ----------
        if "limit_price" in k:
            k["limit_price"] = float(k["limit_price"])

        # ---------- REDUCE ONLY ----------
        if "reduce_only" in k:
            k["reduce_only"] = bool(k["reduce_only"])
        else:
            k["reduce_only"] = False

        # ---------- CLEAN ----------
        k.pop("product_symbol", None)
        k.pop("stop_order_type", None)

        return k

    # ==============================
    # PLACE ORDER
    # ==============================
    def place_order(self, **kwargs):
        try:
            k = self._normalize_order(kwargs)

            res = self.client.place_order(**k)

            if not res or "result" not in res:
                raise Exception(f"Bad response: {res}")

            self.send_telegram(f"✅ ORDER: {k}", key="order")
            return res

        except Exception as e:
            msg = f"❌ PLACE ERROR: {e} | {kwargs}"
            print(msg)
            self.send_telegram(msg, key="place_error")
            return None

    # ==============================
    # PLACE STOP LOSS
    # ==============================
    def place_stop_order(self, **kwargs):
        try:
            k = kwargs.copy()

            # SIDE
            k["side"] = k["side"].lower()

            # PRICE
            if "stop_price" not in k:
                raise ValueError("Missing stop_price")

            k["stop_price"] = float(k["stop_price"])

            # DEFAULT TYPE
            k["order_type"] = "market_order"

            k.pop("stop_order_type", None)

            res = self.client.place_stop_order(**k)

            if not res or "result" not in res:
                raise Exception(f"Bad response: {res}")

            self.send_telegram(f"🛑 SL: {k}", key="sl")
            return res

        except Exception as e:
            msg = f"❌ SL ERROR: {e} | {kwargs}"
            print(msg)
            self.send_telegram(msg, key="sl_error")
            return None

    # ==============================
    # CANCEL ORDER
    # ==============================
    def cancel_order(self, order_id, product_id):
        try:
            res = self.client.cancel_order(order_id, product_id)

            self.send_telegram(f"⚠️ CANCELLED: {order_id}", key="cancel")
            return res

        except Exception as e:
            msg = f"❌ CANCEL ERROR: {e}"
            print(msg)
            self.send_telegram(msg, key="cancel_error")
            return None

    # ==============================
    # LIVE ORDERS
    # ==============================
    def get_live_orders(self):
        try:
            return self.client.get_live_orders()
        except Exception as e:
            msg = f"❌ LIVE ORDER ERROR: {e}"
            print(msg)
            self.send_telegram(msg, key="live_error")
            return []

    # ==============================
    # POSITION CHECK (IMPORTANT)
    # ==============================
    def has_open_position(self, product_id):
        try:
            positions = self.client.get_positions()

            for p in positions.get("result", []):
                if p["product_id"] == product_id and float(p["size"]) != 0:
                    return True

            return False

        except Exception as e:
            print(f"Position check error: {e}")
            return False