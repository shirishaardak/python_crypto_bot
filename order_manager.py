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

    # FIX 1: Use cancel_all_orders instead of filtering by stop_order_type
    # which was always None for bracket orders, causing nothing to be cancelled
    def cancel_bracket_order(self, symbol=None, product_id=None):

        try:

            if not product_id:

                if not symbol:
                    raise ValueError(
                        "Either symbol or product_id required"
                    )

                product_id = self.get_product_id(symbol)

            res = self.cancel_all_orders(product_id)

            if res and res.get("success"):

                self.send_telegram(
                    f"BRACKET CANCELLED for {symbol or product_id}",
                    key=f"cancel_bracket_{product_id}"
                )

                return {"success": True}

            print(f"cancel_bracket_order failed: {res}")

            return {"success": False}

        except Exception as e:

            msg = f"CANCEL BRACKET ERROR: {e}"

            print(msg)

            self.send_telegram(
                msg,
                key="cancel_bracket_error"
            )

            return None

    # FIX 2: Any open order for this product means bracket is still active
    # Old code checked stop_order_type which was always None → always False
    def has_open_bracket_order(self, product_id):

        try:

            live_orders = self.get_live_orders(
                product_id=product_id
            )

            return len(live_orders) > 0

        except Exception as e:

            print(f"Bracket check error: {e}")

            return False

    # FIX 3: Call client directly for GET with query params
    # Old _request() was sending params as body payload for GET → wrong results
    def get_live_orders(self, product_id=None):

        try:

            endpoint = "/v2/orders?state=open"

            if product_id:
                endpoint += f"&product_id={product_id}"

            response = self.client.request(
                "GET",
                endpoint,
                None,
                auth=True
            )

            res = (
                response.json()
                if hasattr(response, "json")
                else response
            )

            if not res or not res.get("success"):
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

            res = self._request(
                "DELETE",
                "/v2/orders/all",
                payload
            )

            return res

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

            result = res.get("result", [])

            # Handle case where result is a single dict instead of a list
            if isinstance(result, dict):
                result = [result]

            # Add product_id to each position if missing
            for pos in result:
                if "product_id" not in pos and product_id:
                    pos["product_id"] = product_id

            return result

        except Exception as e:

            print(f"Position fetch error: {e}")

            return []

    def get_position(self, product_id):

        try:

            positions = self.get_positions(
                product_id=product_id
            )

            if not positions:
                return None

            for pos in positions:

                # Guard against malformed response
                if not isinstance(pos, dict):
                    print(f"Unexpected position format: {pos}")
                    continue

                # Guard against missing product_id
                if "product_id" not in pos:
                    print(f"Position missing product_id: {pos}")
                    continue

                # Get size - handle both 'size' and 'qty' keys
                size_value = float(pos.get("size") or pos.get("qty") or 0)
                size = abs(size_value)

                if size <= 0:
                    continue

                # Determine side from size sign
                side = "buy" if size_value > 0 else "sell"

                return {
                    "product_id": product_id,
                    "side": side,
                    "size": size
                }

            return None

        except Exception as e:

            print(f"Get position error: {e}")

            return None