import requests
import time
import datetime
import json
import os
import matplotlib
matplotlib.use('Agg') # IMPORTANT: Use Agg backend for non-GUI environments
import matplotlib.pyplot as plt
import pytz # For timezone handling
import boto3 # NEW: For S3 upload

# --- Configuration ---
BASE_URL = "https://www.deribit.com/api/v2"
HEADERS = {"Accept": "application/json"}
PRICE_RANGE_POINTS = 6000 # Define the range around the current price

# Define the Indian Standard Time timezone
IST = pytz.timezone('Asia/Kolkata')

# --- Telegram Configuration ---
# These variables should be set as environment variables on your EC2 instance for security.
# Example in your crontab -e file:
# export TELEGRAM_BOT_TOKEN="YOUR_ACTUAL_BOT_TOKEN"
# export TELEGRAM_CHAT_ID="YOUR_ACTUAL_CHAT_ID"
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')

# --- S3 Configuration ---
S3_BUCKET_NAME = "gex-charts-mybitcoin" # Your specified S3 bucket
LATEST_CHART_KEY = "latest_gex_chart.png" # The file to be overwritten on S3 for the website

# --- S3 Upload Helper Function (NEW) ---
def upload_to_s3(file_name, bucket, object_name=None):
    """Upload a file to an S3 bucket

    :param file_name: File to upload
    :param bucket: Bucket to upload to
    :param object_name: S3 object name. If not specified then file_name is used
    :return: True if file was uploaded, else False
    """
    if object_name is None:
        object_name = os.path.basename(file_name)

    s3_client = boto3.client('s3')
    try:
        s3_client.upload_file(file_name, bucket, object_name,
                               ExtraArgs={'ContentType': 'image/png'}) # Specify content type
        print(f"File {file_name} uploaded to s3://{bucket}/{object_name}")
    except Exception as e:
        print(f"Error uploading file to S3: {e}")
        import traceback
        traceback.print_exc()
        return False
    return True

# --- API Interaction Functions ---
def get_current_price():
    url = f"{BASE_URL}/public/ticker?instrument_name=BTC-PERPETUAL"
    try:
        response = requests.get(url, headers=HEADERS)
        response.raise_for_status()
        data = response.json()
        if data and "result" in data and "index_price" in data["result"]:
            return float(data["result"]["index_price"])
        else:
            print(f"API Error: No 'result' or 'index_price' in response for {url}. Full response: {data}")
            return None
    except requests.exceptions.RequestException as e:
        print(f"Error fetching BTC-PERPETUAL price: {e}")
        return None

def get_instruments():
    url = f"{BASE_URL}/public/get_instruments?currency=BTC&kind=option&expired=false"
    try:
        response = requests.get(url, headers=HEADERS)
        response.raise_for_status()
        data = response.json()
        if data and "result" in data and isinstance(data["result"], list):
            return data["result"]
        else:
            print(f"API Error: No 'result' list in response for {url}. Full response: {data}")
            return []
    except requests.exceptions.RequestException as e:
        print(f"Error fetching instruments: {e}")
        return []

def get_greeks_and_oi(instrument_name):
    url = f"{BASE_URL}/public/ticker?instrument_name={instrument_name}"
    try:
        response = requests.get(url, headers=HEADERS)
        response.raise_for_status()
        result = response.json()["result"]
        if "greeks" in result and "gamma" in result["greeks"] and "open_interest" in result:
            return {
                "gamma": result["greeks"]["gamma"],
                "oi": result["open_interest"]
            }
        else:
            return None
    except requests.exceptions.RequestException as e:
        print(f"Error fetching greeks/oi for {instrument_name}: {e}")
        return None
    except KeyError:
        print(f"API Error: Unexpected JSON structure for {instrument_name}. Full response: {response.json()}")
        return None

