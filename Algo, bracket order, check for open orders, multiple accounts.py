import sys # Import sys for flushing output
import time # Import time for time.localtime() and time.sleep()
import pandas as pd
import requests
import datetime
from ta.momentum import RSIIndicator as RSI
from ta.trend import ADXIndicator
from delta_rest_client import DeltaRestClient, OrderType # Ensure this is imported



# ADD YOUR MULTIPLE API KEYS AND SECRETS HERE
# Example:
client_credentials = [
    {"api_key": '1nybRkqMUOp5PcUuQFvJptm3jJsZPu', "api_secret": 'zDgaOpt2QDk1HvOxObMKHT46DSOG0RZGQamcNJ0mb62RZx3njAlfjQA3xuob'},
    {"api_key": 'SAeyxviw90fQZaf8z5FLqobdoBx41X', "api_secret": 'AdLiUKLGReg8f7TxaxIY2bahhMMuXMXgSPZUoBBtFsf3I4CtzxDOWJs5zbNL'},
    # Add more accounts as needed
    # {"api_key": '1nybRkqMUOp5PcUuQFvJptm3jJsZPu', "api_secret": 'zDgaOpt2QDk1HvOxObMKHT46DSOG0RZGQamcNJ0mb62RZx3njAlfjQA3xuob'},
]

# ==== Constants ====
RSI_Period = 30
Vol_EMA_Pd = 14
ADX_Len = 14
adx_follow = 20
adx_BT = 45
adx_BTU = 50
Time_period = '5m'
symbol = 'BTCUSD'
order_quantity = 1
TP_RISK_RATIO = 5

# ==== Helper Function for Price Rounding ====
def round_to_tick_size(price, tick_size):
    """
    Rounds a price to the nearest multiple of the exchange's tick size.
    Ensures inputs are floats for division and handles zero tick_size.
    """
    try:
        # Explicitly convert to float to prevent type errors (e.g., if tick_size is a string)
        price_f = float(price)
        tick_size_f = float(tick_size)
    except (ValueError, TypeError) as e:
        print(f"Error converting price ({price}) or tick_size ({tick_size}) to float in round_to_tick_size: {e}")
        sys.stdout.flush()
        # Fallback: if conversion fails, try to return original price as float, or 0.0
        return float(price) if isinstance(price, (int, float)) else 0.0

    if tick_size_f == 0:
        print("Warning: tick_size is zero in round_to_tick_size. Returning original price.")
        sys.stdout.flush()
        return price_f # Return the original price as a float if tick_size is zero
    return round(price_f / tick_size_f) * tick_size_f

# ==== Function to check for open orders and positions ====
def check_for_open_trades(client, symbol):
    """
    Checks if there are any open orders or current positions for the given symbol
    for a specific client account.

    Args:
        client (DeltaRestClient): An initialized DeltaRestClient instance.
        symbol (str): The trading pair symbol.

    Returns:
        bool: True if there are open orders or a non-zero position, False otherwise.
    """
    # Truncate API key for logging to prevent full key exposure in logs
    truncated_api_key = client.api_key[:6] + '...' + client.api_key[-4:]
    try:
        # 1. Check for open orders
        open_orders_response = client.get_live_orders()
        if open_orders_response and isinstance(open_orders_response, list) and len(open_orders_response) > 0:
            print(f"Client {truncated_api_key}: Found {len(open_orders_response)} open order(s). Skipping new order.")
            sys.stdout.flush()
            return True

        # 2. Check for open positions
        product = client.get_product(symbol)
        market_id = product['id']

        position_response = client.get_position(product_id=market_id)

        if position_response and isinstance(position_response, dict) and 'size' in position_response:
            position_size = float(position_response.get('size', 0))
            if abs(position_size) > 0:
                print(f"Client {truncated_api_key}: Found an open position of size {position_size} for {symbol}. Skipping new order.")
                sys.stdout.flush()
                return True

        print(f"Client {truncated_api_key}: No open orders or current positions found. Ready to place new order.")
        sys.stdout.flush()
        return False

    except Exception as e:
        print(f"Client {truncated_api_key}: Error checking for open trades: {e}")
        sys.stdout.flush()
        import traceback
        traceback.print_exc()
        sys.stdout.flush()
        # On error checking, it's safer to assume a problem and prevent new orders for this client.
        return True


