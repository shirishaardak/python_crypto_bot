import os
import time
import requests
import pandas as pd
from datetime import datetime, timedelta
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry



class TradingUtils:

    def __init__(self, contract_size, taker_fee,
                 timeframe, days,
                 telegram_token=None, telegram_chat_id=None,
                 bot_name="BOT"):

        self.CONTRACT_SIZE = contract_size
        self.TAKER_FEE = taker_fee
        self.TIMEFRAME = timeframe
        self.timeframe = timeframe
        self.DAYS = days
        self.BOT_NAME = bot_name

        # PATH AUTO
        self.BASE_DIR = os.getcwd()
        self.SAVE_DIR = os.path.join(self.BASE_DIR, "data", self.BOT_NAME)
        os.makedirs(self.SAVE_DIR, exist_ok=True)

        self.TRADE_CSV = os.path.join(self.SAVE_DIR, "live_trades.csv")

        # TELEGRAM
        self.TELEGRAM_TOKEN = telegram_token
        self.TELEGRAM_CHAT_ID = telegram_chat_id
        self._last_tg = {}

        # SESSION
        self.session = requests.Session()
        retry = Retry(total=5, backoff_factor=1,
                      status_forcelist=[429, 500, 502, 503, 504])
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("https://", adapter)

    # ================= FIXED MULTI-TIMEFRAME CANDLES =================

    def fetch_candles(self, symbol, timeframe=None):

        # default = original timeframe
        tf = timeframe if timeframe is not None else self.TIMEFRAME

        start = int((datetime.now() - timedelta(days=self.DAYS)).timestamp())

        data = self.safe_get(
            "https://api.india.delta.exchange/v2/history/candles",
            params={
                "resolution": tf,
                "symbol": symbol,
                "start": str(start),
                "end": str(int(time.time()))
            }
        )

        if not data or "result" not in data:
            return pd.DataFrame()

        df = pd.DataFrame(
            data["result"],
            columns=["time", "open", "high", "low", "close", "volume"]
        )

        if df.empty:
            return df

        df.rename(columns=str.title, inplace=True)

        df["Time"] = pd.to_datetime(df["Time"], unit="s")
        df.set_index("Time", inplace=True)
        df.sort_index(inplace=True)

        return df.astype(float)

    # ================= REST SAME =================

    def log(self, msg, tg=False, key=None):
        text = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
        print(text)
        if tg:
            self.send_telegram(text, key)

    def send_telegram(self, msg, key=None, cooldown=30):
        try:
            now = time.time()

            if key and key in self._last_tg and now - self._last_tg[key] < cooldown:
                return

            if key:
                self._last_tg[key] = now

            msg = f"{self.BOT_NAME} | {msg}"

            self.session.post(
                f"https://api.telegram.org/bot{self.TELEGRAM_TOKEN}/sendMessage",
                json={"chat_id": self.TELEGRAM_CHAT_ID, "text": msg},
                timeout=5
            )
        except:
            pass

    def safe_get(self, url, params=None):
        try:
            r = self.session.get(url, params=params, timeout=10)
            if r.status_code == 200:
                return r.json()
        except:
            pass
        return None

    def fetch_price(self, symbol):
        data = self.safe_get(f"https://api.india.delta.exchange/v2/tickers/{symbol}")
        try:
            return float(data["result"]["mark_price"])
        except:
            return None

    def commission(self, price, qty, symbol):
        return price * self.CONTRACT_SIZE[symbol] * qty * self.TAKER_FEE

    def save_trade(self, trade):
        t = trade.copy()

        for k in ["entry_time", "exit_time"]:
            t[k] = t[k].strftime("%Y-%m-%d %H:%M:%S")

        pd.DataFrame([t])[[ 
            "entry_time","exit_time","symbol","side",
            "entry_price","exit_price","qty","net_pnl"
        ]].to_csv(
            self.TRADE_CSV,
            mode="a",
            header=not os.path.exists(self.TRADE_CSV),
            index=False
        )