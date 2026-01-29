import os
import requests
import pandas as pd
import numpy as np
import pandas_ta as ta
from scipy.signal import argrelextrema

# ================= FIXED IST OFFSET =================
from datetime import datetime, timedelta
IST_OFFSET = timedelta(hours=5, minutes=30)

def convert_to_ist(timestamps):
    """Convert Unix timestamps (seconds) to IST datetime"""
    return pd.to_datetime(timestamps, unit='s') + IST_OFFSET

# ================= HISTORICAL DATA =================
def get_stock_historical_data(data, fyers=None):
    """
    data: dict with key 'candles' or fyers data
    fyers: optional (None if using REST API)
    """
    final_data = data
    if fyers is not None:
        final_data = fyers.history(data=data)
    
    df = pd.DataFrame(final_data['candles'])
    df = df.rename(columns={0: 'Date', 1: 'Open',2: 'High',3: 'Low',4: 'Close',5: 'V'})
    
    df['Date'] = convert_to_ist(df['Date'])
    df.set_index("Date", inplace=True)
    
    return df

# ================= STOCK TOKENS =================
def get_stock_instrument_token(stock_list, fyers):
    tokens=[]
    for i in range(len(stock_list)):
        df = pd.read_csv("https://api.kite.trade/instruments")
        item = stock_list[i]

        if item['segment'] in ['MCX-FUT','NFO-FUT']:
            df = df[(df['name']==item['name']) & (df['segment']==item['segment'])]
            df = df[df["expiry"] == sorted(list(df["expiry"].unique()))[item['expiry']]]
            tokens.append({
                'strategy_name': item['strategy_name'],
                'instrument_token': df['instrument_token'].item(),
                'tradingsymbol': df['tradingsymbol'].item()
            })

        elif item['segment'] in ['MCX-OPT','NFO-OPT']:
            spot_price = fyers.quotes(data={"symbols":"NSE:NIFTYBANK-INDEX"})['d'][0]['v']['lp']
            df = df[(df['name']==item['name']) & (df['segment']==item['segment'])]
            df = df[df["expiry"] == sorted(list(df["expiry"].unique()))[item['expiry']]]
            atm_strike = 100*round(spot_price/100)
            atm_strike_CE = atm_strike - 100*item['offset']
            atm_strike_PE = atm_strike + 100*item['offset']

            CE_token = df[(df['strike']==atm_strike_CE) & (df['instrument_type']=='CE')]['instrument_token'].item()
            PE_token = df[(df['strike']==atm_strike_PE) & (df['instrument_type']=='PE')]['instrument_token'].item()
            CE_name = df[(df['strike']==atm_strike_CE) & (df['instrument_type']=='CE')]['tradingsymbol'].item()
            PE_name = df[(df['strike']==atm_strike_PE) & (df['instrument_type']=='PE')]['tradingsymbol'].item()

            tokens.append({'strategy_name': item['strategy_name'], 'instrument_token': CE_token, 'tradingsymbol': CE_name})
            tokens.append({'strategy_name': item['strategy_name'], 'instrument_token': PE_token, 'tradingsymbol': PE_name})
    return tokens

# ================= TREND & TRADE SIGNALS =================
def high_low_trend(data, fyers=None):
    df = get_stock_historical_data(data, fyers)
    df = df.reset_index(drop=True)

    ha = pd.DataFrame(index=df.index)
    ha["HA_close"] = (df["Open"] + df["High"] + df["Low"] + df["Close"])/4
    ha["HA_open"] = np.nan
    ha.loc[0, "HA_open"] = (df.loc[0, "Open"] + df.loc[0, "Close"])/2

    for i in range(1, len(df)):
        ha.loc[i, "HA_open"] = 0.5*(ha.loc[i-1,"HA_open"] + ha.loc[i-1,"HA_close"])

    ha["HA_high"] = pd.concat([df["High"], ha["HA_open"], ha["HA_close"]], axis=1).max(axis=1)
    ha["HA_low"] = pd.concat([df["Low"], ha["HA_open"], ha["HA_close"]], axis=1).min(axis=1)

    # Smooth HA
    ha["high_smooth"] = ta.ema(ha["HA_high"], length=5)
    ha["low_smooth"] = ta.ema(ha["HA_low"], length=5)

    # Swing High / Low
    max_idx = argrelextrema(ha["high_smooth"].values, np.greater_equal, order=21)[0]
    min_idx = argrelextrema(ha["low_smooth"].values, np.less_equal, order=21)[0]
    ha["max_high"] = np.nan; ha["max_low"] = np.nan
    ha.loc[max_idx,"max_high"] = ha.loc[max_idx,"HA_high"]
    ha.loc[min_idx,"max_low"] = ha.loc[min_idx,"HA_low"]
    ha["max_high"].ffill(inplace=True); ha["max_low"].ffill(inplace=True)

    # Trendline
    ha["trendline"] = np.nan
    trendline = ha.loc[0, "HA_close"]
    ha.loc[0, "trendline"] = trendline
    for i in range(1,len(ha)):
        if ha.loc[i,"HA_high"]==ha.loc[i,"max_high"]:
            trendline = ha.loc[i,"HA_low"]
        elif ha.loc[i,"HA_low"]==ha.loc[i,"max_low"]:
            trendline = ha.loc[i,"HA_high"]
        ha.loc[i,"trendline"]=trendline

    # ATR
    ha["ATR"] = ta.atr(high=ha["HA_high"], low=ha["HA_low"], close=ha["HA_close"], length=14)
    ha["ATR_EMA"] = ta.ema(ha["ATR"], length=14)
    ha["atr_condition"] = ha["ATR"] > ha["ATR_EMA"]

    # Trade signals
    ha["trade_single"] = 0
    for i in range(1,len(ha)):
        if not ha.loc[i,"atr_condition"]: continue
        if ha.loc[i,"HA_close"]>ha.loc[i,"trendline"] and ha.loc[i,"HA_close"]>ha.loc[i-1,"HA_open"] and ha.loc[i,"HA_close"]>ha.loc[i-1,"HA_close"]:
            ha.loc[i,"trade_single"]=1
        elif ha.loc[i,"HA_close"]<ha.loc[i,"trendline"] and ha.loc[i,"HA_close"]<ha.loc[i-1,"HA_open"] and ha.loc[i,"HA_close"]<ha.loc[i-1,"HA_close"]:
            ha.loc[i,"trade_single"]=-1

    return ha
