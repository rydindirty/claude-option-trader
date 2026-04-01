"""
Get Greeks - Tradier implementation
Greeks are already embedded in chains.json from step 02 (greeks=true).
This step extracts them and writes chains_with_greeks.json in the same
format downstream steps expect.
"""
import json
import sys
import os
from datetime import datetime

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def extract_greeks_from_chains():
    print("=" * 60)
    print("STEP 04: Extract Greeks from Chains (Tradier)")
    print("=" * 60)

    # Load chains produced by step 02 which already include greeks
    try:
        with open("data/chains.json", "r") as f:
            chains_data = json.load(f)
    except FileNotFoundError:
        print("❌ data/chains.json not found — run step 02 first")
        sys.exit(1)

    all_symbols = 0
    greeks_found = 0

    # Walk the chain structure and embed greeks at each strike location
    for ticker, expirations in chains_data["chains"].items():
        for exp_data in expirations:
            for strike in exp_data["strikes"]:

                # Extract call greeks from the greeks sub-object
                if strike.get("call_symbol"):
                    all_symbols += 1
                    greeks = strike.get("call_greeks") or {}
                    if not greeks:
                        # greeks may be embedded directly on the option object
                        # from the Tradier response - check alternate locations
                        greeks = strike.get("greeks") or {}
                    if greeks:
                        strike["call_greeks"] = {
                            "iv":    round(float(greeks.get("iv") or 0), 4),
                            "delta": round(float(greeks.get("delta") or 0), 4),
                            "gamma": round(float(greeks.get("gamma") or 0), 6),
                            "theta": round(float(greeks.get("theta") or 0), 4),
                            "vega":  round(float(greeks.get("vega") or 0), 4)
                        }
                        greeks_found += 1

                # Extract put greeks
                if strike.get("put_symbol"):
                    all_symbols += 1
                    greeks = strike.get("put_greeks") or {}
                    if not greeks:
                        greeks = strike.get("greeks") or {}
                    if greeks:
                        strike["put_greeks"] = {
                            "iv":    round(float(greeks.get("iv") or 0), 4),
                            "delta": round(float(greeks.get("delta") or 0), 4),
                            "gamma": round(float(greeks.get("gamma") or 0), 6),
                            "theta": round(float(greeks.get("theta") or 0), 4),
                            "vega":  round(float(greeks.get("vega") or 0), 4)
                        }
                        greeks_found += 1

    coverage = round(greeks_found / all_symbols * 100, 1) if all_symbols else 0

    # Write chains_with_greeks.json in the same format the original produced
    output = {
        "timestamp": datetime.now().isoformat(),
        "total_options": all_symbols,
        "greeks_collected": greeks_found,
        "coverage": coverage,
        "chains_with_greeks": chains_data["chains"]
    }

    with open("data/chains_with_greeks.json", "w") as f:
        json.dump(output, f, indent=2)

    print(f"\n✅ Greeks extracted: {greeks_found}/{all_symbols} ({coverage}%)")
    print(f"   Saved to: chains_with_greeks.json")
    print("✅ Step 04 complete")

def main():
    extract_greeks_from_chains()

if __name__ == "__main__":
    main()
