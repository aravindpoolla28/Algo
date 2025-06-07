import sys # Import sys for flushing output
import time # Import time for time.localtime() and time.sleep()
import pandas as pd
import requests
import datetime
from ta.momentum import RSIIndicator as RSI
from ta.trend import EMAIndicator # Import EMA indicator for calculations
import pytz # Import pytz for timezone conversion

# ==== Store all client credentials here ====
# WARNING: API KEYS AND SECRETS ARE HARDCODED BELOW.
# THIS IS HIGHLY INSECURE FOR PRODUCTION OR PUBLIC REPOSITORIES.
# FOR SECURE DEPLOYMENT, REVERT TO USING GITHUB SECRETS OR A SIMILAR METHOD.
client_credentials = [
    {"api_key": '1nybRkqMUOp5PcUuQFvJptm3jJsZPu', "api_secret": 'zDgaOpt2QDk1HvOxObMKHT46DSOG0RZGQamcNJ0mb62RZx3njAlfjQA3xuob'},
    {"api_key": 'SAeyxviw90fQZaf8z5FLqobdoBx41X', "api_secret": 'AdLiUKLGReg8f7TxaxIY2bahhMMuXMXgSPZUoBBtFsf3I4CtzxDOWJs5zbNL'},
]

# ==== Telegram Bot Configuration ====
# WARNING: TELEGRAM TOKEN AND CHAT ID ARE HARDCODED BELOW.
# THIS IS HIGHLY INSECURE FOR PRODUCTION OR PUBLIC REPOSITORIES.
# FOR SECURE DEPLOYMENT, REVERT TO USING GITHUB SECRETS OR A SIMILAR METHOD.
TELEGRAM_BOT_TOKEN = '7877965990:AAFwec4v_FU2lRhhkeTXhYc93nbRy12ECIg' # Your bot token
TELEGRAM_CHAT_ID = '-1002715827375'   # Your group chat ID (starts with -)


# ==== Constants (New Algo Parameters) ====
SHORT_EMA_PERIOD = 12 # Period for the faster EMA
LONG_EMA_PERIOD = 26  # Period for the slower EMA
RSI_PERIOD = 14       # Standard RSI period
RSI_OVERBOUGHT = 70   # RSI level considered overbought
RSI_OVERSOLD = 30     # RSI level considered oversold
TP_RISK_RATIO = 2.5   # Take Profit Risk-Reward Ratio (Adjusted from 5 for potentially more frequent exits)
SL_PERCENTAGE = 0.01  # 1% Stop Loss (from entry price)

