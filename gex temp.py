import requests
import time
import datetime

BASE_URL = "https://www.deribit.com/api/v2"
HEADERS = {"Accept": "application/json"}

def get_current_price():
    url = f"{BASE_URL}/public/ticker?instrument_name=BTC-PERPETUAL"
    response = requests.get(url, headers=HEADERS)
    return float(response.json()["result"]["index_price"])

def get_instruments():
    url = f"{BASE_URL}/public/get_instruments?currency=BTC&kind=option&expired=false"
    response = requests.get(url, headers=HEADERS)
    return response.json()["result"]

def get_greeks_and_oi(instrument_name):
    url = f"{BASE_URL}/public/ticker?instrument_name={instrument_name}"
    response = requests.get(url, headers=HEADERS)
    result = response.json()["result"]
    return {
        "gamma": result["greeks"]["gamma"],
        "oi": result["open_interest"]
    }

def get_next_expiry(instruments):
    future_dates = sorted(set(instr["expiration_timestamp"] for instr in instruments))
    now_ts = int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000)
    for ts in future_dates:
        if ts > now_ts:
            return ts
    return None

def format_ts(ts):
    return datetime.datetime.fromtimestamp(ts / 1000).strftime('%d%b%y')

def calculate_gamma_exposure():
    print("\nConnected to Deribit Production API.\n")
    print("=" * 50)
    print("Fetching data at", datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    price = get_current_price()
    print(f"Current BTC Price (from BTC-PERPETUAL): ${price:,.2f}")

    instruments = get_instruments()
    expiry_ts = get_next_expiry(instruments)
    if not expiry_ts:
        print("No upcoming expiry found.")
        return

    expiry_label = format_ts(expiry_ts)
    print(f"Targeting next expiry: {expiry_label} (approx. {datetime.datetime.utcfromtimestamp(expiry_ts/1000).isoformat()})")

    relevant = [i for i in instruments if i["expiration_timestamp"] == expiry_ts]
    print(f"\nProcessing {len(relevant)} options for {expiry_label} expiry...\n")

    strike_map = {}
    for instr in relevant:
        data = get_greeks_and_oi(instr["instrument_name"])
        strike = instr["strike"]
        gamma = data["gamma"]
        oi = data["oi"]
        option_type = instr["option_type"]

        if strike not in strike_map:
            strike_map[strike] = {"call": {"gamma": 0.0, "oi": 0}, "put": {"gamma": 0.0, "oi": 0}}

        strike_map[strike][option_type]["gamma"] = gamma
        strike_map[strike][option_type]["oi"] = oi

    net_gex_map = {}
    for strike in strike_map:
        call = strike_map[strike]["call"]
        put = strike_map[strike]["put"]
        call_gex = call["oi"] * call["gamma"] * 1000
        put_gex = put["oi"] * put["gamma"] * 1000
        net_gex = round(call_gex - put_gex)
        net_gex_map[strike] = net_gex

    max_gex_strike = max(net_gex_map, key=net_gex_map.get)
    min_gex_strike = min(net_gex_map, key=net_gex_map.get)
    closest_strike = min(net_gex_map.keys(), key=lambda x: abs(x - price))

    print(f"Net Gamma Exposure for BTC Options (Next Expiry: {expiry_label}):")
    print(f"{'Strike':<10} | {'Net GEX (BTC)':<20}")
    print("-" * 35)

    for strike in sorted(net_gex_map.keys()):
        label = ""
        if strike == closest_strike:
            label += " <="
        if strike == max_gex_strike:
            label += " ++++"
        if strike == min_gex_strike:
            label += " ----"

        print(f"{strike:<10.1f} | {net_gex_map[strike]:<5}  {label}")

    print("\n" + "=" * 50)
    print("Waiting 15 minutes for next update...\n")

# === Run Loop ===
while True:
    try:
        calculate_gamma_exposure()
    except Exception as e:
        print(f"Error occurred: {e}")
    time.sleep(15 * 60)  # wait 15 minutes
