"""
market_dashboard.py
===================
ONE FILE. Run it, it downloads 15-minute candles by itself from Delta
Exchange's public API, analyses them, and shows a light dashboard with:

  • Price + time
  • Trend label (UP / DOWN / SIDEWAYS) over the last few hours
  • Support & Resistance levels
  • Buy / Sell zones
  • Heikin-Ashi fractal trendline (with up/down bands)

It is LIVE: pick a refresh interval in the sidebar and it re-downloads,
re-analyses and redraws itself automatically. Start it once, leave it open.

No utils.py, no API key, no CSV. Just run ONCE:

    pip install streamlit plotly pandas numpy requests streamlit-autorefresh
    streamlit run market_dashboard.py
"""

import time
import datetime as dt

import numpy as np
import pandas as pd
import requests
import streamlit as st
import plotly.graph_objects as go
from plotly.subplots import make_subplots

# Auto-refresh component (optional). If not installed we fall back to a
# meta-refresh tag so the page still reloads on its own.
try:
    from streamlit_autorefresh import st_autorefresh
    _HAS_AUTOREFRESH = True
except Exception:
    _HAS_AUTOREFRESH = False

# ============================================================
# CONFIG
# ============================================================
DELTA_BASE = "https://api.india.delta.exchange"   # public REST (no key needed)
RESOLUTION = "15m"                                # 15-minute candles
DEFAULT_SYMBOL = "BTCUSD"
BAR_MINUTES = 15                                  # matches RESOLUTION


# ============================================================
# DATA DOWNLOAD  (Delta public candles endpoint)
# ============================================================
@st.cache_data(ttl=30, show_spinner=False)
def download_candles(symbol: str, days: int, _bust: int = 0) -> pd.DataFrame:
    # `_bust` lets each auto-refresh force a fresh download (cache key changes).
    """
    Pull historical 15m candles from Delta Exchange public API.
    Endpoint: GET /v2/history/candles
    Returns a DataFrame indexed by datetime with Open/High/Low/Close/Volume.
    """
    end = int(time.time())
    start = end - days * 24 * 60 * 60

    url = f"{DELTA_BASE}/v2/history/candles"
    params = {
        "resolution": RESOLUTION,
        "symbol": symbol,
        "start": start,
        "end": end,
    }
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    payload = r.json()

    rows = payload.get("result", []) if isinstance(payload, dict) else payload
    if not rows:
        raise RuntimeError("Delta returned no candles for this symbol/range.")

    df = pd.DataFrame(rows)
    # Delta candle time is epoch seconds
    df["time"] = pd.to_datetime(df["time"], unit="s")
    df = df.rename(columns={"open": "Open", "high": "High",
                            "low": "Low", "close": "Close",
                            "volume": "Volume"})
    for c in ("Open", "High", "Low", "Close", "Volume"):
        if c in df:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df = (df.dropna(subset=["Open", "High", "Low", "Close"])
            .drop_duplicates(subset="time")
            .sort_values("time")
            .set_index("time"))
    return df


# ============================================================
# HEIKIN-ASHI FRACTAL TRENDLINE
# ============================================================
def _heikin_ashi(df: pd.DataFrame) -> pd.DataFrame:
    """Compute Heikin-Ashi OHLC from a normal OHLC DataFrame (no extra deps)."""
    o = df["Open"].to_numpy(float)
    h = df["High"].to_numpy(float)
    l = df["Low"].to_numpy(float)
    c = df["Close"].to_numpy(float)
    n = len(df)

    ha_close = (o + h + l + c) / 4.0
    ha_open = np.empty(n)
    if n:
        ha_open[0] = (o[0] + c[0]) / 2.0
    for i in range(1, n):
        ha_open[i] = (ha_open[i - 1] + ha_close[i - 1]) / 2.0
    ha_high = np.maximum.reduce([h, ha_open, ha_close])
    ha_low = np.minimum.reduce([l, ha_open, ha_close])

    return pd.DataFrame({
        "HA_open": ha_open, "HA_high": ha_high,
        "HA_low": ha_low, "HA_close": ha_close,
    }, index=df.index).reset_index(drop=True)


