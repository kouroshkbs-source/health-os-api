#!/usr/bin/env python3
"""
Garmin → Notion Sync (Cookie-based)

Uses browser session cookies instead of OAuth (garth) to avoid the
permanent rate-limit ban on the mobile OAuth endpoint.

Secrets required:
  GARMIN_COOKIE  - Full cookie string from browser session
  GARMIN_CSRF    - Connect-Csrf-Token header value
  NOTION_TOKEN   - Notion integration token

Exit codes:
  0  - Success (or no data available yet)
  1  - Generic error (Notion down, network issue)
  41 - Auth error (cookies expired) → triggers circuit breaker
  42 - Rate limit (429) → triggers circuit breaker
"""

import os
import sys
import json
import requests
from datetime import date, timedelta

# ── Config ────────────────────────────────────────────────────────
GARMIN_COOKIE = os.environ.get("GARMIN_COOKIE", "")
GARMIN_CSRF = os.environ.get("GARMIN_CSRF", "")
NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "")
NOTION_DB_ID = "2deda7da88db811f98f5f860d49af03d"

GARMIN_BASE = "https://connect.garmin.com/gc-api"
NOTION_BASE = "https://api.notion.com/v1"


# ── Garmin API ────────────────────────────────────────────────────
def garmin_headers():
    return {
        "Cookie": GARMIN_COOKIE,
        "Connect-Csrf-Token": GARMIN_CSRF,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:149.0) "
                      "Gecko/20100101 Firefox/149.0",
        "Accept": "*/*",
        "Accept-Language": "fr,fr-FR;q=0.9,en-US;q=0.8,en;q=0.7",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "Connection": "keep-alive",
    }


def garmin_get(url, label="API"):
    """GET with auth error handling — exits on 401/403/429."""
    try:
        r = requests.get(url, headers=garmin_headers(), timeout=20)
    except requests.RequestException as e:
        print(f"   ⚠️ {label} network error: {e}")
        return None

    if r.status_code in (401, 403):
        # Check if it's an HTML redirect to login (JWT expired)
        if r.text.strip().startswith("<!DOCTYPE") or "Sign In" in r.text:
            print(f"   ❌ {label}: Auth error — cookies/JWT expired (got login page)")
            sys.exit(41)
        # Some gc-api endpoints return 403 normally (e.g. Heart Rate)
        print(f"   ⚠️ {label}: {r.status_code} (may be normal for this endpoint)")
        return None

    if r.status_code == 429:
        print(f"   ❌ {label}: Rate limited (429)")
        sys.exit(42)

    if r.status_code != 200:
        print(f"   ⚠️ {label}: HTTP {r.status_code}")
        return None

    # Check for HTML login redirect on 200
    body = r.text[:200]
    if body.strip().startswith("<!DOCTYPE") or "Sign In" in body:
        print(f"   ❌ {label}: Got login page on 200 — session expired")
        sys.exit(41)

    try:
        return r.json()
    except json.JSONDecodeError:
        print(f"   ⚠️ {label}: Invalid JSON response")
        return None


# ── Data fetching ─────────────────────────────────────────────────
def fetch_sleep(target_date):
    url = (f"{GARMIN_BASE}/sleep-service/sleep/dailySleepData"
           f"?date={target_date}&nonSleepBufferMinutes=60")
    return garmin_get(url, "Sleep")


def fetch_body_battery(target_date):
    prev = (date.fromisoformat(target_date) - timedelta(days=1)).isoformat()
    url = (f"{GARMIN_BASE}/wellness-service/wellness/bodyBattery"
           f"/reports/daily?startDate={prev}&endDate={target_date}")
    return garmin_get(url, "Body Battery")


def fetch_steps(target_date):
    url = (f"{GARMIN_BASE}/usersummary-service/stats/steps"
           f"/daily/{target_date}/{target_date}")
    return garmin_get(url, "Steps")


def fetch_weight(target_date):
    start = (date.fromisoformat(target_date) - timedelta(days=7)).isoformat()
    url = (f"{GARMIN_BASE}/weight-service/weight"
           f"/dateRange?startDate={start}&endDate={target_date}")
    return garmin_get(url, "Weight")


