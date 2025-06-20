import requests
import time
import gspread
import datetime
import json
import os
import matplotlib
matplotlib.use('Agg')  # For non-GUI environments
import matplotlib.pyplot as plt
import pytz
import boto3
import math

from oauth2client.service_account import ServiceAccountCredentials

# --- Configuration ---
BASE_URL = "https://www.deribit.com/api/v2"
HEADERS = {"Accept": "application/json"}
PRICE_RANGE_POINTS = 6000

# Debug mode - set to True to enable detailed logging
DEBUG_MODE = True

IST = pytz.timezone('Asia/Kolkata')

TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')

S3_BUCKET_NAME = "gex-charts-mybitcoin"
LATEST_CHART_KEY = "latest_gex_chart.png"

# Update this path to match your actual credentials file location
SHEET_CREDENTIALS = '/home/ubuntu/Algo/gex-sheet-integration-1fa62d638e51.json'
SHEET_NAME = 'BTC GEX log'

# Constants for straddle premium calculation
RISK_FREE_RATE = 0.05  # 5% annual risk-free rate
DAYS_IN_YEAR = 365

# Number of strikes to try for ATM straddle calculation
MAX_STRIKES_TO_TRY = 5

scope = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive"
]

def debug_log(message):
    """Print debug messages if DEBUG_MODE is enabled"""
    if DEBUG_MODE:
        print(f"[DEBUG] {message}")

def append_gex_data_to_sheet(row):
    try:
        # Check if credentials file exists
        if not os.path.exists(SHEET_CREDENTIALS):
            print(f"WARNING: Google Sheets credentials file not found at {SHEET_CREDENTIALS}")
            print("Skipping Google Sheets update. Please check the file path.")
            return False
            
        creds = ServiceAccountCredentials.from_json_keyfile_name(SHEET_CREDENTIALS, scope)
        gs_client = gspread.authorize(creds)
        sh = gs_client.open(SHEET_NAME)
        worksheet = sh.sheet1
        result = worksheet.append_row(row, value_input_option='USER_ENTERED')
        print("Appended row to Google Sheet:", row)
        print("Google Sheets API response:", result)
        return True
    except FileNotFoundError:
        print(f"ERROR: Google Sheets credentials file not found at {SHEET_CREDENTIALS}")
        return False
    except Exception as e:
        print(f"Error appending to Google Sheet: {repr(e)}")
        return False

def upload_to_s3(file_name, bucket, object_name=None):
    if object_name is None:
        object_name = os.path.basename(file_name)
    s3_client = boto3.client('s3')
    try:
        s3_client.upload_file(file_name, bucket, object_name,
                              ExtraArgs={'ContentType': 'image/png'})
        print(f"File {file_name} uploaded to s3://{bucket}/{object_name}")
    except Exception as e:
        print(f"Error uploading file to S3: {e}")
        import traceback
        traceback.print_exc()
        return False
    return True

