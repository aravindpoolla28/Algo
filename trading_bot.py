import sys # Import sys for flushing output
import time # Import time for time.localtime() and time.sleep()
import pandas as pd
import requests
import datetime
from ta.momentum import RSIIndicator as RSI
from ta.trend import ADXIndicator
from delta_rest_client import DeltaRestClient, OrderType # Ensure this is imported
import pytz # Import pytz for timezone conversion


# ==== Store all client credentials here ====
# ADD YOUR MULTIPLE API KEYS AND SECRETS HERE
# Example:
client_credentials = [
    {"api_key": '1nybRkqMUOp5PcUuQFvJptm3jJsZPu', "api_secret": 'zDgaOpt2QDk1HvOxObMKHT46DSOG0RZGQamcNJ0mb62RZx3njAlfjQA3xuob'},
    {"api_key": 'SAeyxviw90fQZaf8z5FLqobdoBx41X', "api_secret": 'AdLiUKLGReg8f7TxaxIY2bahhMMuXMXgSPZUoBBtFsf3I4CtzxDOWJs5zbNL'},
    # Add more accounts as needed
]

# ==== Telegram Bot Configuration ====
# IMPORTANT: These values are now populated based on our previous conversation.
TELEGRAM_BOT_TOKEN = '7877965990:AAFwec4v_FU2lRhhkeTXhYc93nbRy12ECIg' # Your bot token
TELEGRAM_CHAT_ID = '-1002715827375'   # Your group chat ID (starts with -)

# ==== Constants ====
RSI_Period = 30
Vol_EMA_Pd = 14
ADX_Len = 14
adx_follow = 20
adx_BT = 45
adx_BTU = 50
Time_period = '5m'
symbol = 'BTCUSD'
order_quantity = 10
TP_RISK_RATIO = 5

# Define the target timezone
INDIA_TZ = pytz.timezone('Asia/Kolkata')

# ==== Helper Function for Price Rounding ====
def round_to_tick_size(price, tick_size):
    """
    Rounds a price to the nearest multiple of the exchange's tick size.
    Ensures inputs are floats for division and handles zero tick_size.
    """
    try:
        price_f = float(price)
        tick_size_f = float(tick_size)
    except (ValueError, TypeError) as e:
        print(f"Error converting price ({price}) or tick_size ({tick_size}) to float in round_to_tick_size: {e}")
        sys.stdout.flush()
        return float(price) if isinstance(price, (int, float)) else 0.0

    if tick_size_f == 0:
        print("Warning: tick_size is zero in round_to_tick_size. Returning original price.")
        sys.stdout.flush()
        return price_f
    return round(price_f / tick_size_f) * tick_size_f