# --- Helper Functions ---
def get_next_expiry(instruments):
    # Deribit expiries are typically 8 AM UTC
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    today_8am_utc = now_utc.replace(hour=8, minute=0, second=0, microsecond=0)
    
    if now_utc < today_8am_utc:
        # If before 8 AM UTC, next expiry is today at 8 AM UTC
        next_expiry_date_obj = today_8am_utc
    else:
        # If after 8 AM UTC, next expiry is tomorrow at 8 AM UTC
        next_expiry_date_obj = (now_utc + datetime.timedelta(days=1)).replace(hour=8, minute=0, second=0, microsecond=0)

    # Convert to milliseconds timestamp
    return int(next_expiry_date_obj.timestamp() * 1000)

def format_ts_to_label(ts_ms):
    return datetime.datetime.fromtimestamp(ts_ms / 1000, tz=datetime.timezone.utc).strftime('%d%b%y').upper()

# --- Main Logic ---
def calculate_gamma_exposure():
    print("\n" + "=" * 50)
    print("Beginning new data collection cycle...")
    
    # Get current UTC time (system time on EC2/GitHub Actions)
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    # Convert to IST for display
    now_ist = now_utc.astimezone(IST)

    print("Fetching data at", now_ist.strftime("%Y-%m-%d %H:%M:%S %Z"))

    # Check Telegram credentials at the start of the function as well
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("ERROR: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID environment variables are not set. Cannot send to Telegram.")
        print("Please set them on your EC2 instance (e.g., in crontab -e).")
        print("Cycle completed with errors.")
        return # Exit if Telegram credentials are not configured

    price = get_current_price()
    if price is None:
        print("Failed to get current BTC price. Skipping this iteration.")
        print("Cycle completed with errors.")
        return

    print(f"Current BTC Price (from BTC-PERPETUAL Mark Price): ${price:,.2f}")

    # --- Define price range for filtering ---
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

    # Filter options by expiry first
    relevant_options_all_strikes = [
        i for i in instruments
        if i["expiration_timestamp"] == target_expiry_ts
    ]

    if not relevant_options_all_strikes:
        print(f"No options found for {expiry_label} expiry. Skipping this iteration.")
        print("Cycle completed with errors.")
        return

    # Filter relevant_options by price range
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
        else: # put
            strike_map[strike]["put_gamma_sum"] += gamma_from_api * oi_from_api
            strike_map[strike]["put_oi_sum"] += oi_from_api

    net_gex_map = {}
    for strike in strike_map:
        call_total_gamma_oi = strike_map[strike]["call_gamma_sum"]
        put_total_gamma_oi = strike_map[strike]["put_gamma_sum"]
        
        net_gex = call_total_gamma_oi - put_total_gamma_oi
        
        net_gex_map[strike] = round(net_gex * 1000)

    # Sort strikes and prepare data for plotting
    sorted_strikes = sorted(net_gex_map.keys())
    gex_values = [net_gex_map[s] for s in sorted_strikes] # Corrected 'sorted_gex_map.keys()' to 'sorted_strikes'

    # Calculate Total Net GEX from the *filtered* values
    total_net_gex = sum(gex_values)
    
    # Get current local timestamp (HH:MM) - Using IST
    current_time_hhmm = now_ist.strftime('%H:%M')

    # Identify key strikes for labels (from the *filtered* map)
    max_gex_strike = None
    min_gex_strike = None
    closest_strike = None
    if net_gex_map:
        max_gex_strike = max(net_gex_map, key=net_gex_map.get)
        min_gex_strike = min(net_gex_map, key=net_gex_map.get)
        closest_strike = min(net_gex_map.keys(), key=lambda x: abs(x - price))

    # --- Console Output ---
    print(f"Net Gamma Exposure for BTC Options (Next Expiry: {expiry_label}) within ±{PRICE_RANGE_POINTS} range:")
    print(f"{'Strike':<10} | {'Net GEX (BTC)':<20}")
    print("-" * 35)

    for strike in sorted_strikes:
        label = ""
        if strike == closest_strike:
            label += " <= (Closest to price)"
        if strike == max_gex_strike:
            label += " ++++ (Max GEX)"
        if strike == min_gex_strike:
            label += " ---- (Min GEX)"

        print(f"{strike:<10.1f} | {net_gex_map[strike]:<5}  {label}")

    print("\n" + "=" * 50)
    print(f"TOTAL NET GEX (within ±{PRICE_RANGE_POINTS} range): {total_net_gex:,.0f} ")
    print(f"Generated at: {current_time_hhmm} IST")
    print("=" * 50)


    # --- Matplotlib Charting, Telegram Send & S3 Upload Logic ---
    temp_dir = "/tmp"
    output_filename = "current_gex_chart.png" # Just a temporary name for saving locally before sending
    temp_filepath = os.path.join(temp_dir, output_filename)

    try:
        print("Generating and saving chart locally...")
        plt.figure(figsize=(12, 7)) 
        
        # Use filtered sorted_strikes and gex_values for the bars
        bar_width = min(abs(sorted_strikes[i+1]-sorted_strikes[i]) for i in range(len(sorted_strikes)-1)) * 0.8 if len(sorted_strikes) > 1 else 1000
        bars = plt.bar(sorted_strikes, gex_values, width=bar_width, color='skyblue')

        # --- Data labels on bars ---
        for bar in bars:
            yval = bar.get_height()
            plt.text(bar.get_x() + bar.get_width()/2, yval, round(yval), ha='center', va='bottom', fontsize=12) 

        for i, strike in enumerate(sorted_strikes):
            if strike == max_gex_strike:
                bars[i].set_color('green')
            elif strike == min_gex_strike:
                bars[i].set_color('red')
            elif strike == closest_strike:
                bars[i].set_color('orange')

        plt.axhline(0, color='gray', linestyle='--', linewidth=0.8)
        plt.axvline(price, color='red', linestyle=':', linewidth=2, label=f'Current BTC Price (${price:,.0f})')

        plt.title(f'BTC Options Net Gamma Exposure ({expiry_label} Expiry)\n±{PRICE_RANGE_POINTS} Around Current Price', fontsize=14) 
        plt.xlabel('Strike Price', fontsize=12)
        plt.ylabel('Net Gamma Exposure (BTC Equivalent)', fontsize=12)
        plt.xticks(sorted_strikes, rotation=90, ha='right')
        plt.grid(axis='y', linestyle='--', alpha=0.7)
        plt.legend()
        plt.tight_layout()

        # --- Simplified info_text and increased font size ---
        info_text = f"gex: {total_net_gex:,.0f} at {current_time_hhmm} IST"
        plt.figtext(0.5, 0.01, info_text, ha="center", fontsize=25, bbox={"facecolor":"white", "alpha":0.8, "pad":5})

        plt.savefig(temp_filepath) # Save the plot temporarily to file
        print(f"Plot saved locally to: {temp_filepath}")
        plt.close() # Close the plot after saving to free memory

        # --- Telegram Send Logic ---
        telegram_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
        
        # Prepare the caption
        caption = (
            #f"BTC Options Net Gamma Exposure ({expiry_label} Expiry)\n"
            f"Net GEX: {total_net_gex:,.0f}"
            
        )

        with open(temp_filepath, 'rb') as photo_file:
            files = {'photo': photo_file}
            data = {'chat_id': TELEGRAM_CHAT_ID, 'caption': caption}
            print("Sending chart to Telegram...")
            response = requests.post(telegram_url, files=files, data=data)
            response.raise_for_status() # Raise an exception for HTTP errors
            
            print(f"Chart sent to Telegram. Response: {response.json()}")

        # --- S3 Upload Logic (NEW) ---
        print(f"Uploading chart to S3 bucket: {S3_BUCKET_NAME}...")
        upload_to_s3(temp_filepath, S3_BUCKET_NAME, LATEST_CHART_KEY)

    except Exception as e:
        print(f"Error generating plot, sending to Telegram, or uploading to S3: {e}")
        import traceback
        traceback.print_exc() # Print full traceback for debugging
    finally:
        # Clean up the local temporary file
        if os.path.exists(temp_filepath):
            os.remove(temp_filepath)
            print(f"Cleaned up temporary file: {temp_filepath}")

    print("Data collection cycle completed successfully.")
    print("==================================================")


# === Run Loop ===
# This script is designed to be run by cron on EC2.
# The `if __name__ == "__main__":` block ensures calculate_gamma_exposure() is called once
# when the script is executed. Cron will handle the repeated execution.
if __name__ == "__main__":
    calculate_gamma_exposure()