Time_period = '5m'
symbol = 'BTCUSD'
order_quantity = 1


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
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram bot token or chat ID is empty. Skipping Telegram notification.")
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
    truncated_api_key = client.api_key[:6] + '...' + client.api_key[-4:] if client.api_key else "N/A"
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
def place_order(client, side, symbol, size, entry_price_estimate, stop_loss_price, take_profit_price):
    """
    Places a market order with a bracket (Stop Loss and Take Profit) using client.request.
    Args:
        client (DeltaRestClient): An initialized DeltaRestClient instance.
        side (str): The order side ('buy' or 'sell').
        symbol (str): The trading pair symbol.
        size (int): The quantity of the asset to trade.
        entry_price_estimate (float): The estimated entry price (e.g., current close).
        stop_loss_price (float): The calculated Stop Loss price.
        take_profit_price (float): The calculated Take Profit price.
    """
    truncated_api_key = client.api_key[:6] + '...' + client.api_key[-4:] if client.api_key else "N/A"
    try:
        product = client.get_product(symbol)
        market_id = product['id']
        tick_size = product.get('tick_size', 0.01)

        print(f"Client {truncated_api_key}: Preparing bracket order for {symbol} with market ID: {market_id}")
        sys.stdout.flush()

        if side not in ['buy', 'sell']:
            raise ValueError("Invalid side, must be 'buy' or 'sell'")

        # Round SL/TP to tick size
        stop_loss_price = round_to_tick_size(stop_loss_price, tick_size)
        take_profit_price = round_to_tick_size(take_profit_price, tick_size)

        print(f"Client {truncated_api_key}: Calculated Entry Estimate: {entry_price_estimate:.2f}, SL: {stop_loss_price:.2f}, TP: {take_profit_price:.2f}")
        sys.stdout.flush()

        payload = {
            "product_id": market_id,
            "size": size,
            "side": side,
            "order_type": "market_order",
            "post_only": False,
            "bracket_stop_loss_price": stop_loss_price,
            "bracket_take_profit_price": take_profit_price,
            "bracket_stop_loss_limit_price": stop_loss_price, # Assuming limit price is same as trigger price for simplicity
            "bracket_take_profit_limit_price": take_profit_price, # Assuming limit price is same as trigger price for simplicity
        }

        response = client.request("POST", "/v2/orders", payload, auth=True)
        order_response_data = response.json()
        print(f"Client {truncated_api_key}: Bracket order placed for {side.upper()}. Response: {order_response_data}")
        sys.stdout.flush()

        # Send Telegram notification after successful order placement
        telegram_message = (
            f"🔔 *TRADE ALERT for Crossover Algo Bot!* 🔔\n"
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
    current_utc_time = datetime.datetime.now(pytz.utc)
    current_ist_time = current_utc_time.astimezone(INDIA_TZ)

    cmin = current_ist_time.strftime("%M")
    csec = current_ist_time.strftime("%S")

    time.sleep(1)

    if int(cmin) % 1 == 0 and int(csec) == 6:
        sys.stdout.flush()

        yesterday = datetime.date.today() - datetime.timedelta(days=1)
        start_date = datetime.datetime.combine(yesterday, datetime.time(0, 0, 0))
        start_timestamp = int(pytz.utc.localize(start_date).timestamp())
        end_timestamp = int(datetime.datetime.now(pytz.utc).timestamp())

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

            # Ensure enough data for EMA and RSI calculations
            required_data_length = max(LONG_EMA_PERIOD, RSI_PERIOD) + 2 # +2 for shift and current candle
            if len(df) < required_data_length:
                print(f"Not enough historical data ({len(df)} candles) from API to calculate indicators. Need at least {required_data_length}. Waiting for more data.")
                sys.stdout.flush()
                time.sleep(55)
                continue

            # ==== Calculate New Indicators ====
            df['short_ema'] = EMAIndicator(df.close, SHORT_EMA_PERIOD).ema_indicator()
            df['long_ema'] = EMAIndicator(df.close, LONG_EMA_PERIOD).ema_indicator()
            df['rsi'] = RSI(df.close, RSI_PERIOD).rsi()

            df_cleaned = df.dropna()

            if df_cleaned.empty:
                print("DataFrame is empty after dropping NaN values from indicators. Not enough valid data for a signal.")
                sys.stdout.flush()
                time.sleep(55)
                continue

            latest = df_cleaned.iloc[-1]
            previous = df_cleaned.iloc[-2] # Need previous for crossover checks

            # ==== Signal Logic (Dual EMA Crossover with RSI Confirmation) ====
            # Buy signal: Short EMA crosses above Long EMA AND RSI confirms
            buy_signal = (previous['short_ema'] <= previous['long_ema'] and
                          latest['short_ema'] > latest['long_ema'] and
                          latest['rsi'] > 50 and # RSI indicating bullish momentum
                          latest['rsi'] < RSI_OVERBOUGHT) # Not overbought

            # Sell signal: Short EMA crosses below Long EMA AND RSI confirms
            sell_signal = (previous['short_ema'] >= previous['long_ema'] and
                           latest['short_ema'] < latest['long_ema'] and
                           latest['rsi'] < 50 and # RSI indicating bearish momentum
                           latest['rsi'] > RSI_OVERSOLD) # Not oversold

            signal_type = None
            if buy_signal:
                signal_type = 'buy'
            elif sell_signal:
                signal_type = 'sell'

            print(f"> No signal detected at: [{current_ist_time.strftime('%H:%M:%S')}]")
            sys.stdout.flush()

            if signal_type:
                print(f"{signal_type.upper()} 🔔signal detected at {latest['date_time']} (Close: {latest['close']:.2f})")
                sys.stdout.flush()

                # Calculate SL/TP based on new strategy's percentage logic
                entry_price = float(latest['close']) # Ensure entry_price is float
                stop_loss_price = 0.0
                take_profit_price = 0.0

                if signal_type == 'buy':
                    stop_loss_price = entry_price * (1 - SL_PERCENTAGE)
                    risk_points = entry_price - stop_loss_price
                    take_profit_price = entry_price + (risk_points * TP_RISK_RATIO)
                elif signal_type == 'sell':
                    stop_loss_price = entry_price * (1 + SL_PERCENTAGE)
                    risk_points = stop_loss_price - entry_price
                    take_profit_price = entry_price - (risk_points * TP_RISK_RATIO)

                # Ensure risk_points is positive for calculation
                # This fallback should ideally not be needed with percentage-based SL,
                # but kept for robustness.
                if risk_points <= 0:
                    # Fallback to a tiny fixed percentage if calculated risk is zero or negative (unlikely with percentage SL)
                    risk_points = entry_price * 0.001
                    if signal_type == 'buy':
                        stop_loss_price = entry_price - risk_points
                        take_profit_price = entry_price + (risk_points * TP_RISK_RATIO)
                    else: # sell
                        stop_loss_price = entry_price + risk_points
                        take_profit_price = entry_price - (risk_points * TP_RISK_RATIO)
                    print(f"Client: Adjusted risk points due to initial non-positive value. New risk: {risk_points:.2f}")
                    sys.stdout.flush()


                for creds in client_credentials:
                    client = DeltaRestClient(
                        base_url='https://api.india.delta.exchange',
                        api_key=creds['api_key'],
                        api_secret=creds['api_secret']
                    )
                    if not check_for_open_trades(client, symbol):
                        place_order(client, signal_type, symbol, order_quantity, entry_price, stop_loss_price, take_profit_price)
                    else:
                        truncated_api_key = client.api_key[:6] + '...' + client.api_key[-4:]
                        print(f"Client {truncated_api_key}: Skipping order placement due to existing open trades.")
                        sys.stdout.flush()
            else:
                # This block is now intentionally empty as per your request
                # Only the "> No signal detected at: [HH:MM:SS]" line will print
                pass
                sys.stdout.flush()

        else:
            print(f"Error fetching data: {r.status_code}. Response: {r.text}")
            sys.stdout.flush()
            send_telegram_message(f"❌ *Data Fetch Error!* ❌\nStatus Code: `{r.status_code}`\nResponse: `{r.text}`")


        time.sleep(55) # Sleep for almost the rest of the minute
