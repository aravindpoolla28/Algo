mport requests
import time
import datetime
import json
import os
import matplotlib
matplotlib.use('Agg') # IMPORTANT: Use Agg backend for non-GUI environments
import matplotlib.pyplot as plt
import boto3 # Added for S3 upload
import pytz

# --- Configuration ---
BASE_URL = "https://www.deribit.com/api/v2"
HEADERS = {"Accept": "application/json"}
PRICE_RANGE_POINTS = 6000 # Define the range around the current price

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
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    today_8am_utc = now_utc.replace(hour=8, minute=0, second=0, microsecond=0)
    
    if now_utc < today_8am_utc:
        next_expiry_date_obj = today_8am_utc
    else:
        next_expiry_date_obj = (now_utc + datetime.timedelta(days=1)).replace(hour=8, minute=0, second=0, microsecond=0)

    return int(next_expiry_date_obj.timestamp() * 1000)

def format_ts_to_label(ts_ms):
    return datetime.datetime.fromtimestamp(ts_ms / 1000, tz=datetime.timezone.utc).strftime('%d%b%y').upper()

# --- Main Logic ---
def calculate_gamma_exposure():
    print("\n" + "=" * 50)
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    now_ist = now_utc.astimezone(IST)
    print("Beginning new data collection cycle...")
    print("Fetching data at", now.ist.strftime("%Y-%m-%d %H %M:%S %Z"))

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

    # --- MODIFIED: Filter relevant_options by price range ---
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
    for instr in relevant_options_filtered_by_price: # Use the filtered list here
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
    gex_values = [net_gex_map[s] for s in sorted_strikes]

    # Calculate Total Net GEX from the *filtered* values
    total_net_gex = sum(gex_values)
    
    # Get current local timestamp (HH:MM)
    current_time_hhmm = datetime.datetime.now().strftime('%H:%M')

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
    print(f"TOTAL NET GEX (within ±{PRICE_RANGE_POINTS} range): {total_net_gex:,.0f} BTC")
    print(f"Generated at: {current_time_hhmm}")
    print("=" * 50)


    # --- Matplotlib Charting & S3 Upload ---
    current_time_hhmm= now_ist.strftime('%H:%M')
    
    try:
        print("Generating and saving chart...")
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
        info_text = f"gex: {total_net_gex:,.0f} at {current_time_hhmm}"
        plt.figtext(0.5, 0.01, info_text, ha="center", fontsize=25, bbox={"facecolor":"white", "alpha":0.8, "pad":5})

        # --- S3 Upload Logic ---
        s3 = boto3.client('s3')
        # Using the same bucket as your other GEX charts
        bucket_name = 'gex-charts-mybitcoin' 

        # Generate a timestamped filename for historical record
        timestamp_str = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
        historical_filename = f"gex_chart_{timestamp_str}.png" 
        
        # Define a temporary local path for the image before upload
        temp_dir = "/tmp" # Standard temporary directory on Linux systems (including GitHub Actions runners)
        historical_temp_filepath = os.path.join(temp_dir, historical_filename)

        # Save the plot locally
        plt.savefig(historical_temp_filepath)
        print(f"Plot saved locally to: {historical_temp_filepath}")

        # Upload the timestamped file to S3
        s3.upload_file(historical_temp_filepath, bucket_name, historical_filename,
                       ExtraArgs={'ContentType': 'image/png'})
        print(f"Plot uploaded to S3 as: s3://{bucket_name}/{historical_filename}")

        # You can optionally also upload this as the 'latest' file if you want a fixed URL for the newest one
        latest_filename = "latest_gex_chart_from_action.png" # A distinct name to avoid conflict with EC2's latest
        latest_temp_filepath = os.path.join(temp_dir, latest_filename)
        plt.savefig(latest_temp_filepath) # Save it again for the 'latest' version
        s3.upload_file(latest_temp_filepath, bucket_name, latest_filename,
                       ExtraArgs={'ContentType': 'image/png'})
        print(f"Plot also uploaded as 'latest_gex_chart_from_action.png' to S3.")


        plt.close() # Close the plot to free memory

        # Clean up local temporary files
        os.remove(historical_temp_filepath) 
        os.remove(latest_temp_filepath)
        
        print("Data collection cycle completed successfully.")

    except Exception as e:
        print(f"Error generating plot or uploading to S3: {e}")
        import traceback
        traceback.print_exc()
        print("Cycle completed with errors.")

    # No "Waiting..." message needed for GitHub Actions as it exits after execution

# === Single Run Execution ===
# This script is designed to be run once per GitHub Actions job.
if __name__ == "__main__":
    calculate_gamma_exposure()

