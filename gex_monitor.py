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

IST = pytz.timezone('Asia/Kolkata')

TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')

S3_BUCKET_NAME = "gex-charts-mybitcoin"
LATEST_CHART_KEY = "latest_gex_chart.png"

SHEET_CREDENTIALS = '/home/ubuntu/Algo/gex-sheet-integration-1fa62d638e51.json'
SHEET_NAME = 'BTC GEX log'

# Constants for straddle premium calculation
RISK_FREE_RATE = 0.05  # 5% annual risk-free rate
DAYS_IN_YEAR = 365

scope = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive"
]

creds = ServiceAccountCredentials.from_json_keyfile_name(SHEET_CREDENTIALS, scope)
gs_client = gspread.authorize(creds)

def append_gex_data_to_sheet(row):
    try:
        sh = gs_client.open(SHEET_NAME)
        worksheet = sh.sheet1
        result = worksheet.append_row(row, value_input_option='USER_ENTERED')
        print("Appended row to Google Sheet:", row)
        print("Google Sheets API response:", result)
    except Exception as e:
        print("Error appending to Google Sheet:", repr(e))

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
        resp = requests.get(url, headers=HEADERS, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        return data['result']['mark_price']
    except Exception as e:
        print(f"Error fetching current BTC price: {e}")
        return None

def get_instruments():
    """Fetch all BTC option instruments from Deribit."""
    try:
        url = f"{BASE_URL}/public/get_instruments?currency=BTC&kind=option&expired=false"
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        data = resp.json()
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
    """Fetch option price from Deribit."""
    try:
        url = f"{BASE_URL}/public/ticker?instrument_name={instrument_name}"
        resp = requests.get(url, headers=HEADERS, timeout=10)
        resp.raise_for_status()
        data = resp.json()['result']
        mark_price = data.get('mark_price', 0.0)
        return mark_price
    except Exception as e:
        print(f"Error fetching option price for {instrument_name}: {e}")
        return None

def calculate_straddle_premium(instruments, price, expiry_ts):
    """Calculate ATM straddle premium for the given expiry."""
    try:
        # Find the closest ATM strike
        atm_strikes = sorted([i["strike"] for i in instruments if i["expiration_timestamp"] == expiry_ts], 
                            key=lambda x: abs(x - price))
        
        if not atm_strikes:
            print("No strikes found for straddle calculation")
            return None
            
        atm_strike = atm_strikes[0]
        print(f"Using strike {atm_strike} for ATM straddle calculation (current price: {price})")
        
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
            print(f"Could not find both call and put instruments for strike {atm_strike}")
            return None
            
        # Get prices for call and put
        call_price = get_option_price(call_instrument)
        put_price = get_option_price(put_instrument)
        
        if call_price is None or put_price is None:
            print("Failed to fetch option prices for straddle calculation")
            return None
            
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
        
        return {
            "straddle_premium": straddle_premium,
            "straddle_premium_pct": straddle_premium_pct,
            "days_to_expiry": days_to_expiry,
            "implied_volatility_approx": implied_vol_approx * 100  # Convert to percentage
        }
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
    
    append_gex_data_to_sheet(sheet_row)

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
        plt.grid(axis='y', linestyle='--', alpha=0.7)
        plt.legend()
        plt.tight_layout()
        plt.savefig(temp_filepath)
        print(f"Plot saved locally to: {temp_filepath}")
        plt.close()

        no_trade_line=""
        if ratio is not None and 80 <= ratio <= 120:
            no_trade_line = "👉🏻 Sideways\n"
        elif ratio is not None and ratio < 80:
            no_trade_line = "👇🏻 Bearish bias\n"
        elif ratio is not None and ratio > 120:
            no_trade_line = "👆🏻 Bullish bias\n"

        telegram_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
        
        # Add straddle premium to the caption if available
        straddle_info = ""
        if straddle_premium is not None:
            straddle_info = (
                f"----\n"
                f"Straddle Premium: ${straddle_premium:.2f} ({straddle_premium_pct:.2f}%)\n"
                f"Implied Vol: {implied_vol:.2f}%\n"
            )
        
        caption = (
            f"GEX below: {gex_below}\n"
            f"GEX above: {gex_above}\n"
            f"----\n"
            f"Ratio: {ratio_str}\n"
            f"{no_trade_line}"
            f"{straddle_info}"
            f"----\n"
            f"{direction_line} upto {largest_gex_strike:.0f}\n"
            f"Net GEX: {total_net_gex:,.0f}"
        )

        with open(temp_filepath, 'rb') as photo_file:
            files = {'photo': photo_file}
            data = {'chat_id': TELEGRAM_CHAT_ID, 'caption': caption}
            print("Sending chart to Telegram...")
            response = requests.post(telegram_url, files=files, data=data)
            response.raise_for_status()
            print(f"Chart sent to Telegram. Response: {response.json()}")

        print(f"Uploading chart to S3 bucket: {S3_BUCKET_NAME}...")
        upload_to_s3(temp_filepath, S3_BUCKET_NAME, LATEST_CHART_KEY)
    except Exception as e:
        print(f"Error generating plot, sending to Telegram, or uploading to S3: {e}")
        import traceback
        traceback.print_exc()
    finally:
        if os.path.exists(temp_filepath):
            os.remove(temp_filepath)
            print(f"Cleaned up temporary file: {temp_filepath}")

    print("Data collection cycle completed successfully.")
    print("==================================================")

if __name__ == "__main__":
    calculate_gamma_exposure()
    
