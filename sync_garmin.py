#!/usr/bin/env python3
"""
Garmin → Notion Sync Script for GitHub Actions.
Fetches health data from Garmin and writes it to Notion Journal database.
"""

import os
import json
import tempfile
import httpx
from datetime import date, timedelta

from garminconnect import Garmin

# ============================================
# ENVIRONMENT VARIABLES
# ============================================
GARMIN_OAUTH_TOKEN = os.getenv("GARMIN_OAUTH_TOKEN")
GARMIN_DISPLAY_NAME = os.getenv("GARMIN_DISPLAY_NAME")
NOTION_TOKEN = os.getenv("NOTION_TOKEN")

# Notion Database ID (Journal)
NOTION_DATABASE_ID = "2deda7da-88db-811f-98f5-f860d49af03d"
NOTION_API_URL = "https://api.notion.com/v1"

# ============================================
# GARMIN FUNCTIONS
# ============================================

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


def fetch_garmin_data(client, target_date: str) -> dict:
    """Fetch health metrics for a specific date."""
    print(f"\n📊 Fetching Garmin data for {target_date}...")
    
    results = {
        "date": target_date,
        "bodyBattery": 0,
        "sleepScore": 0,
        "sleepDuration": "0h 00",
        "sleepSeconds": 0,
        "weight": 0.0,
        "weightChange": 0.0,
        "steps": 0,
    }
    
    # Body Battery
    try:
        bb_data = client.get_body_battery(target_date)
        print(f"   Body Battery raw: {type(bb_data)} - {len(bb_data) if isinstance(bb_data, list) else 'N/A'} items")
        if bb_data and isinstance(bb_data, list) and len(bb_data) > 0:
            # Get the max charged value of the day
            max_bb = 0
            for item in bb_data:
                if isinstance(item, dict):
                    val = item.get("chargedValue", 0) or item.get("charged", 0) or item.get("bodyBatteryLevel", 0) or 0
                    if val > max_bb:
                        max_bb = val
            results["bodyBattery"] = max_bb
        print(f"   ✓ Body Battery: {results['bodyBattery']}")
    except Exception as e:
        print(f"   ❌ Body Battery error: {e}")
    
    # Sleep
    try:
        sleep_data = client.get_sleep_data(target_date)
        print(f"   Sleep raw type: {type(sleep_data)}")
        if sleep_data and isinstance(sleep_data, dict):
            daily_sleep = sleep_data.get("dailySleepDTO", {})
            
            # Sleep score
            sleep_scores = daily_sleep.get("sleepScores", {})
            if sleep_scores:
                overall = sleep_scores.get("overall", {})
                results["sleepScore"] = overall.get("value", 0) if isinstance(overall, dict) else 0
            
            # Sleep duration
            sleep_seconds = daily_sleep.get("sleepTimeSeconds", 0)
            if sleep_seconds:
                results["sleepSeconds"] = sleep_seconds
                hours = sleep_seconds // 3600
                minutes = (sleep_seconds % 3600) // 60
                results["sleepDuration"] = f"{hours}h {minutes:02d}"
        
        print(f"   ✓ Sleep: {results['sleepDuration']}, Score: {results['sleepScore']}")
    except Exception as e:
        print(f"   ❌ Sleep error: {e}")
    
    # Weight (look back 7 days to find latest)
    try:
        start_date = (date.fromisoformat(target_date) - timedelta(days=7)).isoformat()
        weight_data = client.get_body_composition(start_date, target_date)
        print(f"   Weight raw type: {type(weight_data)}")
        
        if weight_data and isinstance(weight_data, dict):
            weights = weight_data.get("dateWeightList", []) or weight_data.get("weightList", [])
            if weights and len(weights) > 0:
                latest = weights[-1]
                weight_grams = latest.get("weight", 0)
                if weight_grams > 0:
                    results["weight"] = round(weight_grams / 1000, 1)
                    
                    # Calculate change from first weight in period
                    if len(weights) > 1:
                        first_weight = weights[0].get("weight", 0) / 1000
                        results["weightChange"] = round(results["weight"] - first_weight, 1)
        
        print(f"   ✓ Weight: {results['weight']} kg (change: {results['weightChange']})")
    except Exception as e:
        print(f"   ❌ Weight error: {e}")
    
    # Steps
    try:
        steps_data = client.get_steps_data(target_date)
        print(f"   Steps raw type: {type(steps_data)}")
        if steps_data:
            if isinstance(steps_data, list) and len(steps_data) > 0:
                # Sum all steps from the day
                total_steps = 0
                for item in steps_data:
                    if isinstance(item, dict):
                        total_steps += item.get("steps", 0) or 0
                results["steps"] = total_steps
            elif isinstance(steps_data, dict):
                results["steps"] = steps_data.get("totalSteps", 0) or steps_data.get("steps", 0) or 0
        print(f"   ✓ Steps: {results['steps']}")
    except Exception as e:
        print(f"   ❌ Steps error: {e}")
    
    return results


# ============================================
# NOTION FUNCTIONS
# ============================================

def notion_headers():
    """Get headers for Notion API requests."""
    return {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28"
    }


def find_journal_entry(target_date: str) -> dict | None:
    """Find existing Journal entry for a specific date."""
    print(f"\n🔍 Searching for Journal entry on {target_date}...")
    
    url = f"{NOTION_API_URL}/databases/{NOTION_DATABASE_ID}/query"
    
    payload = {
        "filter": {
            "property": "Date",
            "date": {
                "equals": target_date
            }
        }
    }
    
    response = httpx.post(url, headers=notion_headers(), json=payload)
    
    if response.status_code == 200:
        data = response.json()
        results = data.get("results", [])
        if results:
            print(f"   ✓ Found existing entry: {results[0]['id']}")
            return results[0]
        else:
            print(f"   ℹ️ No entry found for {target_date}")
            return None
    else:
        print(f"   ❌ Notion query error: {response.status_code} - {response.text}")
        return None