# ==== Function to send Telegram messages ====
def send_telegram_message(message):
    """
    Sends a message to the configured Telegram chat.
    """
    # Check if the token or chat ID are still placeholders
    if TELEGRAM_BOT_TOKEN == 'YOUR_TELEGRAM_BOT_TOKEN' or TELEGRAM_CHAT_ID == 'YOUR_TELEGRAM_CHAT_ID':
        print("Telegram bot token or chat ID not configured. Skipping Telegram notification.")
        sys.stdout.flush()
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        'chat_id': TELEGRAM_CHAT_ID,
        'text': message,
        'parse_mode': 'Markdown' # Optional: allows bold, italics, etc.
    }
    try:
        response = requests.post(url, json=payload)
        response.raise_for_status() # Raise an exception for HTTP errors
        print(f"Telegram message sent successfully.")
        sys.stdout.flush()
    except requests.exceptions.RequestException as e:
        print(f"Error sending Telegram message: {e}")
        sys.stdout.flush()
    except Exception as e:
        print(f"An unexpected error occurred while sending Telegram message: {e}")
        sys.stdout.flush()


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
    truncated_api_key = client.api_key[:6] + '...' + client.api_key[-4:]
    try:
        open_orders_response = client.get_live_orders()
        if open_orders_response and isinstance(open_orders_response, list) and len(open_orders_response) > 0:
            print(f"Client {truncated_api_key}: Found {len(open_orders_response)} open order(s). Skipping new order.")
            sys.stdout.flush()
            return True

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
        signal_candle_data (pd.Series): The pandas Series containing the OHLCV data for the candle that generated the signal.
    """
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

        # Send Telegram notification after successful order placement
        telegram_message = (
            f"🔔 *TRADE ALERT!* 🔔\n"
            f"Client: `{truncated_api_key}`\n"
            f"Symbol: `{symbol}`\n"
            f"Side: *{side.upper()}*\n"
            f"Quantity: `{size}`\n"
            f"Entry Est: `{entry_price_estimate:.2f}`\n"
            f"SL: `{stop_loss_price:.2f}`\n"
            f"TP: `{take_profit_price:.2f}`\n"
            f"Response: ```json\n{order_response_data}\n```"
        )
        send_telegram_message(telegram_message)

    except Exception as e:
        print(f"Client {truncated_api_key}: Order failed: {e}")
        sys.stdout.flush()
        import traceback
        traceback.print_exc()
        sys.stdout.flush()
        telegram_error_message = (
            f"❌ *ORDER FAILED!* ❌\n"
            f"Client: `{truncated_api_key}`\n"
            f"Symbol: `{symbol}`\n"
            f"Side: *{side.upper()}*\n"
            f"Error: `{e}`"
        )
        send_telegram_message(telegram_error_message)

# ==== Main Loop ====
while True:
    # Get current time in UTC, then convert to India timezone
    current_utc_time = datetime.datetime.now(datetime.UTC)
    current_ist_time = current_utc_time.replace(tzinfo=pytz.utc).astimezone(INDIA_TZ)

    cmin = current_ist_time.strftime("%M")
    csec = current_ist_time.strftime("%S")

    time.sleep(1)

    if int(cmin) % 1 == 0 and int(csec) == 6:
        # print(f"\n--- Running trade logic at {current_ist_time.strftime('%Y-%m-%d %H:%M:%S')} (IST) ---")
        sys.stdout.flush()

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
            df['date_time'] = pd.to_datetime(df['time'], unit='s').dt.tz_localize('UTC').dt.tz_convert(INDIA_TZ).dt.tz_localize(None)
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


            df['Follow_Sell'] = ((df['Prsi'] - df['rsi']) >= 3.0) & (df['volume'] > df['VolEMA']) & (df['Prsi'] > 50) & (df['Prsi'] < 55) & (df['adx'] >= adx_follow) & (df['vol_change'] >= 0.75)
            df['Follow_Buy'] = ((df['rsi'] - df['Prsi']) >= 3.0) & (df['volume'] > df['VolEMA']) & (df['Prsi'] < 50) & (df['Prsi'] > 45) & (df['adx'] >= adx_follow) & (df['vol_change'] >= 0.75)
            df['BT_Sell'] = ((df['Prsi'] - df['rsi']) >= 3.0) & (df['Prsi'] > 65) & (df['adx'] >= adx_BT) & (df['adx'] < adx_BTU)
            df['BT_Buy'] = ((df['rsi'] - df['Prsi']) >= 3.0) & (df['Prsi'] < 35) & (df['adx'] >= adx_BT) & (df['adx'] < adx_BTU)

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

            print(f"> No signal detected at: [{latest['date_time'].time()}]")
            sys.stdout.flush()

            signal_type = None
            if latest['Follow_Sell']:
                signal_type = 'sell'
            elif latest['Follow_Buy']:
                signal_type = 'buy'
            # elif latest['BT_Sell']:
            #     signal_type = 'sell'
            # elif latest['BT_Buy']:
            #     signal_type = 'buy'

            if signal_type:
                print(f"{signal_type.upper()} 🔔signal detected at {latest['date_time']}")
                sys.stdout.flush()
                for creds in client_credentials:
                    client = DeltaRestClient(
                        base_url='https://api.india.delta.exchange',
                        api_key=creds['api_key'],
                        api_secret=creds['api_secret']
                    )
                    if not check_for_open_trades(client, symbol):
                        place_order(client, signal_type, symbol, order_quantity, latest)
                    else:
                        truncated_api_key = client.api_key[:6] + '...' + client.api_key[-4:]
                        print(f"Client {truncated_api_key}: Skipping order placement due to existing open trades.")
                        sys.stdout.flush()
            else:
                # MODIFIED SECTION: Print indicator values when no signal is detected
                print(f"No trade signal | RSI: {latest['rsi']:.0f} (Prev: {latest['Prsi']:.0f}) | Volume: {latest['volume']:.0f} (EMA: {latest['VolEMA']:.0f}) | ADX: {latest['adx']:.0f} | Volume Change: {latest['vol_change'] * 100:.0f}%")
                sys.stdout.flush()

        else:
            print(f"Error fetching data: {r.status_code}. Response: {r.text}")
            sys.stdout.flush()

        time.sleep(55) # Sleep for almost the rest of the minute
