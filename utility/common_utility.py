import os
import sys
mydir = os.getcwd() # directory that your python file
sys.path.append(mydir)
import requests
import pandas as pd
import numpy as np
import pandas_ta as ta
from scipy.signal import argrelextrema
from utility.kite_trade import *

def get_stock_historical_data(data, fyers): 
    final_data = fyers.history(data=data)
    df = pd.DataFrame(final_data['candles'])  
    df = df.rename(columns={0: 'Date', 1: 'Open',2: 'High',3: 'Low',4: 'Close',5: 'V'})
    df['Date'] =  pd.to_datetime(df['Date'], unit='s')
    df['Date'] =  df['Date'].dt.tz_localize('utc').dt.tz_convert('asia/Kolkata')
    df['Date'] =  df['Date'].dt.tz_localize(None)
    df.set_index("Date", inplace = True)
   
    return df

# get stock token

def get_stock_instrument_token(stock_name, fyers):
    tokens=[]
    for i in range(len(stock_name)):
        print(stock_name[i]['name']) 
        df = pd.read_csv("https://api.kite.trade/instruments")
        if stock_name[i]['segment'] == 'MCX-FUT' or  stock_name[i]['segment'] == 'NFO-FUT':
            df = df[(df['name'] == stock_name[i]['name']) & (df['segment'] == stock_name[i]['segment'])]
            df = df[df["expiry"] == sorted(list(df["expiry"].unique()))[stock_name[i]['expiry']]]
            df_instrument_token= df['instrument_token'].item()
            df_instrument_name= df['tradingsymbol'].item()
            tokens.append({'strategy_name' : stock_name[i]['strategy_name'], 'instrument_token':df_instrument_token, 'tradingsymbol':df_instrument_name})
        elif stock_name[i]['segment'] == 'MCX-OPT' or  stock_name[i]['segment'] == 'NFO-OPT':
            spot_price = fyers.quotes(data={"symbols":"NSE:NIFTYBANK-INDEX"})['d'][0]['v']['lp']
            df = df[(df['name'] == stock_name[i]['name']) & (df['segment'] == stock_name[i]['segment'])]
            df = df[df["expiry"] == sorted(list(df["expiry"].unique()))[stock_name[i]['expiry']]]     
            atm_strike = 100*round(spot_price/100)          
            atm_strike_CE = atm_strike - 100*stock_name[i]['offset']
            atm_strike_PE = atm_strike + 100*stock_name[i]['offset']            
                        
            option_strike_CE = df[(df['strike'] == atm_strike_CE) & (df['instrument_type'] == 'CE')]['instrument_token'].item()
            option_strike_PE = df[(df['strike'] == atm_strike_PE) & (df['instrument_type'] == 'PE')]['instrument_token'].item()
          
            option_strike_name_CE = df[(df['strike'] == atm_strike_CE) & (df['instrument_type'] == 'CE')]['tradingsymbol'].item()
            option_strike_name_PE = df[(df['strike'] == atm_strike_PE) & (df['instrument_type'] == 'PE')]['tradingsymbol'].item()
            # print(option_strike_name_CE)
            # print(option_strike_name_PE)
            tokens.append({'strategy_name' : stock_name[i]['strategy_name'], 'instrument_token':option_strike_CE, 'tradingsymbol':option_strike_name_CE})
            tokens.append({'strategy_name' : stock_name[i]['strategy_name'], 'instrument_token':option_strike_PE, 'tradingsymbol':option_strike_name_PE})
    return tokens


def high_low_trend(data, fyers):

    # ------------------------------------------------------------------
    # Fetch & prepare data
    # ------------------------------------------------------------------
    df = pd.DataFrame(get_stock_historical_data(data, fyers))

    # ------------------------------------------------------------------
    # Heikin Ashi candles
    # ------------------------------------------------------------------
    ha = ta.ha(
        open_=df["Open"],
        high=df["High"],
        low=df["Low"],
        close=df["Close"]
    ).reset_index(drop=True)

    # ------------------------------------------------------------------
    # Smooth HA highs & lows
    # ------------------------------------------------------------------
    ha["high_smooth"] = ta.ema(ha["HA_high"], length=5)
    ha["low_smooth"] = ta.ema(ha["HA_low"], length=5)

    # ------------------------------------------------------------------
    # Swing Highs / Lows
    # ------------------------------------------------------------------
    max_idx = argrelextrema(
        ha["high_smooth"].values,
        np.greater_equal,
        order=21
    )[0]

    min_idx = argrelextrema(
        ha["low_smooth"].values,
        np.less_equal,
        order=42
    )[0]

    ha["max_high"] = np.nan
    ha["max_low"] = np.nan

    ha.loc[max_idx, "max_high"] = ha.loc[max_idx, "high_smooth"]
    ha.loc[min_idx, "max_low"] = ha.loc[min_idx, "low_smooth"]

    ha["max_high"] = ha["max_high"].ffill()
    ha["max_low"] = ha["max_low"].ffill()

    # ------------------------------------------------------------------
    # Trendline construction
    # ------------------------------------------------------------------
    ha["Trendline"] = np.nan
    current_trend = ha.loc[0, "HA_high"]
    ha["trade_single"] = 0 

    for i in range(len(ha)):
        if ha.loc[i, "HA_high"] == ha.loc[i, "max_high"]:
            current_trend = ha.loc[i, "max_high"]
        elif ha.loc[i, "HA_low"] == ha.loc[i, "max_low"]:
            current_trend = ha.loc[i, "max_low"]

        ha.loc[i, "Trendline"] = current_trend

    # ------------------------------------------------------------------
    # ATR & ATR EMA
    # ------------------------------------------------------------------
    ha["ATR"] = ta.atr(
        high=df["High"],
        low=df["Low"],
        close=df["Close"],
        length=14
    )

    ha["ATR_EMA"] = ta.ema(ha["ATR"], length=14)

    # ATR condition
    ha["atr_condition"] = ha["ATR"] > ha["ATR_EMA"]

    # ------------------------------------------------------------------
    # Trade signals
    # ------------------------------------------------------------------
    ha["trade_signal"] = 0

    for i in range(1, len(ha)):

        if not ha.loc[i, "atr_condition"]:
            continue

        # BUY
        if (
            ha.loc[i, "HA_close"] > ha.loc[i, "Trendline"] and
            ha.loc[i, "HA_close"] > ha.loc[i - 1, "HA_open"] and
            ha.loc[i, "HA_close"] > ha.loc[i - 1, "HA_close"]
        ):
            ha.loc[i, "trade_single"] = 1

        # SELL
        elif (
            ha.loc[i, "HA_close"] < ha.loc[i, "Trendline"] and
            ha.loc[i, "HA_close"] < ha.loc[i - 1, "HA_open"] and
            ha.loc[i, "HA_close"] < ha.loc[i - 1, "HA_close"]
        ):
            ha.loc[i, "trade_single"] = -1

    return ha