def get_current_price():
    """Get the current BTC-PERPETUAL mark price from Deribit."""
    try:
        url = f"{BASE_URL}/public/ticker?instrument_name=BTC-PERPETUAL"
        debug_log(f"Fetching current price from: {url}")
        resp = requests.get(url, headers=HEADERS, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        price = data['result']['mark_price']
        debug_log(f"Current price API response: {json.dumps(data['result'], indent=2)}")
        return price
    except Exception as e:
        print(f"Error fetching current BTC price: {e}")
        return None

def get_instruments():
    """Fetch all BTC option instruments from Deribit."""
    try:
        url = f"{BASE_URL}/public/get_instruments?currency=BTC&kind=option&expired=false"
        debug_log(f"Fetching instruments from: {url}")
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        debug_log(f"Found {len(data['result'])} instruments")
        return data['result']
    except Exception as e:
        print(f"Error fetching BTC instruments: {e}")
        return []

def get_next_expiry(instruments):
    """Find the next expiry timestamp (8:00 UTC) from the instruments list."""
    expiries = sorted(set(i['expiration_timestamp'] for i in instruments))
    now = int(time.time() * 1000)
    for ts in expiries:
        if ts > now:
            dt = datetime.datetime.fromtimestamp(ts/1000, tz=datetime.timezone.utc)
            if dt.hour == 8 and dt.minute == 0:
                return ts
    return None

def format_ts_to_label(ts):
    """Format timestamp to expiry label, e.g. 20JUN25."""
    dt = datetime.datetime.fromtimestamp(ts/1000, tz=datetime.timezone.utc)
    return dt.strftime('%d%b%y').upper()

def get_greeks_and_oi(instrument_name):
    """Fetch gamma and open interest for an instrument."""
    try:
        url = f"{BASE_URL}/public/ticker?instrument_name={instrument_name}"
        resp = requests.get(url, headers=HEADERS, timeout=10)
        resp.raise_for_status()
        data = resp.json()['result']
        gamma = data.get('greeks', {}).get('gamma', 0.0)
        oi = data.get('open_interest', 0)
        return {'gamma': gamma, 'oi': oi}
    except Exception as e:
        print(f"Error fetching greeks/OI for {instrument_name}: {e}")
        return None

def get_option_price(instrument_name):
    """Fetch option price from Deribit with enhanced debugging."""
    try:
        url = f"{BASE_URL}/public/ticker?instrument_name={instrument_name}"
        debug_log(f"Fetching option price for {instrument_name} from: {url}")
        resp = requests.get(url, headers=HEADERS, timeout=10)
        resp.raise_for_status()
        data = resp.json()['result']
        
        # Log the full response for debugging
        debug_log(f"Option price API response for {instrument_name}: {json.dumps(data, indent=2)}")
        
        mark_price = data.get('mark_price', 0.0)
        
        # Validate the price
        if mark_price <= 0:
            debug_log(f"WARNING: Zero or negative price ({mark_price}) returned for {instrument_name}")
            
        # Check other price indicators if mark_price is zero
        if mark_price == 0:
            debug_log("Attempting to use alternative price indicators...")
            # Try last_price if available
            last_price = data.get('last_price')
            if last_price and last_price > 0:
                debug_log(f"Using last_price ({last_price}) instead of mark_price")
                return last_price
                
            # Try best_bid_price if available
            best_bid = data.get('best_bid_price')
            if best_bid and best_bid > 0:
                debug_log(f"Using best_bid_price ({best_bid}) instead of mark_price")
                return best_bid
                
            # Try best_ask_price if available
            best_ask = data.get('best_ask_price')
            if best_ask and best_ask > 0:
                debug_log(f"Using best_ask_price ({best_ask}) instead of mark_price")
                return best_ask
                
            # Try settlement_price if available
            settlement = data.get('settlement_price')
            if settlement and settlement > 0:
                debug_log(f"Using settlement_price ({settlement}) instead of mark_price")
                return settlement
        
        return mark_price
    except Exception as e:
        print(f"Error fetching option price for {instrument_name}: {e}")
        import traceback
        traceback.print_exc()
        return None

def calculate_straddle_premium(instruments, price, expiry_ts):
    """Calculate ATM straddle premium for the given expiry with enhanced error handling and fallbacks."""
    try:
        # Find all strikes sorted by distance from current price
        all_strikes = sorted([i["strike"] for i in instruments if i["expiration_timestamp"] == expiry_ts], 
                            key=lambda x: abs(x - price))
        
        if not all_strikes:
            print("No strikes found for straddle calculation")
            return None
            
        debug_log(f"Found {len(all_strikes)} strikes for expiry {format_ts_to_label(expiry_ts)}")
        debug_log(f"Current price: {price}, closest strikes: {all_strikes[:5]}")
        
        # Try multiple strikes in order of proximity to current price
        strikes_to_try = all_strikes[:min(MAX_STRIKES_TO_TRY, len(all_strikes))]
        
        for atm_strike in strikes_to_try:
            debug_log(f"Attempting straddle calculation with strike {atm_strike}")
            
            # Find the call and put instruments for this strike
            call_instrument = None
            put_instrument = None
            
            for instr in instruments:
                if instr["expiration_timestamp"] == expiry_ts and instr["strike"] == atm_strike:
                    if instr["option_type"] == "call":
                        call_instrument = instr["instrument_name"]
                    else:
                        put_instrument = instr["instrument_name"]
            
            if not call_instrument or not put_instrument:
                debug_log(f"Could not find both call and put instruments for strike {atm_strike}, trying next strike")
                continue
                
            debug_log(f"Found instruments: Call: {call_instrument}, Put: {put_instrument}")
            
            # Get prices for call and put
            call_price = get_option_price(call_instrument)
            put_price = get_option_price(put_instrument)
            
            debug_log(f"Retrieved prices: Call: {call_price}, Put: {put_price}")
            
            if call_price is None or put_price is None:
                debug_log("Failed to fetch option prices, trying next strike")
                continue
                
            if call_price <= 0 or put_price <= 0:
                debug_log(f"Invalid prices (call: {call_price}, put: {put_price}), trying next strike")
                continue
                
            # Calculate straddle premium
            straddle_premium = call_price + put_price
            straddle_premium_pct = (straddle_premium / price) * 100
            
            # Calculate days to expiry for annualized values
            now = int(time.time() * 1000)
            days_to_expiry = (expiry_ts - now) / (1000 * 60 * 60 * 24)
            
            # Calculate implied volatility from straddle premium
            # Using simplified approximation: IV ≈ straddle_premium / (spot_price * sqrt(T))
            # where T is time to expiry in years
            time_to_expiry_years = days_to_expiry / DAYS_IN_YEAR
            implied_vol_approx = straddle_premium / (price * math.sqrt(time_to_expiry_years))
            
            debug_log(f"Successfully calculated straddle premium: ${straddle_premium:.2f} ({straddle_premium_pct:.2f}%)")
            debug_log(f"Implied volatility: {implied_vol_approx * 100:.2f}%, Days to expiry: {days_to_expiry:.2f}")
            
            return {
                "straddle_premium": straddle_premium,
                "straddle_premium_pct": straddle_premium_pct,
                "days_to_expiry": days_to_expiry,
                "implied_volatility_approx": implied_vol_approx * 100,  # Convert to percentage
                "strike": atm_strike,
                "call_price": call_price,
                "put_price": put_price,
                "call_instrument": call_instrument,
                "put_instrument": put_instrument
            }
        
        # If we get here, all strikes failed
        print("Could not calculate straddle premium: all strikes failed validation")
        return None
        
    except Exception as e:
        print(f"Error calculating straddle premium: {e}")
        import traceback
        traceback.print_exc()
        return None

def calculate_gamma_exposure():
    print("\n" + "=" * 50)
    print("Beginning new data collection cycle...")

    now_utc = datetime.datetime.now(datetime.timezone.utc)
    now_ist = now_utc.astimezone(IST)
    print("Fetching data at", now_ist.strftime("%Y-%m-%d %H:%M:%S %Z"))

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("ERROR: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID environment variables are not set. Cannot send to Telegram.")
        print("Please set them on your EC2 instance (e.g., in crontab -e).")
        print("Cycle completed with errors.")
        return

    price = get_current_price()
    if price is None:
        print("Failed to get current BTC price. Skipping this iteration.")
        print("Cycle completed with errors.")
        return

    print(f"Current BTC Price (from BTC-PERPETUAL Mark Price): ${price:,.2f}")

    lower_strike_bound = price - PRICE_RANGE_POINTS
    upper_strike_bound = price + PRICE_RANGE_POINTS
    print(f"Filtering strikes between {lower_strike_bound:,.0f} and {upper_strike_bound:,.0f} (±{PRICE_RANGE_POINTS} from current price)")

    instruments = get_instruments()
    if not instruments:
        print("No BTC options instruments found. Skipping this iteration.")
        print("Cycle completed with errors.")
        return

    target_expiry_ts = get_next_expiry(instruments)
    if not target_expiry_ts:
        print("No upcoming expiry found matching the next 8 AM UTC time. Skipping this iteration.")
        print("Cycle completed with errors.")
        return

    expiry_label = format_ts_to_label(target_expiry_ts)
    print(f"Targeting next expiry: {expiry_label} (approx. {datetime.datetime.fromtimestamp(target_expiry_ts/1000, tz=datetime.timezone.utc).isoformat()} UTC)")

    # Calculate straddle premium for the next expiry
    print("\nCalculating ATM straddle premium...")
    straddle_data = calculate_straddle_premium(instruments, price, target_expiry_ts)
    straddle_premium = None
    straddle_premium_pct = None
    implied_vol = None
    
    if straddle_data:
        straddle_premium = straddle_data["straddle_premium"]
        straddle_premium_pct = straddle_data["straddle_premium_pct"]
        implied_vol = straddle_data["implied_volatility_approx"]
        print(f"ATM Straddle Premium: ${straddle_premium:.2f} ({straddle_premium_pct:.2f}% of spot)")
        print(f"Implied Volatility (approx): {implied_vol:.2f}%")
        print(f"Days to Expiry: {straddle_data['days_to_expiry']:.2f}")
        print(f"Strike: {straddle_data['strike']}")
        print(f"Call Price: ${straddle_data['call_price']:.2f}, Put Price: ${straddle_data['put_price']:.2f}")
    else:
        print("Could not calculate straddle premium")

    relevant_options_all_strikes = [
        i for i in instruments
        if i["expiration_timestamp"] == target_expiry_ts
    ]
    if not relevant_options_all_strikes:
        print(f"No options found for {expiry_label} expiry. Skipping this iteration.")
        print("Cycle completed with errors.")
        return

    relevant_options_filtered_by_price = [
        i for i in relevant_options_all_strikes
        if lower_strike_bound <= i["strike"] <= upper_strike_bound
    ]
    if not relevant_options_filtered_by_price:
        print(f"No options found within ±{PRICE_RANGE_POINTS} price range for {expiry_label} expiry. Skipping this iteration.")
        print("Cycle completed with errors.")
        return

    print(f"\nProcessing {len(relevant_options_filtered_by_price)} options for {expiry_label} expiry within price range...\n")

    strike_map = {}
    for instr in relevant_options_filtered_by_price:
        instrument_name = instr["instrument_name"]
        data = get_greeks_and_oi(instrument_name)
        if data is None:
            continue
        strike = instr["strike"]
        gamma_from_api = data["gamma"]
        oi_from_api = data["oi"]
        option_type = instr["option_type"]
        if strike not in strike_map:
            strike_map[strike] = {"call_gamma_sum": 0.0, "call_oi_sum": 0,
                                  "put_gamma_sum": 0.0, "put_oi_sum": 0}
        if option_type == "call":
            strike_map[strike]["call_gamma_sum"] += gamma_from_api * oi_from_api
            strike_map[strike]["call_oi_sum"] += oi_from_api
        else:
            strike_map[strike]["put_gamma_sum"] += gamma_from_api * oi_from_api
            strike_map[strike]["put_oi_sum"] += oi_from_api

    net_gex_map = {}
    for strike in strike_map:
        call_total_gamma_oi = strike_map[strike]["call_gamma_sum"]
        put_total_gamma_oi = strike_map[strike]["put_gamma_sum"]
        net_gex = call_total_gamma_oi - put_total_gamma_oi
        net_gex_map[strike] = round(net_gex * 1000)

    sorted_strikes = sorted(net_gex_map.keys())
    gex_values = [net_gex_map[s] for s in sorted_strikes]
    total_net_gex = sum(gex_values)
    current_time_hhmm = now_ist.strftime('%H:%M')

    gex_below = sum(abs(gex) for strike, gex in net_gex_map.items() if strike < price)
    gex_above = sum(abs(gex) for strike, gex in net_gex_map.items() if strike > price)
    gex_total = gex_below + gex_above
    if gex_total > 0 and gex_below > 0:
        ratio = (gex_above / gex_below)*100
        ratio_str = f"{ratio:.0f}%"
    else:
        ratio = None
        ratio_str = "N/A"

    largest_gex_strike = max(net_gex_map, key=lambda x: abs(net_gex_map[x]))
    distance_to_largest_gex = abs(price - largest_gex_strike)

    direction_line = ""
    if price > largest_gex_strike:
        direction_line = f"👇🏻 by {int(distance_to_largest_gex)}"
    elif price < largest_gex_strike:
        direction_line = f"👆🏻 by {int(distance_to_largest_gex)}"

    # Add straddle premium to the sheet row
    sheet_row = [
        now_ist.strftime("%Y-%m-%d %H:%M:%S"),
        price,
        expiry_label,
        gex_below,
        gex_above,
        ratio_str,
        largest_gex_strike,
        direction_line,
        total_net_gex
    ]
    
    # Add straddle premium data if available
    if straddle_premium is not None:
        sheet_row.extend([
            f"{straddle_premium:.2f}",
            f"{straddle_premium_pct:.2f}%",
            f"{implied_vol:.2f}%"
        ])
    else:
        sheet_row.extend(["N/A", "N/A", "N/A"])
    
    # Try to append to Google Sheet, but continue if it fails
    sheet_result = append_gex_data_to_sheet(sheet_row)
    if not sheet_result:
        print("WARNING: Failed to update Google Sheet, continuing with other operations")

    temp_dir = "/tmp"
    output_filename = "current_gex_chart.png"
    temp_filepath = os.path.join(temp_dir, output_filename)

    try:
        print("Generating and saving chart locally...")
        plt.figure(figsize=(12, 7))
        bar_width = min(abs(sorted_strikes[i+1]-sorted_strikes[i]) for i in range(len(sorted_strikes)-1)) * 0.8 if len(sorted_strikes) > 1 else 1000
        bars = plt.bar(sorted_strikes, gex_values, width=bar_width, color='skyblue')
        for bar in bars:
            yval = bar.get_height()
            plt.text(bar.get_x() + bar.get_width()/2, yval, round(yval), ha='center', va='bottom', fontsize=12)
        for i, strike in enumerate(sorted_strikes):
            if strike == max(net_gex_map, key=net_gex_map.get):
                bars[i].set_color('green')
            elif strike == min(net_gex_map, key=net_gex_map.get):
                bars[i].set_color('red')
            elif strike == min(net_gex_map.keys(), key=lambda x: abs(x - price)):
                bars[i].set_color('orange')
            elif strike == largest_gex_strike:
                bars[i].set_edgecolor('black')
                bars[i].set_linewidth(2)
        plt.axhline(0, color='gray', linestyle='--', linewidth=0.8)
        plt.axvline(price, color='red', linestyle=':', linewidth=2, label=f'Current BTC Price (${price:,.0f})')
        plt.title('BTC GEX for next expiry', fontsize=14)
        plt.xlabel('Strike Price', fontsize=12)
        plt.ylabel('Net Gamma Exposure (BTC Equivalent)', fontsize=12)
        plt.xticks(sorted_strikes, rotation=90, ha='right')