def create_journal_entry(target_date: str, garmin_data: dict) -> dict | None:
    """Create a new Journal entry with Garmin data."""
    print(f"\n📝 Creating new Journal entry for {target_date}...")
    
    url = f"{NOTION_API_URL}/pages"
    
    # Build properties - only include non-zero values
    properties = {
        "Date": {
            "date": {"start": target_date}
        },
    }
    
    if garmin_data["weight"] > 0:
        properties["Weight"] = {"number": garmin_data["weight"]}
    
    if garmin_data["sleepScore"] > 0:
        properties["Sleep score"] = {"number": garmin_data["sleepScore"]}
    
    if garmin_data["bodyBattery"] > 0:
        properties["Body battery"] = {"number": garmin_data["bodyBattery"]}
    
    if garmin_data["sleepDuration"] != "0h 00":
        properties["Sleep duration"] = {"rich_text": [{"text": {"content": garmin_data["sleepDuration"]}}]}
    
    if garmin_data.get("steps", 0) > 0:
        properties["Steps"] = {"number": garmin_data["steps"]}
    
    payload = {
        "parent": {"database_id": NOTION_DATABASE_ID},
        "properties": properties
    }
    
    response = httpx.post(url, headers=notion_headers(), json=payload)
    
    if response.status_code == 200:
        data = response.json()
        print(f"   ✓ Created entry: {data['id']}")
        return data
    else:
        print(f"   ❌ Notion create error: {response.status_code} - {response.text}")
        return None


def update_journal_entry(page_id: str, garmin_data: dict) -> bool:
    """Update existing Journal entry with Garmin data."""
    print(f"\n📝 Updating Journal entry {page_id}...")
    
    url = f"{NOTION_API_URL}/pages/{page_id}"
    
    # Build properties - only include non-zero values
    properties = {}
    
    if garmin_data["weight"] > 0:
        properties["Weight"] = {"number": garmin_data["weight"]}
    
    if garmin_data["sleepScore"] > 0:
        properties["Sleep score"] = {"number": garmin_data["sleepScore"]}
    
    if garmin_data["bodyBattery"] > 0:
        properties["Body battery"] = {"number": garmin_data["bodyBattery"]}
    
    if garmin_data["sleepDuration"] != "0h 00":
        properties["Sleep duration"] = {"rich_text": [{"text": {"content": garmin_data["sleepDuration"]}}]}
    
    if garmin_data.get("steps", 0) > 0:
        properties["Steps"] = {"number": garmin_data["steps"]}
    
    if not properties:
        print("   ℹ️ No Garmin data to update")
        return True
    
    payload = {"properties": properties}
    
    response = httpx.patch(url, headers=notion_headers(), json=payload)
    
    if response.status_code == 200:
        print(f"   ✓ Updated successfully")
        return True
    else:
        print(f"   ❌ Notion update error: {response.status_code} - {response.text}")
        return False


def sync_to_notion(garmin_data: dict) -> bool:
    """Sync Garmin data to Notion Journal."""
    target_date = garmin_data["date"]
    
    # Check if entry exists
    existing = find_journal_entry(target_date)
    
    if existing:
        # Update existing entry
        return update_journal_entry(existing["id"], garmin_data)
    else:
        # Create new entry if we have ANY Garmin data
        has_data = (
            garmin_data["weight"] > 0 or 
            garmin_data["sleepScore"] > 0 or 
            garmin_data["bodyBattery"] > 0 or
            garmin_data.get("steps", 0) > 0
        )
        if has_data:
            result = create_journal_entry(target_date, garmin_data)
            return result is not None
        else:
            print("   ℹ️ No Garmin data available, skipping entry creation")
            return True


# ============================================
# MAIN
# ============================================

def main():
    """Main entry point."""
    print("🚀 GitHub Actions - Garmin → Notion Sync")
    print("=" * 50)
    
    # Validate environment
    if not NOTION_TOKEN:
        raise Exception("NOTION_TOKEN not set")
    
    # Get today's date
    today = date.today().isoformat()
    
    try:
        # Initialize Garmin client
        client = get_garmin_client()
        print("✅ Garmin client initialized")
        
        # Fetch data for today
        garmin_data = fetch_garmin_data(client, today)
        
        print("\n" + "=" * 50)
        print("📋 GARMIN DATA:")
        print(f"   Date: {garmin_data['date']}")
        print(f"   Body Battery: {garmin_data['bodyBattery']}")
        print(f"   Sleep Score: {garmin_data['sleepScore']}")
        print(f"   Sleep Duration: {garmin_data['sleepDuration']}")
        print(f"   Weight: {garmin_data['weight']} kg")
        print(f"   Steps: {garmin_data.get('steps', 0)}")
        print("=" * 50)
        
        # Sync to Notion
        if garmin_data["weight"] > 0 or garmin_data["bodyBattery"] > 0 or garmin_data["sleepScore"] > 0 or garmin_data.get("steps", 0) > 0:
            print("\n📤 Syncing to Notion...")
            success = sync_to_notion(garmin_data)
            
            if success:
                print("\n✅ SUCCESS! Garmin data synced to Notion!")
            else:
                print("\n❌ FAILED to sync to Notion")
                raise Exception("Notion sync failed")
        else:
            print("\n⚠️ No Garmin data retrieved (all zeros)")
            print("   This might be normal if run before syncing your watch")
        
    except Exception as e:
        print(f"\n❌ ERROR: {e}")
        raise


if __name__ == "__main__":
    main()
