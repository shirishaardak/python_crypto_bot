import streamlit as st
from streamlit_autorefresh import st_autorefresh
import pandas as pd
import os
import plotly.graph_objects as go

# ================== CONFIG ==================
DATA_DIR = os.path.join(os.getcwd(), "data")

# Detect all strategies dynamically
STRATEGIES = {
    name: os.path.join(DATA_DIR, name)
    for name in os.listdir(DATA_DIR)
    if os.path.isdir(os.path.join(DATA_DIR, name))
}

st.set_page_config(page_title="Unified Trading Dashboard", layout="wide")
st_autorefresh(interval=30 * 1000, key="datarefresh")

st.title("📊 Unified Trading Dashboard")

# ================== STRATEGY SELECTION ==================
selected_strategy = st.sidebar.selectbox("Select Strategy", options=list(STRATEGIES.keys()))
SAVE_DIR = STRATEGIES[selected_strategy]

# Load processed data files
data_files = [f for f in os.listdir(SAVE_DIR) if f.endswith("_processed.csv")]

# Locate live_trades.csv
local_trades_file = os.path.join(SAVE_DIR, "live_trades.csv")
global_trades_file = os.path.join(DATA_DIR, "live_trades.csv")

if os.path.exists(local_trades_file):
    trades_file = local_trades_file
elif os.path.exists(global_trades_file):
    trades_file = global_trades_file
else:
    trades_file = None

# ================== LOAD TRADES ==================
trades_df = pd.DataFrame()
if trades_file:
    try:
        trades_df = pd.read_csv(trades_file, parse_dates=["entry_time", "exit_time"])
        trades_df["symbol_clean"] = trades_df["symbol"].str.replace(r"[^A-Z]", "", regex=True).str.upper()
    except Exception as e:
        st.error(f"Error reading live_trades.csv: {e}")

# Discover symbols
symbols_from_files = [f.split("_")[0] for f in data_files]
symbols_from_trades = trades_df["symbol_clean"].unique().tolist() if not trades_df.empty else []
all_symbols = sorted(set(symbols_from_files + symbols_from_trades))

if not all_symbols:
    st.warning("No symbols found in data or live_trades.csv.")
    st.stop()

# ================== SIDEBAR SETTINGS ==================
st.sidebar.header("Settings")
symbols_to_view = st.sidebar.multiselect(
    "Select Symbols",
    options=all_symbols,
    default=all_symbols
)

# ================== DASHBOARD ==================
for symbol in symbols_to_view:
    st.header(f"💹 {symbol} - {selected_strategy}")

    # ================== PRICE CHART ==================
    symbol_files = [f for f in data_files if f.startswith(symbol)]
    if symbol_files:
        file_path = os.path.join(SAVE_DIR, symbol_files[0])
        df = pd.read_csv(file_path, index_col="time", parse_dates=["time"])

        st.subheader("Latest Data (Last 10 Rows)")
        st.dataframe(df.tail(10))

        # ---------- INDICATOR SELECTION ----------
        excluded_cols = [
            "open", "high", "low", "close",
            "HA_open", "HA_high", "HA_low", "HA_close"
        ]

        available_indicators = [
            col for col in df.select_dtypes(include=["float", "int"]).columns
            if col not in excluded_cols
        ]

        # 👉 DEFAULT = TRENDLINE IF PRESENT
        default_inds = ["trendline"] if "trendline" in available_indicators else []

        st.sidebar.subheader(f"Select Indicators for {symbol}")
        selected_indicators = st.sidebar.multiselect(
            f"Indicators for {symbol}",
            options=available_indicators,
            default=default_inds
        )

        # ================== CANDLESTICK CHART ==================
        st.subheader("📈 Candlestick Chart (Last 50 Data Points)")

        # Keep original column names safe
        df_plot = df.tail(50).copy()

        fig = go.Figure()

        # ======================================================
        # 👉 AUTO DETECT CANDLE TYPE
        # ======================================================

        has_ha = all(col in df_plot.columns for col in
                     ["HA_open", "HA_high", "HA_low", "HA_close"])

        has_normal = all(col in df_plot.columns for col in
                         ["open", "high", "low", "close"])

        # ---- Show Heiken Ashi if available ----
        if has_ha:
            fig.add_trace(go.Candlestick(
                x=df_plot.index,
                open=df_plot["HA_open"],
                high=df_plot["HA_high"],
                low=df_plot["HA_low"],
                close=df_plot["HA_close"],
                name="Heiken Ashi",
                increasing_line_color='green',
                decreasing_line_color='red'
            ))

        # ---- Otherwise show normal candles ----
        elif has_normal:
            fig.add_trace(go.Candlestick(
                x=df_plot.index,
                open=df_plot["open"],
                high=df_plot["high"],
                low=df_plot["low"],
                close=df_plot["close"],
                name="Candlestick",
                increasing_line_color='green',
                decreasing_line_color='red'
            ))

        else:
            st.warning("No valid OHLC or Heiken Ashi columns found.")

        # ---- Add Selected Indicators ----
        for col in selected_indicators:
            if col in df_plot.columns:
                fig.add_trace(go.Scatter(
                    x=df_plot.index,
                    y=df_plot[col].ffill(),
                    mode="lines",
                    name=col,
                    line=dict(width=1.5)
                ))

        fig.update_layout(
            title=f"{symbol} - Last 50 Candles",
            xaxis_title="Time",
            yaxis_title="Price",
            xaxis_rangeslider_visible=False,
            template="plotly_dark",
            legend=dict(
                orientation="h",
                yanchor="bottom",
                y=-0.3,
                xanchor="center",
                x=0.5
            )
        )

        st.plotly_chart(fig, use_container_width=True)

    else:
        st.info(f"No processed data found for {symbol}. Showing trades only below.")

    # ================== TRADES & PNL ==================
    st.subheader("Executed Trades & PNL")

    if not trades_df.empty:
        symbol_clean = "".join(filter(str.isalpha, symbol)).upper()

        trades_df_symbol = trades_df[
            (trades_df["symbol_clean"] == symbol_clean) |
            (trades_df["symbol_clean"].str.contains(symbol_clean, na=False))
        ].copy()

        if not trades_df_symbol.empty:
            trades_df_symbol.sort_values(by="exit_time", inplace=True)
            trades_df_symbol["cumulative_pnl"] = trades_df_symbol["net_pnl"].cumsum()

            st.dataframe(trades_df_symbol.tail(10))

            fig2 = go.Figure()
            fig2.add_trace(go.Scatter(
                x=trades_df_symbol["exit_time"],
                y=trades_df_symbol["cumulative_pnl"],
                mode="lines+markers",
                name="Cumulative PNL",
                line=dict(color="purple", width=2)
            ))

            fig2.update_layout(
                title=f"{symbol} - Cumulative PNL",
                xaxis_title="Time",
                yaxis_title="Cumulative PNL",
                template="plotly_dark"
            )

            st.plotly_chart(fig2, use_container_width=True)

        else:
            st.warning(f"No trades found for {symbol} in live_trades.csv.")
    else:
        st.warning("No live_trades.csv found or empty.")
