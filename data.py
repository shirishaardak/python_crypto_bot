# save as fetch_btc.py, run once:  pip install ccxt
import ccxt, pandas as pd, time

ex = ccxt.binance()
symbol, tf = "BTC/USDT", "1h"
since = ex.parse8601("2017-01-01T00:00:00Z")   # ~2 years back
rows = []
while since < ex.milliseconds():
    batch = ex.fetch_ohlcv(symbol, tf, since=since, limit=1000)
    if not batch:
        break
    rows += batch
    since = batch[-1][0] + 1
    time.sleep(ex.rateLimit / 1000)

df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
df.drop_duplicates("timestamp").to_csv("btc_15m.csv", index=False)
print(f"Saved {len(df)} candles to btc_15m.csv")