# ==== Trade Execution Function ====
def place_order(client, side, symbol, size, signal_candle_data):
    """
    Places a market order with a bracket (Stop Loss and Take Profit) using client.request.

    Args:
        client (DeltaRestClient): An initialized DeltaRestClient instance.
        side (str): The order side ('buy' or 'sell').
        symbol (str): The trading pair symbol.
        size (int): The quantity of the asset to trade.
        signal_candle_data (pd.Series): The pandas Series containing the
                                        OHLCV data for the candle that
                                        generated the signal.
    """
    # Truncate API key for logging
    truncated_api_key = client.api_key[:6] + '...' + client.api_key[-4:]
    try:
        product = client.get_product(symbol)
        market_id = product['id']
        tick_size = product.get('tick_size', 0.01)

        print(f"Client {truncated_api_key}: Preparing bracket order for {symbol} with market ID: {market_id}")
        sys.stdout.flush()

        if side not in ['buy', 'sell']:
            raise ValueError("Invalid side, must be 'buy' or 'sell'")

        entry_price_estimate = float(signal_candle_data['close'])
        stop_loss_price = 0.0
        take_profit_price = 0.0
        risk_points = 0.0

        if side == 'buy':
            stop_loss_price = float(signal_candle_data['low'])
            risk_points = entry_price_estimate - stop_loss_price
            if risk_points <= 0:
                print(f"Client {truncated_api_key}: Warning: Calculated risk for BUY is non-positive. Adjusting SL to be 0.1% below entry.")
                sys.stdout.flush()
                risk_points = entry_price_estimate * 0.001
                stop_loss_price = entry_price_estimate - risk_points
            take_profit_price = entry_price_estimate + (risk_points * TP_RISK_RATIO)

        elif side == 'sell':
            stop_loss_price = float(signal_candle_data['high'])
            risk_points = stop_loss_price - entry_price_estimate
            if risk_points <= 0:
                print(f"Client {truncated_api_key}: Warning: Calculated risk for SELL is non-positive. Adjusting SL to be 0.1% above entry.")
                sys.stdout.flush()
                risk_points = entry_price_estimate * 0.001
                stop_loss_price = entry_price_estimate + risk_points
            take_profit_price = entry_price_estimate - (risk_points * TP_RISK_RATIO)

        print(f"Client {truncated_api_key}: DEBUG: tick_size from API: {tick_size} (type: {type(tick_size)})")
        print(f"Client {truncated_api_key}: DEBUG: stop_loss_price before rounding: {stop_loss_price} (type: {type(stop_loss_price)})")
        print(f"Client {truncated_api_key}: DEBUG: take_profit_price before rounding: {take_profit_price} (type: {type(take_profit_price)})")
        sys.stdout.flush()

        stop_loss_price = round_to_tick_size(stop_loss_price, tick_size)
        take_profit_price = round_to_tick_size(take_profit_price, tick_size)

        print(f"Client {truncated_api_key}: Calculated Entry Estimate: {entry_price_estimate:.2f}, SL: {stop_loss_price:.2f}, TP: {take_profit_price:.2f}")
        print(f"Client {truncated_api_key}: Risk Points: {risk_points:.2f}, Reward Points: {risk_points * TP_RISK_RATIO:.2f}")
        sys.stdout.flush()

        payload = {
            "product_id": market_id,
            "size": size,
            "side": side,
            "order_type": "market_order",
            "post_only": False,
            "bracket_stop_loss_price": stop_loss_price,
            "bracket_take_profit_price": take_profit_price,
            "bracket_stop_loss_limit_price": stop_loss_price,
            "bracket_take_profit_limit_price": take_profit_price,
        }

        response = client.request("POST", "/v2/orders", payload, auth=True)
        order_response_data = response.json()
        print(f"Client {truncated_api_key}: Bracket order placed for {side.upper()}. Response: {order_response_data}")
        sys.stdout.flush()

    except Exception as e:
        print(f"Client {truncated_api_key}: Order failed: {e}")
        sys.stdout.flush()
        import traceback
        traceback.print_exc()
        sys.stdout.flush()