def calculate_trendline(df: pd.DataFrame, band: float = 50.0) -> pd.DataFrame:
    """
    Heikin-Ashi + non-repainting fractal trendline.
    Returns a DataFrame (positional index) with HA_* columns plus
    Trendline / up_Trendline / down_Trendline.
    """
    ha = _heikin_ashi(df)

    # ---- FRACTALS (non-repainting, confirmed 2 candles later) ----
    ha["high_fractal"] = np.nan
    ha["low_fractal"] = np.nan

    for i in range(2, len(ha) - 2):
        is_high = (
            ha.loc[i, "HA_high"] > ha.loc[i - 1, "HA_high"]
            and ha.loc[i, "HA_high"] > ha.loc[i - 2, "HA_high"]
            and ha.loc[i, "HA_high"] > ha.loc[i + 1, "HA_high"]
            and ha.loc[i, "HA_high"] > ha.loc[i + 2, "HA_high"]
        )
        is_low = (
            ha.loc[i, "HA_low"] < ha.loc[i - 1, "HA_low"]
            and ha.loc[i, "HA_low"] < ha.loc[i - 2, "HA_low"]
            and ha.loc[i, "HA_low"] < ha.loc[i + 1, "HA_low"]
            and ha.loc[i, "HA_low"] < ha.loc[i + 2, "HA_low"]
        )
        if is_high:
            ha.loc[i + 2, "high_fractal"] = ha.loc[i, "HA_high"]
        if is_low:
            ha.loc[i + 2, "low_fractal"] = ha.loc[i, "HA_low"]

    # ---- TRENDLINE ----
    ha["Trendline"] = np.nan
    last_high_fractal = np.nan
    last_low_fractal = np.nan
    trendline = ha.loc[0, "HA_close"] if len(ha) else np.nan

    for i in range(1, len(ha)):
        if not np.isnan(ha.loc[i, "high_fractal"]):
            last_high_fractal = ha.loc[i, "high_fractal"]
        if not np.isnan(ha.loc[i, "low_fractal"]):
            last_low_fractal = ha.loc[i, "low_fractal"]

        current_close = ha.loc[i, "HA_close"]
        prev_close = ha.loc[i - 1, "HA_close"]

        # BULLISH BREAK
        if (
            not np.isnan(last_high_fractal)
            and prev_close <= last_high_fractal
            and current_close > last_high_fractal
            and current_close > trendline
            and not np.isnan(last_low_fractal)
        ):
            trendline = last_low_fractal
        # BEARISH BREAK
        elif (
            not np.isnan(last_low_fractal)
            and prev_close >= last_low_fractal
            and current_close < last_low_fractal
            and current_close < trendline
            and not np.isnan(last_high_fractal)
        ):
            trendline = last_high_fractal

        ha.loc[i, "Trendline"] = trendline
        ha.loc[i, "up_Trendline"] = trendline + band
        ha.loc[i, "down_Trendline"] = trendline - band

    if len(ha):
        ha.loc[0, "up_Trendline"] = trendline + band if not np.isnan(trendline) else np.nan
        ha.loc[0, "down_Trendline"] = trendline - band if not np.isnan(trendline) else np.nan
    return ha