# ── Parsing ───────────────────────────────────────────────────────
def extract_sleep_score(sleep_data):
    """Extract sleep score from various possible JSON locations."""
    if not sleep_data or "dailySleepDTO" not in sleep_data:
        return 0

    dto = sleep_data["dailySleepDTO"]

    # Try multiple known locations for sleep score
    # Location 1: sleepScores.overall.value
    scores = dto.get("sleepScores", {})
    if scores:
        overall = scores.get("overall", scores.get("overallScore", {}))
        if isinstance(overall, dict) and "value" in overall:
            return overall["value"]
        # Location 2: sleepScores.qualityScore.value
        quality = scores.get("qualityScore", {})
        if isinstance(quality, dict) and "value" in quality:
            return quality["value"]

    # Location 3: top-level sleepScore
    if "sleepScore" in dto:
        return dto["sleepScore"]

    # Location 4: overallScore at top level
    if "overallScore" in dto:
        val = dto["overallScore"]
        return val.get("value", val) if isinstance(val, dict) else val

    return 0


def parse_garmin_data(target_date):
    """Fetch and parse all Garmin data for a given date."""
    data = {
        "sleep_score": 0,
        "sleep_duration": "0h 00",
        "body_battery": 0,
        "steps": 0,
        "weight": 0.0,
    }

    # ── Sleep ──
    sleep = fetch_sleep(target_date)
    if sleep and "dailySleepDTO" in sleep:
        dto = sleep["dailySleepDTO"]
        seconds = dto.get("sleepTimeSeconds", 0)
        if seconds > 0:
            hours = seconds // 3600
            minutes = (seconds % 3600) // 60
            data["sleep_duration"] = f"{hours}h {minutes:02d}"
        data["sleep_score"] = extract_sleep_score(sleep)

    # ── Body Battery ──
    bb = fetch_body_battery(target_date)
    if bb and isinstance(bb, list):
        # Find entry for target date
        for entry in bb:
            if entry.get("date") == target_date:
                data["body_battery"] = entry.get("charged", 0)
                break
        # Fallback: use last entry
        if data["body_battery"] == 0 and bb:
            data["body_battery"] = bb[-1].get("charged", 0)

    # ── Steps ──
    steps = fetch_steps(target_date)
    if steps and isinstance(steps, list):
        for entry in steps:
            if entry.get("calendarDate") == target_date:
                data["steps"] = entry.get("totalSteps", 0)
                break

    # ── Weight ──
    weight = fetch_weight(target_date)
    if weight and "dateWeightList" in weight:
        wl = weight["dateWeightList"]
        if wl:
            # Weight from API is in grams
            w = wl[-1].get("weight", 0)
            if w > 1000:  # sanity check: must be in grams
                data["weight"] = round(w / 1000, 1)
            elif w > 0:
                data["weight"] = round(w, 1)

    return data


# ── Notion API ────────────────────────────────────────────────────
def notion_headers():
    return {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json",
    }


def find_journal_entry(target_date):
    """Find existing journal entry for the given date. Returns page_id or None."""
    url = f"{NOTION_BASE}/databases/{NOTION_DB_ID}/query"
    payload = {
        "filter": {
            "property": "Date",
            "date": {"equals": target_date}
        }
    }
    try:
        r = requests.post(url, headers=notion_headers(), json=payload, timeout=15)
        if r.status_code != 200:
            print(f"   ⚠️ Notion query error: {r.status_code}")
            return None
        results = r.json().get("results", [])
        return results[0]["id"] if results else None
    except requests.RequestException as e:
        print(f"   ⚠️ Notion query network error: {e}")
        return None