# ==== Main Loop ====
while True:
    t = time.localtime()
    cmin = time.strftime("%M", t)
    csec = time.strftime("%S", t)
    time.sleep(1)

    if int(cmin) % 1 == 0 and int(csec) == 6:
        print(f"\n--- Running trade logic at {time.strftime('%Y-%m-%d %H:%M:%S', t)} ---")
        sys.stdout.flush()

        # No global check here. Each client will be checked individually.

        yesterday = datetime.date.today() - datetime.timedelta(days=1)
        start_date = datetime.datetime.combine(yesterday, datetime.time(0, 0, 0))
        start_timestamp = int(start_date.timestamp())
        end_timestamp = int(datetime.datetime.now().timestamp())

        headers = {'Accept': 'application/json'}
        r = requests.get(
            'https://api.india.delta.exchange/v2/history/candles',
            params={'resolution': Time_period, 'symbol': symbol, 'start': start_timestamp, 'end': end_timestamp},
            headers=headers
        )

        if r.status_code == 200 and 'result' in r.json():
            df = pd.DataFrame(r.json()['result'])
            df['date_time'] = pd.to_datetime(df['time'], unit='s').dt.tz_localize('UTC').dt.tz_convert('Asia/Kolkata').dt.tz_localize(None)
            df = df.sort_values(by='time', ascending=True)

            if len(df) < max(RSI_Period, ADX_Len) + 2:
                print("Not enough historical data from API to calculate indicators. Waiting for more data.")
                sys.stdout.flush()
                time.sleep(55)
                continue

            df['rsi'] = RSI(df.close, RSI_Period).rsi()
            df['Prsi'] = df['rsi'].shift(1)
            df['VolEMA'] = df['volume'].ewm(span=Vol_EMA_Pd).mean()
            df['vol_change'] = (df['volume'] - df['volume'].shift(1)) / df['volume'].shift(1).replace(0, 1)
            df['adx'] = ADXIndicator(df['high'], df['low'], df['close'], window=ADX_Len).adx()

            df['Follow_Sell'] = ((df['Prsi'] - df['rsi']) >= 3.0) & \
                                (df['volume'] > df['VolEMA']) & \
                                (df['Prsi'] > 50) & (df['Prsi'] < 55) & \
                                (df['adx'] >= adx_follow) & (df['vol_change'] >= 0.75)
            df['Follow_Buy'] = ((df['rsi'] - df['Prsi']) >= 3.0) & \
                               (df['volume'] > df['VolEMA']) & \
                               (df['Prsi'] < 50) & (df['Prsi'] > 45) & \
                               (df['adx'] >= adx_follow) & (df['vol_change'] >= 0.75)
            df['BT_Sell'] = ((df['Prsi'] - df['rsi']) >= 1.0) & \
                            (df['Prsi'] > 35) & \
                            (df['adx'] >= adx_BT) & (df['adx'] < adx_BTU)
            df['BT_Buy'] = ((df['rsi'] - df['Prsi']) >= 1.0) & \
                           (df['Prsi'] < 65) & \
                           (df['adx'] >= adx_BT) & (df['adx'] < adx_BTU)

            df_cleaned = df.dropna()

            if df_cleaned.empty:
                print("DataFrame is empty after dropping NaN values from indicators. Not enough valid data for a signal.")
                sys.stdout.flush()
                time.sleep(55)
                continue

            latest = df_cleaned.iloc[-1]

            required_signal_columns = ['Follow_Sell', 'Follow_Buy', 'BT_Sell', 'BT_Buy']
            if not all(col in latest.index for col in required_signal_columns):
                print(f"Error: Required signal columns missing in latest candle data after cleanup: {required_signal_columns}")
                print(f"Available columns in latest: {latest.index.tolist()}")
                sys.stdout.flush()
                time.sleep(55)
                continue

            print(f">[{latest['date_time'].time()}]")
            sys.stdout.flush()

            signal_type = None
            if latest['Follow_Sell']:
                signal_type = 'sell'
            elif latest['Follow_Buy']:
                signal_type = 'buy'
            elif latest['BT_Sell']:
                signal_type = 'sell'
            elif latest['BT_Buy']:
                signal_type = 'buy'

            if signal_type:
                print(f"{signal_type.upper()} 🔔signal detected at {latest['date_time']}")
                sys.stdout.flush()
                # Loop through credentials and place orders for each account
                for creds in client_credentials:
                    client = DeltaRestClient(
                        base_url='https://api.india.delta.exchange',
                        api_key=creds['api_key'],
                        api_secret=creds['api_secret']
                    )
                    # Check for open trades specifically for this client
                    if not check_for_open_trades(client, symbol):
                        place_order(client, signal_type, symbol, order_quantity, latest)
                    else:
                        truncated_api_key = client.api_key[:6] + '...' + client.api_key[-4:]
                        print(f"Client {truncated_api_key}: Skipping order placement due to existing open trades.")
                        sys.stdout.flush()
            else:
                print("No trade signal detected.")
                sys.stdout.flush()

        else:
            print(f"Error fetching data: {r.status_code}. Response: {r.text}")
            sys.stdout.flush()

        time.sleep(55) # Sleep for almost the rest of the minute