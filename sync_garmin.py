#!/usr/bin/env python3
"""
Standalone Garmin sync script for GitHub Actions.
Fetches health data and prints it (can be extended to push to Notion).
"""

import os
import json
import tempfile
from datetime import date, timedelta

from garminconnect import Garmin

# Environment variables
GARMIN_OAUTH_TOKEN = os.getenv("GARMIN_OAUTH_TOKEN")
GARMIN_DISPLAY_NAME = os.getenv("GARMIN_DISPLAY_NAME")


def get_garmin_client():
    """Initialize Garmin client with OAuth tokens."""
    if not GARMIN_OAUTH_TOKEN:
        raise Exception("GARMIN_OAUTH_TOKEN not set")
    
    token_data = json.loads(GARMIN_OAUTH_TOKEN)
    
    # Create temp directory for token files
    temp_dir = tempfile.mkdtemp()
    
    # Write oauth1 token
    oauth1_file = os.path.join(temp_dir, "oauth1_token.json")
    oauth1_data = token_data.get("oauth1_token", {})
    with open(oauth1_file, 'w') as f:
        json.dump(oauth1_data, f)
    
    # Write oauth2 token
    oauth2_file = os.path.join(temp_dir, "oauth2_token.json")
    oauth2_data = token_data.get("oauth2_token", {})
    with open(oauth2_file, 'w') as f:
        json.dump(oauth2_data, f)
    
    # Initialize client and load tokens
    client = Garmin()
    client.garth.load(temp_dir)
    
    # Force display_name (bypass profile fetch)
    if GARMIN_DISPLAY_NAME:
        print(f"✅ Using GARMIN_DISPLAY_NAME: {GARMIN_DISPLAY_NAME}")
        client.display_name = GARMIN_DISPLAY_NAME
        client.full_name = GARMIN_DISPLAY_NAME
    else:
        raise Exception("GARMIN_DISPLAY_NAME not set")
    
    return client


def fetch_garmin_data(client, target_date: str):
    """Fetch health metrics for a specific date."""
    print(f"\n📊 Fetching Garmin data for {target_date}...")
    
    results = {
        "date": target_date,
        "bodyBattery": 0,
        "sleepScore": 0,
        "sleepDuration": "0h 00",
        "weight": 0.0,
    }
    
    # Body Battery
    try:
        bb_data = client.get_body_battery(target_date)
        print(f"   Body Battery raw: {type(bb_data)} - {bb_data[:100] if isinstance(bb_data, list) and bb_data else bb_data}")
        if bb_data and isinstance(bb_data, list) and len(bb_data) > 0:
            # Get the max charged value of the day
            charged_values = [item.get("charged", 0) for item in bb_data if isinstance(item, dict)]
            results["bodyBattery"] = max(charged_values) if charged_values else 0
    except Exception as e:
        print(f"   ❌ Body Battery error: {e}")
    
    # Sleep
    try:
        sleep_data = client.get_sleep_data(target_date)
        print(f"   Sleep raw type: {type(sleep_data)}")
        if sleep_data and isinstance(sleep_data, dict):
            daily_sleep = sleep_data.get("dailySleepDTO", {})
            results["sleepScore"] = daily_sleep.get("sleepScores", {}).get("overall", {}).get("value", 0)
            sleep_seconds = daily_sleep.get("sleepTimeSeconds", 0)
            hours = sleep_seconds // 3600
            minutes = (sleep_seconds % 3600) // 60
            results["sleepDuration"] = f"{hours}h {minutes:02d}"
    except Exception as e:
        print(f"   ❌ Sleep error: {e}")
    
    # Weight
    try:
        weight_data = client.get_body_composition(target_date)
        print(f"   Weight raw type: {type(weight_data)}")
        if weight_data and isinstance(weight_data, dict):
            weight_grams = weight_data.get("weight", 0)
            results["weight"] = round(weight_grams / 1000, 1) if weight_grams else 0.0
    except Exception as e:
        print(f"   ❌ Weight error: {e}")
    
    return results


def main():
    """Main entry point."""
    print("🚀 GitHub Actions - Garmin Health Sync")
    print("=" * 50)
    
    # Get today and yesterday
    today = date.today().isoformat()
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    
    try:
        # Initialize client
        client = get_garmin_client()
        print("✅ Garmin client initialized")
        
        # Fetch data for yesterday (more likely to have complete data)
        results = fetch_garmin_data(client, yesterday)
        
        print("\n" + "=" * 50)
        print("📋 RESULTS:")
        print(f"   Date: {results['date']}")
        print(f"   Body Battery: {results['bodyBattery']}")
        print(f"   Sleep Score: {results['sleepScore']}")
        print(f"   Sleep Duration: {results['sleepDuration']}")
        print(f"   Weight: {results['weight']} kg")
        print("=" * 50)
        
        # Check if we got real data
        if results["bodyBattery"] > 0 or results["sleepScore"] > 0:
            print("\n✅ SUCCESS! Garmin data retrieved!")
        else:
            print("\n⚠️ Data still empty - GitHub Actions might also be blocked")
            print("   Try running again or check token validity")
        
    except Exception as e:
        print(f"\n❌ ERROR: {e}")
        raise


if __name__ == "__main__":
    main()