def update_notion_entry(page_id, data):
    """Update existing Notion page with Garmin data."""
    url = f"{NOTION_BASE}/pages/{page_id}"
    properties = {}

    # Only update non-zero values to avoid overwriting existing data
    if data["sleep_score"] > 0:
        properties["Sleep score"] = {"number": data["sleep_score"]}
    if data["sleep_duration"] != "0h 00":
        properties["Sleep duration"] = {
            "rich_text": [{"text": {"content": data["sleep_duration"]}}]
        }
    if data["body_battery"] > 0:
        properties["Body battery"] = {"number": data["body_battery"]}
    if data["steps"] > 0:
        properties["Steps"] = {"number": data["steps"]}
    if data["weight"] > 0:
        properties["Weight"] = {"number": data["weight"]}

    if not properties:
        print("   ⚠️ No non-zero data to update")
        return True

    try:
        r = requests.patch(url, headers=notion_headers(),
                           json={"properties": properties}, timeout=15)
        if r.status_code != 200:
            print(f"   ⚠️ Notion update error: {r.status_code} — {r.text[:200]}")
            return False
        return True
    except requests.RequestException as e:
        print(f"   ⚠️ Notion update network error: {e}")
        return False


def create_notion_entry(target_date, data):
    """Create new Notion page with Garmin data."""
    url = f"{NOTION_BASE}/pages"
    properties = {
        " ": {"title": [{"text": {"content": ""}}]},
        "Date": {"date": {"start": target_date}},
    }

    if data["sleep_score"] > 0:
        properties["Sleep score"] = {"number": data["sleep_score"]}
    if data["sleep_duration"] != "0h 00":
        properties["Sleep duration"] = {
            "rich_text": [{"text": {"content": data["sleep_duration"]}}]
        }
    if data["body_battery"] > 0:
        properties["Body battery"] = {"number": data["body_battery"]}
    if data["steps"] > 0:
        properties["Steps"] = {"number": data["steps"]}
    if data["weight"] > 0:
        properties["Weight"] = {"number": data["weight"]}

    payload = {
        "parent": {"database_id": NOTION_DB_ID},
        "properties": properties,
    }
    try:
        r = requests.post(url, headers=notion_headers(),
                          json=payload, timeout=15)
        if r.status_code != 200:
            print(f"   ⚠️ Notion create error: {r.status_code} — {r.text[:200]}")
            return False
        return True
    except requests.RequestException as e:
        print(f"   ⚠️ Notion create network error: {e}")
        return False


# ── Main ──────────────────────────────────────────────────────────
def main():
    print("🚀 Garmin → Notion Sync (Cookie-based)")
    print("=" * 50)

    # Validate
    if not GARMIN_COOKIE:
        print("❌ GARMIN_COOKIE not set"); sys.exit(1)
    if not GARMIN_CSRF:
        print("❌ GARMIN_CSRF not set"); sys.exit(1)
    if not NOTION_TOKEN:
        print("❌ NOTION_TOKEN not set"); sys.exit(1)

    target = date.today().isoformat()
    print(f"📊 Fetching Garmin data for {target}...")

    data = parse_garmin_data(target)

    print(f"\n{'='*50}")
    print(f"📋 GARMIN DATA:")
    print(f"   Date: {target}")
    print(f"   Sleep Score: {data['sleep_score']}")
    print(f"   Sleep Duration: {data['sleep_duration']}")
    print(f"   Body Battery: {data['body_battery']}")
    print(f"   Steps: {data['steps']}")
    print(f"   Weight: {data['weight']} kg")
    print(f"{'='*50}")

    # All zeros = no data yet (normal before watch sync)
    if (data["sleep_score"] == 0 and data["steps"] == 0
            and data["body_battery"] == 0):
        print("\n⚠️ No Garmin data retrieved (all zeros)")
        print("   This might be normal if run before syncing your watch")
        sys.exit(0)

    # Upsert to Notion
    page_id = find_journal_entry(target)
    if page_id:
        print(f"\n📝 Updating existing entry...")
        ok = update_notion_entry(page_id, data)
    else:
        print(f"\n📝 Creating new entry...")
        ok = create_notion_entry(target, data)

    if ok:
        print("✅ Notion updated successfully!")
    else:
        print("❌ Failed to update Notion")
        sys.exit(1)


if __name__ == "__main__":
    main()