# ============================================================
# ANALYSIS ENGINE (inline so this stays one file)
# ============================================================
def analyse_trend(df, lookback_bars=16, flat_threshold_pct=0.3):
    """Trend over last `lookback_bars` candles. 16 bars = 4h on 15m."""
    window = df.tail(lookback_bars)
    closes = window["Close"].to_numpy(float)
    n = len(closes)
    if n < 5:
        return {"label": "sideways", "strength": 0.0, "slope_pct": 0.0,
                "note": "Not enough data", "ema_fast": np.nan, "ema_slow": np.nan,
                "bars": n}

    x = np.arange(n)
    slope, intercept = np.polyfit(x, closes, 1)
    fit = slope * x + intercept
    slope_pct = (fit[-1] - fit[0]) / fit[0] * 100.0

    ss_res = np.sum((closes - fit) ** 2)
    ss_tot = np.sum((closes - closes.mean()) ** 2)
    r2 = max(0.0, min(1.0, 1 - ss_res / ss_tot if ss_tot > 0 else 0.0))

    ema_fast = window["Close"].ewm(span=max(3, n // 6), adjust=False).mean().iloc[-1]
    ema_slow = window["Close"].ewm(span=max(6, n // 3), adjust=False).mean().iloc[-1]

    if abs(slope_pct) < flat_threshold_pct:
        label, note = "sideways", f"Net move {slope_pct:+.2f}% inside flat band"
    elif slope_pct > 0 and ema_fast >= ema_slow:
        label, note = "up", "Rising regression + fast EMA above slow"
    elif slope_pct < 0 and ema_fast <= ema_slow:
        label, note = "down", "Falling regression + fast EMA below slow"
    else:
        label, note = "sideways", "Regression and EMA disagree (transition)"

    strength = round(r2 * min(1.0, abs(slope_pct) / 2.0), 3)
    return {"label": label, "strength": strength,
            "slope_pct": round(slope_pct, 3), "note": note,
            "ema_fast": round(float(ema_fast), 2),
            "ema_slow": round(float(ema_slow), 2), "bars": n}


def _swings(df, left=2, right=2):
    H, L, idx = df["High"].to_numpy(float), df["Low"].to_numpy(float), df.index
    highs, lows = [], []
    for i in range(left, len(df) - right):
        wh, wl = H[i - left:i + right + 1], L[i - left:i + right + 1]
        if H[i] == wh.max() and wh.argmax() == left:
            highs.append(H[i])
        if L[i] == wl.min() and wl.argmin() == left:
            lows.append(L[i])
    return highs, lows


def _cluster(levels, tol_pct):
    if not levels:
        return []
    levels = sorted(levels)
    groups, g = [], [levels[0]]
    for p in levels[1:]:
        if abs(p - g[-1]) / g[-1] * 100 <= tol_pct:
            g.append(p)
        else:
            groups.append(g); g = [p]
    groups.append(g)
    out = [{"price": round(float(np.mean(x)), 2), "touches": len(x)} for x in groups]
    out.sort(key=lambda d: (-d["touches"], d["price"]))
    return out


def support_resistance(df, lookback_bars=80, tol_pct=0.25, max_levels=4):
    """80 bars = 20h on 15m."""
    w = df.tail(lookback_bars)
    price = float(w["Close"].iloc[-1])
    highs, lows = _swings(w)
    highs.append(float(w["High"].max()))
    lows.append(float(w["Low"].min()))
    res = [c for c in _cluster(highs, tol_pct) if c["price"] > price][:max_levels]
    sup = [c for c in _cluster(lows, tol_pct) if c["price"] < price][:max_levels]
    sup.sort(key=lambda c: -c["price"])
    res.sort(key=lambda c: c["price"])
    return {"price": round(price, 2), "supports": sup, "resistances": res}


def buy_sell_zones(trend, sr, zone_pct=0.15):
    zones = []

    def band(level):
        d = level * zone_pct / 100
        return round(level - d, 2), round(level + d, 2)

    if sr["supports"]:
        s = sr["supports"][0]["price"]
        lo, hi = band(s)
        if trend["label"] == "up":
            conf, basis = "high", "Buy-the-dip into support, trend up"
        elif trend["label"] == "sideways":
            conf, basis = "medium", "Range buy near support"
        else:
            conf, basis = "low", "Counter-trend buy (risky in downtrend)"
        zones.append({"side": "buy", "low": lo, "high": hi,
                      "basis": basis, "conf": conf})

    if sr["resistances"]:
        r = sr["resistances"][0]["price"]
        lo, hi = band(r)
        if trend["label"] == "down":
            conf, basis = "high", "Sell-the-rally into resistance, trend down"
        elif trend["label"] == "sideways":
            conf, basis = "medium", "Range sell near resistance"
        else:
            conf, basis = "low", "Counter-trend sell (risky in uptrend)"
        zones.append({"side": "sell", "low": lo, "high": hi,
                      "basis": basis, "conf": conf})
    return zones


# ============================================================
# PAGE + LIGHT THEME
# ============================================================
st.set_page_config(page_title="Market Structure", layout="wide",
                   initial_sidebar_state="expanded")

st.markdown("""
<style>
    .stApp { background:#fafbfc; }
    .block-container { padding-top:1.4rem; padding-bottom:2rem; }
    h1,h2,h3 { color:#1a1d23; font-weight:600; }
    .card { background:#fff; border:1px solid #eceef1; border-radius:14px;
            padding:16px 18px; box-shadow:0 1px 3px rgba(16,24,40,.04); }
    .badge { display:inline-block; padding:6px 14px; border-radius:999px;
             font-weight:600; font-size:14px; }
    .up{background:#e7f7ee;color:#067647;} .down{background:#fdeceb;color:#b42318;}
    .side{background:#fef6e7;color:#b54708;}
    .lvl{font-variant-numeric:tabular-nums;}
    .zone-buy{border-left:4px solid #12b76a;padding:10px 14px;background:#f6fef9;
              border-radius:8px;margin-bottom:8px;}
    .zone-sell{border-left:4px solid #f04438;padding:10px 14px;background:#fffbfa;
               border-radius:8px;margin-bottom:8px;}
    .muted{color:#667085;font-size:13px;}
</style>
""", unsafe_allow_html=True)


# ============================================================
# SIDEBAR CONTROLS
# ============================================================
st.sidebar.title("⚙️ Controls")
symbol = st.sidebar.text_input("Symbol", DEFAULT_SYMBOL).strip().upper()
days = st.sidebar.slider("Days of data to download", 2, 30, 10)
trend_hours = st.sidebar.slider("Hours to judge trend", 1, 12, 4)
sr_hours = st.sidebar.slider("Hours of structure (S/R)", 4, 72, 20)

# ----- Heikin-Ashi trendline controls -----
st.sidebar.markdown("### 📈 HA Trendline")
show_trendline = st.sidebar.checkbox("Show HA fractal trendline", True)
trend_band = st.sidebar.slider("Band width (± price)", 0, 500, 50, step=10)

# ----- LIVE auto-refresh -----
st.sidebar.markdown("### 🔴 Live")
interval_label = st.sidebar.selectbox(
    "Auto-refresh every",
    ["Off", "1 min", "2 min", "5 min", "10 min", "15 min"],
    index=3,   # default 5 min
)
_interval_ms = {"Off": 0, "1 min": 60_000, "2 min": 120_000,
                "5 min": 300_000, "10 min": 600_000,
                "15 min": 900_000}[interval_label]

refresh_count = 0
if _interval_ms > 0:
    if _HAS_AUTOREFRESH:
        refresh_count = st_autorefresh(interval=_interval_ms, key="live_refresh")
    else:
        # Fallback: meta refresh tag (whole-page reload) if component missing
        st.markdown(
            f'<meta http-equiv="refresh" content="{_interval_ms // 1000}">',
            unsafe_allow_html=True,
        )
    st.sidebar.success(f"Live · refreshing every {interval_label}")
else:
    st.sidebar.info("Auto-refresh off")

if st.sidebar.button("🔄 Refresh now"):
    st.cache_data.clear()
    st.rerun()

st.sidebar.caption(f"Auto-downloads {RESOLUTION} candles from Delta public API.")

trend_bars = max(5, int(trend_hours * 60 / BAR_MINUTES))
sr_bars = max(20, int(sr_hours * 60 / BAR_MINUTES))

# ============================================================
# DOWNLOAD + ANALYSE
# ============================================================
st.title("📊 Market Structure Dashboard")

try:
    with st.spinner(f"Downloading {symbol} {RESOLUTION} candles…"):
        df = download_candles(symbol, days, _bust=refresh_count)
except Exception as e:
    st.error(f"Could not download data for {symbol}: {e}")
    st.caption("Check the symbol (e.g. BTCUSD, ETHUSD) and your internet. "
               "Delta India endpoint must be reachable from this machine.")
    st.stop()

trend = analyse_trend(df, lookback_bars=trend_bars)
sr = support_resistance(df, lookback_bars=sr_bars)
zones = buy_sell_zones(trend, sr)

# Heikin-Ashi fractal trendline (computed on full df, positional index)
ha = calculate_trendline(df, band=float(trend_band))

price = float(df["Close"].iloc[-1])
last_time = df.index[-1]

_now = dt.datetime.now().strftime("%H:%M:%S")
_live = f"🔴 LIVE · updates every {interval_label}" if _interval_ms > 0 else "⏸ paused"
st.caption(f"As of {last_time:%Y-%m-%d %H:%M}  ·  {symbol}  ·  {RESOLUTION}  ·  "
           f"{len(df)} candles  ·  trend {trend_hours}h, S/R {sr_hours}h  ·  "
           f"{_live} · page updated {_now}")

# ----- metric row -----
badge_cls = {"up": "up", "down": "down", "sideways": "side"}[trend["label"]]
badge_txt = {"up": "▲ UPTREND", "down": "▼ DOWNTREND",
             "sideways": "■ SIDEWAYS"}[trend["label"]]

ha_last = float(ha["Trendline"].iloc[-1]) if len(ha) and not np.isnan(ha["Trendline"].iloc[-1]) else np.nan
ha_pos = "above" if not np.isnan(ha_last) and price > ha_last else "below"

c1, c2, c3, c4 = st.columns(4)
c1.markdown(f'<div class="card"><div class="muted">Price</div>'
            f'<h2 class="lvl">{price:,.2f}</h2>'
            f'<div class="muted">{last_time:%H:%M %d-%b}</div></div>',
            unsafe_allow_html=True)
c2.markdown(f'<div class="card"><div class="muted">Trend ({trend_hours}h)</div>'
            f'<br><span class="badge {badge_cls}">{badge_txt}</span></div>',
            unsafe_allow_html=True)
c3.markdown(f'<div class="card"><div class="muted">Move over window</div>'
            f'<h2 class="lvl">{trend["slope_pct"]:+.2f}%</h2></div>',
            unsafe_allow_html=True)
if np.isnan(ha_last):
    c4.markdown('<div class="card"><div class="muted">HA Trendline</div>'
                '<h2 class="lvl">—</h2></div>', unsafe_allow_html=True)
else:
    c4.markdown(f'<div class="card"><div class="muted">HA Trendline</div>'
                f'<h2 class="lvl">{ha_last:,.2f}</h2>'
                f'<div class="muted">price is {ha_pos}</div></div>',
                unsafe_allow_html=True)
st.write("")

# ----- chart -----
chart_bars = min(300, len(df))
plot_df = df.tail(chart_bars)
# Align HA trendline (positional) to the same tail window
ha_plot = ha.tail(chart_bars).copy()
ha_plot.index = plot_df.index   # map positional HA rows back to datetimes

fig = make_subplots(rows=2, cols=1, shared_xaxes=True,
                    row_heights=[0.78, 0.22], vertical_spacing=0.03)
fig.add_trace(go.Candlestick(
    x=plot_df.index, open=plot_df["Open"], high=plot_df["High"],
    low=plot_df["Low"], close=plot_df["Close"],
    increasing_line_color="#12b76a", decreasing_line_color="#f04438",
    increasing_fillcolor="#12b76a", decreasing_fillcolor="#f04438",
    name="Price"),
    row=1, col=1)

# ----- Heikin-Ashi fractal trendline + bands -----
if show_trendline and "Trendline" in ha_plot:
    # Upper/lower band as a shaded channel
    if trend_band > 0:
        fig.add_trace(go.Scatter(
            x=ha_plot.index, y=ha_plot["up_Trendline"],
            line=dict(width=0), mode="lines",
            hoverinfo="skip", showlegend=False, name="up band"),
            row=1, col=1)
        fig.add_trace(go.Scatter(
            x=ha_plot.index, y=ha_plot["down_Trendline"],
            line=dict(width=0), mode="lines", fill="tonexty",
            fillcolor="rgba(99,102,241,0.10)",
            hoverinfo="skip", showlegend=False, name="down band"),
            row=1, col=1)
    # Main trendline (step-like, since it jumps at breaks)
    fig.add_trace(go.Scatter(
        x=ha_plot.index, y=ha_plot["Trendline"],
        mode="lines", line=dict(color="#6366f1", width=2, shape="hv"),
        name="HA Trendline"),
        row=1, col=1)

for s in sr["supports"]:
    fig.add_hline(y=s["price"], line=dict(color="#12b76a", width=1, dash="dot"),
                  row=1, col=1, annotation_text=f"S {s['price']:,.0f}",
                  annotation_position="right",
                  annotation_font_color="#067647", annotation_font_size=11)
for r in sr["resistances"]:
    fig.add_hline(y=r["price"], line=dict(color="#f04438", width=1, dash="dot"),
                  row=1, col=1, annotation_text=f"R {r['price']:,.0f}",
                  annotation_position="right",
                  annotation_font_color="#b42318", annotation_font_size=11)
for z in zones:
    color = "rgba(18,183,106,0.12)" if z["side"] == "buy" else "rgba(240,68,56,0.12)"
    fig.add_hrect(y0=z["low"], y1=z["high"], line_width=0, fillcolor=color,
                  row=1, col=1)

if "Volume" in plot_df:
    fig.add_trace(go.Bar(x=plot_df.index, y=plot_df["Volume"],
                         marker_color="#cdd5df", showlegend=False), row=2, col=1)

fig.update_layout(height=600, margin=dict(l=10, r=70, t=10, b=10),
                  paper_bgcolor="#fafbfc", plot_bgcolor="#ffffff",
                  font=dict(color="#1a1d23", size=12),
                  xaxis_rangeslider_visible=False,
                  showlegend=show_trendline,
                  legend=dict(orientation="h", yanchor="bottom", y=1.0,
                              xanchor="left", x=0),
                  hovermode="x unified")
fig.update_xaxes(gridcolor="#f0f1f3")
fig.update_yaxes(gridcolor="#f0f1f3")
st.plotly_chart(fig, use_container_width=True)

# ----- levels + zones -----
left, right = st.columns(2)
with left:
    st.subheader("Support & Resistance")
    if sr["resistances"]:
        st.markdown("**Resistance (above)**")
        for r in sr["resistances"]:
            st.markdown(f'<div class="lvl">🔴 {r["price"]:,.2f} '
                        f'<span class="muted">· {r["touches"]} touches</span></div>',
                        unsafe_allow_html=True)
    if sr["supports"]:
        st.markdown("**Support (below)**")
        for s in sr["supports"]:
            st.markdown(f'<div class="lvl">🟢 {s["price"]:,.2f} '
                        f'<span class="muted">· {s["touches"]} touches</span></div>',
                        unsafe_allow_html=True)
    if not sr["supports"] and not sr["resistances"]:
        st.caption("No clear levels in this window.")

with right:
    st.subheader("Suggested Zones")
    if not zones:
        st.caption("No actionable zone right now.")
    for z in zones:
        cls = "zone-buy" if z["side"] == "buy" else "zone-sell"
        emoji = "🟢 BUY" if z["side"] == "buy" else "🔴 SELL"
        st.markdown(f'<div class="{cls}"><b>{emoji} zone</b> '
                    f'<span class="lvl">{z["low"]:,.2f} – {z["high"]:,.2f}</span><br>'
                    f'<span class="muted">{z["basis"]} · confidence: {z["conf"]}</span>'
                    f'</div>', unsafe_allow_html=True)
    st.caption("Structural references, not trade advice. Confirm with your own risk rules.")

with st.expander("Why this trend label?"):
    st.write(trend["note"])
    st.write(f"Fast EMA {trend['ema_fast']:,.2f} · Slow EMA {trend['ema_slow']:,.2f} "
             f"· bars {trend['bars']}")
    if not np.isnan(ha_last):
        st.write(f"HA fractal trendline: {ha_last:,.2f} "
                 f"(band ±{trend_band}) · price currently {ha_pos} the line.")