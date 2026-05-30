import os
import time
import requests

from delta_rest_client import DeltaRestClient
from dotenv import load_dotenv

load_dotenv()


class OrderManager:

    def __init__(self):

        api_key = os.getenv("DELTA_API_KEY")
        api_secret = os.getenv("DELTA_API_SECRET")

        if not api_key or not api_secret:
            raise ValueError("Missing API credentials")

        self.client = DeltaRestClient(
            base_url="https://api.india.delta.exchange",
            api_key=api_key,
            api_secret=api_secret
        )

        self.tg_token = os.getenv("BOT_TOKEN")
        self.tg_chat_id = os.getenv("CHAT_ID")

        self._last_tg = {}
        self.product_cache = {}

    def send_telegram(self, msg, key=None, cooldown=20):

        try:

            if not self.tg_token:
                return

            if not self.tg_chat_id:
                return

            now = time.time()

            if key:

                last = self._last_tg.get(key)

                if last and now - last < cooldown:
                    return

                self._last_tg[key] = now

            requests.post(
                f"https://api.telegram.org/bot{self.tg_token}/sendMessage",
                json={
                    "chat_id": self.tg_chat_id,
                    "text": msg
                },
                timeout=5
            )

        except Exception as e:
            print(f"Telegram error: {e}")

    def _request(self, method, endpoint, payload=None, retries=3):

        for i in range(retries):

            try:

                response = self.client.request(
                    method,
                    endpoint,
                    payload,
                    auth=True
                )

                res = (
                    response.json()
                    if hasattr(response, "json")
                    else response
                )

                if res:

                    if res.get("success"):
                        return res

                    error_code = (
                        res.get("error", {}).get("code")
                    )

                    if error_code == "bracket_order_position_exists":
                        print("Bracket already exists on position")
                        return {"success": True, "result": res}

                    print(f"API error [{method} {endpoint}]: {res}")

            except Exception as e:

                msg = str(e)

                if "bracket_order_position_exists" in msg:
                    print("Bracket already exists on position")
                    return {"success": True}

                print(f"Retry {i+1}/{retries}: {method} {endpoint} -> {e}")

            time.sleep(1)

        return None

    def get_product_id(self, symbol):

        try:

            if symbol in self.product_cache:
                return self.product_cache[symbol]

            res = self._request(
                "GET",
                f"/v2/products/{symbol}"
            )

            if not res:
                return None

            product_id = res["result"]["id"]

            self.product_cache[symbol] = product_id

            return product_id

        except Exception as e:
            print(f"Product lookup error: {e}")
            return None

    def place_order(
        self,
        size,
        side,
        symbol=None,
        product_id=None,
        order_type="market",
        limit_price=None,
        reduce_only=False
    ):

        try:

            if not product_id:

                if not symbol:
                    raise ValueError(
                        "Either symbol or product_id required"
                    )

                product_id = self.get_product_id(symbol)

            payload = {
                "product_id": product_id,
                "size": int(size),
                "side": side,
                "reduce_only": "true" if reduce_only else "false",
                "order_type": (
                    "market_order"
                    if order_type == "market"
                    else "limit_order"
                )
            }

            if order_type == "limit":

                if limit_price is None:
                    raise ValueError("limit_price required")

                payload["limit_price"] = str(limit_price)

            res = self._request(
                "POST",
                "/v2/orders",
                payload
            )

            if res:
                self.send_telegram(
                    f"ORDER {side.upper()} {size} {symbol or product_id}",
                    key="order"
                )

            return res

        except Exception as e:

            msg = f"ORDER ERROR: {e}"
            print(msg)

            self.send_telegram(
                msg,
                key="order_error"
            )

            return None

    def place_bracket_order(
        self,
        symbol=None,
        product_id=None,
        stop_loss_price=None,
        take_profit_price=None,
        trigger="mark_price"
    ):

        try:

            if not product_id:

                if not symbol:
                    raise ValueError(
                        "Either symbol or product_id required"
                    )

                product_id = self.get_product_id(symbol)

            payload = {
                "product_id": product_id,
                "bracket_stop_trigger_method": trigger
            }

            if stop_loss_price is not None:

                payload["stop_loss_order"] = {
                    "order_type": "market_order",
                    "stop_price": str(round(stop_loss_price, 1))
                }

            if take_profit_price is not None:

                payload["take_profit_order"] = {
                    "order_type": "market_order",
                    "stop_price": str(round(take_profit_price, 1))
                }

            res = self._request(
                "POST",
                "/v2/orders/bracket",
                payload
            )

            if res:

                self.send_telegram(
                    f"BRACKET SET\n"
                    f"Symbol : {symbol or product_id}\n"
                    f"SL     : {stop_loss_price}\n"
                    f"TP     : {take_profit_price}",
                    key=f"bracket_{product_id}"
                )

            return res

        except Exception as e:

            msg = f"BRACKET ORDER ERROR: {e}"

            print(msg)

            self.send_telegram(
                msg,
                key="bracket_error"
            )

            return None

    def cancel_bracket_order(self, symbol=None, product_id=None):

        try:

            if not product_id:

                if not symbol:
                    raise ValueError(
                        "Either symbol or product_id required"
                    )

                product_id = self.get_product_id(symbol)

            live_orders = self.get_live_orders(product_id)

            if not live_orders:
                return {"success": True}

            cancelled = 0

            for order in live_orders:

                if not order.get("stop_order_type"):
                    continue

                order_id = order.get("id")

                res = self.cancel_order(
                    order_id,
                    product_id
                )

                if res and res.get("success"):
                    cancelled += 1

            self.send_telegram(
                f"BRACKET CANCELLED\n"
                f"Cancelled: {cancelled}",
                key=f"cancel_bracket_{product_id}"
            )

            return {
                "success": True,
                "cancelled": cancelled
            }

        except Exception as e:

            msg = f"CANCEL BRACKET ERROR: {e}"

            print(msg)

            self.send_telegram(
                msg,
                key="cancel_bracket_error"
            )

            return None

    def has_open_bracket_order(self, product_id):

        try:

            live_orders = self.get_live_orders(
                product_id=product_id
            )

            for order in live_orders:

                if order.get("stop_order_type"):
                    return True

            return False

        except Exception as e:

            print(f"Bracket check error: {e}")

            return False

    def get_live_orders(self, product_id=None):

        try:

            endpoint = "/v2/orders"

            params = "?state=open"

            if product_id:
                params += f"&product_id={product_id}"

            res = self._request(
                "GET",
                endpoint + params
            )

            if not res:
                return []

            return res.get("result", [])

        except Exception as e:

            print(f"Live orders error: {e}")

            return []

    def cancel_order(self, order_id, product_id):

        try:

            payload = {
                "id": order_id,
                "product_id": product_id
            }

            return self._request(
                "DELETE",
                "/v2/orders",
                payload
            )

        except Exception as e:

            print(f"Cancel error: {e}")

            return None

    def cancel_all_orders(self, product_id):

        try:

            payload = {
                "product_id": product_id
            }

            return self._request(
                "DELETE",
                "/v2/orders/all",
                payload
            )

        except Exception as e:

            print(f"Cancel all error: {e}")

            return None

    def get_positions(self, product_id=None):

        try:

            endpoint = "/v2/positions"

            if product_id:
                endpoint += f"?product_id={product_id}"

            res = self._request(
                "GET",
                endpoint
            )

            if not res:
                return []

            return res.get("result", [])

        except Exception as e:

            print(f"Position fetch error: {e}")

            return []

    # FIX ADDED
    def get_position(self, product_id):

        try:

            positions = self.get_positions(
                product_id=product_id
            )

            if not positions:
                return None

            for pos in positions:

                if int(pos["product_id"]) != int(product_id):
                    continue

                size = abs(float(
                    pos.get("size")
                    or pos.get("qty")
                    or 0
                ))

                if size <= 0:
                    continue

                return {
                    "product_id": product_id,
                    "side": (
                        "long"
                        if float(pos.get("size", 0)) > 0
                        else "short"
                    ),
                    "qty": size
                }

            return None

        except Exception as e:

            print(f"Get position error: {e}")

            return None