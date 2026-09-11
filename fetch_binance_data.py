import requests
import pandas as pd
import time
import os
from datetime import datetime, timedelta

def fetch_binance_futures_klines(symbol, interval, start_time_ms, end_time_ms, limit=1500):
    """
    Fetches historical klines from Binance USDM Futures API.
    """
    url = "https://fapi.binance.com/fapi/v1/klines"
    all_klines = []
    current_start = start_time_ms
    
    print(f"Fetching {symbol} {interval} data...")
    
    while current_start < end_time_ms:
        params = {
            "symbol": symbol,
            "interval": interval,
            "startTime": current_start,
            "endTime": end_time_ms,
            "limit": limit
        }
        
        try:
            response = requests.get(url, params=params)
            response.raise_for_status()
            data = response.json()
            
            if not data:
                break
                
            all_klines.extend(data)
            
            # Update current_start to the open time of the last kline + 1ms to avoid duplicates
            current_start = data[-1][0] + 1
            
            print(f"Fetched {len(data)} rows. Last timestamp: {pd.to_datetime(data[-1][0], unit='ms')}")
            time.sleep(0.5)  # Avoid rate limits
            
        except Exception as e:
            print(f"Error fetching data: {e}")
            break
            
    # Convert to DataFrame
    columns = [
        "open_time", "open", "high", "low", "close", "volume", 
        "close_time", "quote_asset_volume", "number_of_trades", 
        "taker_buy_base_asset_volume", "taker_buy_quote_asset_volume", "ignore"
    ]
    df = pd.DataFrame(all_klines, columns=columns)
    
    # Clean up types
    numeric_cols = ["open", "high", "low", "close", "volume", "quote_asset_volume", "taker_buy_base_asset_volume", "taker_buy_quote_asset_volume"]
    df[numeric_cols] = df[numeric_cols].apply(pd.to_numeric, axis=1)
    
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
    df["close_time"] = pd.to_datetime(df["close_time"], unit="ms")
    
    df.set_index("open_time", inplace=True)
    df.drop(columns=["ignore"], inplace=True)
    
    # Remove duplicates if any
    df = df[~df.index.duplicated(keep='first')]
    
    return df

def main():
    from modules.watchlist import get_focused_watchlist
    
    # Define parameters
    # Fetch 20 coins as configured in .env FOCUSED_SIZE
    symbols = get_focused_watchlist(20)
    intervals = ["1h", "15m"] # We need 1h for multi-timeframe strategies
    
    print(f"Fetching data for {len(symbols)} symbols: {symbols}")
    
    # Fetch last 30 days
    end_date = datetime.utcnow()
    start_date = end_date - timedelta(days=30)
    
    start_time_ms = int(start_date.timestamp() * 1000)
    end_time_ms = int(end_date.timestamp() * 1000)
    
    # Create data directory if it doesn't exist
    os.makedirs("data", exist_ok=True)
    
    for symbol in symbols:
        for interval in intervals:
            df = fetch_binance_futures_klines(symbol, interval, start_time_ms, end_time_ms)
            
            if not df.empty:
                filename = f"data/{symbol}_{interval}_30d.csv"
                df.to_csv(filename)
                print(f"Saved {len(df)} rows to {filename}\n")
            else:
                print(f"Failed to fetch data for {symbol} {interval}\n")

if __name__ == "__main__":
    main()
