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
    df = df.reset_index(drop=True)

    # ------------------------------------------------------------------
    # Manual Heikin Ashi (NO pandas-ta ha → no warnings)
    # ------------------------------------------------------------------
    ha = pd.DataFrame(index=df.index)

    ha["HA_close"] = (
        df["Open"] + df["High"] + df["Low"] + df["Close"]
    ) / 4

    ha["HA_open"] = np.nan
    ha.loc[0, "HA_open"] = (df.loc[0, "Open"] + df.loc[0, "Close"]) / 2

    for i in range(1, len(df)):
        ha.loc[i, "HA_open"] = 0.5 * (
            ha.loc[i - 1, "HA_open"] +
            ha.loc[i - 1, "HA_close"]
        )

    ha["HA_high"] = pd.concat(
        [df["High"], ha["HA_open"], ha["HA_close"]],
        axis=1
    ).max(axis=1)

    ha["HA_low"] = pd.concat(
        [df["Low"], ha["HA_open"], ha["HA_close"]],
        axis=1
    ).min(axis=1)

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
        order=21
    )[0]

    ha["max_high"] = np.nan
    ha["max_low"] = np.nan

    ha.loc[max_idx, "max_high"] = ha.loc[max_idx, "HA_high"]
    ha.loc[min_idx, "max_low"] = ha.loc[min_idx, "HA_low"]

    ha["max_high"] = ha["max_high"].ffill()
    ha["max_low"] = ha["max_low"].ffill()

    # ------------------------------------------------------------------
    # Trendline construction
    # ------------------------------------------------------------------
    ha["trendline"] = np.nan
    trendline = ha.loc[0, "HA_close"]
    ha.loc[0, "trendline"] = trendline

    for i in range(1, len(ha)):
        if ha.loc[i, "HA_high"] == ha.loc[i, "max_high"]:
            trendline = ha.loc[i, "HA_low"]
        elif ha.loc[i, "HA_low"] == ha.loc[i, "max_low"]:
            trendline = ha.loc[i, "HA_high"]

        ha.loc[i, "trendline"] = trendline

    # ------------------------------------------------------------------
    # ATR & ATR EMA
    # ------------------------------------------------------------------
    ha["ATR"] = ta.atr(
        high=ha["HA_high"],
        low=ha["HA_low"],
        close=ha["HA_close"],
        length=14
    )

    ha["ATR_EMA"] = ta.ema(ha["ATR"], length=14)

    ha["atr_condition"] = ha["ATR"] > ha["ATR_EMA"]

    # ------------------------------------------------------------------
    # Trade signals
    # ------------------------------------------------------------------
    ha["trade_single"] = 0

    for i in range(1, len(ha)):

        if not ha.loc[i, "atr_condition"]:
            continue

        # BUY
        if (
            ha.loc[i, "HA_close"] > ha.loc[i, "trendline"] and
            ha.loc[i, "HA_close"] > ha.loc[i - 1, "HA_open"] and
            ha.loc[i, "HA_close"] > ha.loc[i - 1, "HA_close"]
        ):
            ha.loc[i, "trade_single"] = 1

        # SELL
        elif (
            ha.loc[i, "HA_close"] < ha.loc[i, "trendline"] and
            ha.loc[i, "HA_close"] < ha.loc[i - 1, "HA_open"] and
            ha.loc[i, "HA_close"] < ha.loc[i - 1, "HA_close"]
        ):
            ha.loc[i, "trade_single"] = -1

    return ha