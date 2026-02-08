"""
Health OS API v4.3.0 - Added Quick Notes endpoints
- Garmin: Token-based authentication (generate tokens locally first)
- YAZIO: OAuth v15 for auth + individual food items with full macros
- Quick Notes: CRUD for Notion-backed notes widget
- FIX: YAZIO nutrients are PER GRAM, not per 100g

Deploy on Railway
"""

import os
import json
import logging
import tempfile
from datetime import datetime, date, timedelta
from typing import Optional
import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ============================================
# CONFIGURATION
# ============================================

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Health OS API",
    description="Aggregates health data from Garmin Connect and YAZIO",
    version="4.3.0"
)

# CORS for widget access
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ============================================
# ENVIRONMENT VARIABLES
# ============================================

# Garmin OAuth token (JSON string from local auth script)
GARMIN_OAUTH_TOKEN = os.getenv("GARMIN_OAUTH_TOKEN")
# Garmin Display Name (bypass for server environments where profile API returns empty)
GARMIN_DISPLAY_NAME = os.getenv("GARMIN_DISPLAY_NAME")

# YAZIO credentials
YAZIO_EMAIL = os.getenv("YAZIO_EMAIL")
YAZIO_PASSWORD = os.getenv("YAZIO_PASSWORD")

# YAZIO Client credentials
YAZIO_BASE_URL = "https://yzapi.yazio.com/v15"
YAZIO_CLIENT_ID = "1_4hiybetvfksgw40o0sog4s884kwc840wwso8go4k8c04goo4c"
YAZIO_CLIENT_SECRET = "6rok2m65xuskgkgogw40wkkk8sw0osg84s8cggsc4woos4s8o"

# Notion API (for Quick Notes)
NOTION_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"

# Token storage
garmin_client = None
yazio_token: Optional[dict] = None


# ============================================
# PYDANTIC MODELS
# ============================================

class NoteCreate(BaseModel):
    note: str
    course: Optional[str] = "General"

class NoteToggle(BaseModel):
    done: bool


# ============================================
# NOTION HELPER
# ============================================

def get_notion_headers() -> dict:
    """Return headers for Notion API calls."""
    token = os.getenv("NOTION_TOKEN")
    if not token:
        raise HTTPException(status_code=500, detail="NOTION_TOKEN not configured")
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


# ============================================
# GARMIN FUNCTIONS
# ============================================

def init_garmin():
    """Initialize Garmin client with stored OAuth tokens."""
    global garmin_client
    
    if not GARMIN_OAUTH_TOKEN:
        logger.warning("GARMIN_OAUTH_TOKEN not set")
        return None
    
    try:
        from garminconnect import Garmin
        
        logger.info("Initializing Garmin client with OAuth tokens...")
        token_data = json.loads(GARMIN_OAUTH_TOKEN)
        
        # Create temp directory for token files
        temp_dir = tempfile.mkdtemp()
        
        # Write oauth1 token
        oauth1_file = os.path.join(temp_dir, "oauth1_token.json")
        with open(oauth1_file, 'w') as f:
            json.dump(token_data.get("oauth1_token", {}), f)
        
        # Write oauth2 token
        oauth2_file = os.path.join(temp_dir, "oauth2_token.json")
        with open(oauth2_file, 'w') as f:
            json.dump(token_data.get("oauth2_token", {}), f)
        
        # Initialize client and load tokens
        client = Garmin()
        client.garth.load(temp_dir)
        
        # Force display_name (bypass profile fetch that returns empty on Railway)
        if GARMIN_DISPLAY_NAME:
            client.display_name = GARMIN_DISPLAY_NAME
            client.full_name = GARMIN_DISPLAY_NAME
        
        logger.info(f"Garmin connected as: {client.display_name}")
        garmin_client = client
        return client
        
    except Exception as e:
        logger.error(f"Garmin init failed: {e}")
        return None


def get_garmin_data(target_date: str = None) -> dict:
    """Fetch Garmin health data for a specific date."""
    global garmin_client
    
    if not garmin_client:
        garmin_client = init_garmin()
    
    if not target_date:
        target_date = date.today().isoformat()
    
    results = {
        "bodyBattery": 0,
        "sleepScore": 0,
        "sleepDuration": "0h 00",
        "weight": 0.0,
        "weightChange": 0.0,
    }
    
    if not garmin_client:
        return results
    
    # Body Battery
    try:
        bb_data = garmin_client.get_body_battery(target_date)
        logger.info(f"Garmin Body Battery raw response: {type(bb_data)}")
        if bb_data and isinstance(bb_data, list) and len(bb_data) > 0:
            max_bb = 0
            for item in bb_data:
                if isinstance(item, dict):
                    val = item.get("chargedValue", 0) or item.get("charged", 0) or item.get("bodyBatteryLevel", 0) or 0
                    if val > max_bb:
                        max_bb = val
            results["bodyBattery"] = max_bb
        logger.info(f"Body Battery: {results['bodyBattery']}")
    except Exception as e:
        logger.error(f"Body Battery error: {e}")
    
    # Sleep
    try:
        sleep_data = garmin_client.get_sleep_data(target_date)
        logger.info(f"Garmin Sleep raw response keys: {type(sleep_data)}")
        if sleep_data and isinstance(sleep_data, dict):
            daily_sleep = sleep_data.get("dailySleepDTO", {})
            
            # Sleep duration
            sleep_seconds = daily_sleep.get("sleepTimeSeconds", 0) or 0
            if sleep_seconds > 0:
                hours = int(sleep_seconds // 3600)
                minutes = int((sleep_seconds % 3600) // 60)
                results["sleepDuration"] = f"{hours}h {minutes:02d}"
            
            # Sleep score
            results["sleepScore"] = daily_sleep.get("sleepScores", {}).get("overall", {}).get("value", 0) or 0
        
        logger.info(f"Sleep: {results['sleepDuration']}, Score: {results['sleepScore']}")
    except Exception as e:
        logger.error(f"Sleep error: {e}")
    
    # Weight
    try:
        weight_data = garmin_client.get_body_composition(target_date)
        logger.info(f"Garmin Weight raw response: {type(weight_data)}")
        if weight_data and isinstance(weight_data, dict):
            weight_list = weight_data.get("dateWeightList", [])
            if weight_list and len(weight_list) > 0:
                latest = weight_list[-1]
                weight_grams = latest.get("weight", 0) or 0
                if weight_grams > 0:
                    results["weight"] = round(weight_grams / 1000, 1)
        logger.info(f"Weight: {results['weight']} kg")
    except Exception as e:
        logger.error(f"Weight error: {e}")
    
    return results


# ============================================
# YAZIO FUNCTIONS
# ============================================

async def yazio_login() -> Optional[str]:
    """Login to YAZIO and return access token."""
    global yazio_token
    
    if not YAZIO_EMAIL or not YAZIO_PASSWORD:
        logger.warning("YAZIO credentials not set")
        return None
    
    logger.info(f"Logging into YAZIO with {YAZIO_EMAIL}...")
    
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            resp = await client.post(
                f"{YAZIO_BASE_URL}/oauth/token",
                data={
                    "grant_type": "password",
                    "username": YAZIO_EMAIL,
                    "password": YAZIO_PASSWORD,
                    "client_id": YAZIO_CLIENT_ID,
                    "client_secret": YAZIO_CLIENT_SECRET,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"}
            )
            logger.info(f"YAZIO login response status: {resp.status_code}")
            
            if resp.status_code == 200:
                yazio_token = resp.json()
                logger.info("YAZIO login successful!")
                return yazio_token.get("access_token")
            else:
                logger.error(f"YAZIO login failed: {resp.text}")
                return None
        except Exception as e:
            logger.error(f"YAZIO login error: {e}")
            return None


async def get_yazio_daily(date_str: str) -> dict:
    """Get YAZIO daily summary (totals only)."""
    token = await yazio_login()
    if not token:
        return {}
    
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json"
    }
    
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            url = f"{YAZIO_BASE_URL}/user/widgets/daily-summary?date={date_str}"
            logger.info(f"YAZIO endpoint: {url}")
            resp = await client.get(url, headers=headers)
            logger.info(f"YAZIO daily-summary response status: {resp.status_code}")
            
            if resp.status_code == 200:
                data = resp.json()
                logger.info(f"YAZIO response keys: {list(data.keys())}")
                
                # Extract meals data
                meals = data.get("meals", {})
                total_cal = 0
                total_protein = 0
                total_carbs = 0
                total_fat = 0
                
                for meal_key, meal_data in meals.items():
                    if isinstance(meal_data, dict):
                        nutritional_info = meal_data.get("nutritional_info", {})
                        total_cal += nutritional_info.get("energy", {}).get("energy", 0) or 0
                        total_protein += nutritional_info.get("nutrient", {}).get("protein", 0) or 0
                        total_carbs += nutritional_info.get("nutrient", {}).get("carb", 0) or 0
                        total_fat += nutritional_info.get("nutrient", {}).get("fat", 0) or 0
                
                # Extract goals
                goals = data.get("goals", {})
                cal_goal = goals.get("energy", 0) or 0
                protein_goal = goals.get("protein", 0) or 0
                carbs_goal = goals.get("carb", 0) or 0
                fat_goal = goals.get("fat", 0) or 0
                
                logger.info(f"YAZIO: {round(total_cal)}/{cal_goal} kcal, P:{round(total_protein)}g, C:{round(total_carbs)}g, F:{round(total_fat)}g")
                
                return {
                    "calories": round(total_cal),
                    "caloriesGoal": round(cal_goal),
                    "protein": round(total_protein),
                    "proteinGoal": round(protein_goal),
                    "carbs": round(total_carbs),
                    "carbsGoal": round(carbs_goal),
                    "fat": round(total_fat),
                    "fatGoal": round(fat_goal),
                }
            else:
                logger.error(f"YAZIO daily-summary failed: {resp.text}")
                return {}
        except Exception as e:
            logger.error(f"YAZIO daily error: {e}")
            return {}


# ============================================
# API ENDPOINTS
# ============================================

@app.get("/")
async def root():
    """Health check endpoint."""
    return {
        "status": "ok",
        "service": "Health OS API",
        "version": "4.3.0",
        "timestamp": datetime.now().isoformat(),
        "config": {
            "garmin_token_set": bool(GARMIN_OAUTH_TOKEN),
            "yazio_credentials_set": bool(YAZIO_EMAIL and YAZIO_PASSWORD),
            "yazio_client_set": bool(YAZIO_CLIENT_ID),
            "notion_token_set": bool(os.getenv("NOTION_TOKEN")),
            "notes_db_set": bool(os.getenv("NOTES_DB_ID")),
        }
    }


@app.get("/garmin")
async def garmin_endpoint():
    """Get Garmin health data."""
    return get_garmin_data()


@app.get("/yazio")
async def yazio_endpoint(date: Optional[str] = None):
    """Get YAZIO nutrition data and goals."""
    if not date:
        date = datetime.now().strftime("%Y-%m-%d")
    return await get_yazio_daily(date)


@app.get("/sync")
async def sync_endpoint(date: Optional[str] = None):
    """Combined sync endpoint - Garmin + YAZIO daily totals."""
    if not date:
        date = datetime.now().strftime("%Y-%m-%d")
    
    # Get Garmin data
    garmin = get_garmin_data(date)
    
    # Get YAZIO daily totals
    yazio = await get_yazio_daily(date)
    
    errors = []
    sources = []
    
    if garmin.get("bodyBattery", 0) > 0 or garmin.get("sleepScore", 0) > 0:
        sources.append("garmin")
    
    if yazio.get("calories", 0) > 0:
        sources.append("yazio")
    
    return {
        "date": date,
        "bodyBattery": garmin.get("bodyBattery", 0),
        "sleepScore": garmin.get("sleepScore", 0),
        "sleepDuration": garmin.get("sleepDuration", "0h 00"),
        "weight": garmin.get("weight", 0.0),
        "weightChange": garmin.get("weightChange", 0.0),
        "calories": yazio.get("calories", 0),
        "caloriesGoal": yazio.get("caloriesGoal", 0),
        "protein": yazio.get("protein", 0),
        "proteinGoal": yazio.get("proteinGoal", 0),
        "carbs": yazio.get("carbs", 0),
        "carbsGoal": yazio.get("carbsGoal", 0),
        "fat": yazio.get("fat", 0),
        "fatGoal": yazio.get("fatGoal", 0),
        "lastUpdated": datetime.now().isoformat(),
        "errors": errors,
        "sources": sources,
    }


@app.get("/widget")
async def widget_endpoint(date: Optional[str] = None):
    """Widget data endpoint."""
    if not date:
        date = datetime.now().strftime("%Y-%m-%d")
    
    # Get all data
    garmin = get_garmin_data(date)
    yazio = await get_yazio_daily(date)
    
    # Format date
    now = datetime.now()
    day_names = {0: "LUN", 1: "MAR", 2: "MER", 3: "JEU", 4: "VEN", 5: "SAM", 6: "DIM"}
    month_names = {1: "JAN", 2: "FEV", 3: "MAR", 4: "AVR", 5: "MAI", 6: "JUN",
                   7: "JUL", 8: "AOU", 9: "SEP", 10: "OCT", 11: "NOV", 12: "DEC"}
    
    day_name = day_names.get(now.weekday(), "???")
    month_name = month_names.get(now.month, "???")
    formatted_date = f"{day_name} {now.day:02d} {month_name}"
    
    return {
        "date": formatted_date,
        "dateISO": date,
        "bodyBattery": garmin.get("bodyBattery", 0),
        "sleepScore": garmin.get("sleepScore", 0),
        "sleepDuration": garmin.get("sleepDuration", "0h 00"),
        "weight": garmin.get("weight", 0.0),
        "weightChange": garmin.get("weightChange", 0.0),
        "steps": 0,
        "calories": yazio.get("calories", 0),
        "caloriesGoal": yazio.get("caloriesGoal", 0),
        "caloriesBurned": 0,
        "protein": yazio.get("protein", 0),
        "proteinGoal": yazio.get("proteinGoal", 0),
        "carbs": yazio.get("carbs", 0),
        "carbsGoal": yazio.get("carbsGoal", 0),
        "fat": yazio.get("fat", 0),
        "fatGoal": yazio.get("fatGoal", 0),
        "lastUpdated": datetime.now().isoformat(),
        "errors": None,
        "source": "yazio+notion",
    }


# ============================================
# FOOD ITEMS ENDPOINT (FIXED v4.1)
# ============================================

@app.get("/food-items")
async def get_food_items(date_str: Optional[str] = None):
    """
    Returns all consumed food items with calculated macros.
    
    IMPORTANT FIX (v4.1): YAZIO's /products API returns nutrients PER GRAM,
    not per 100g. The calculation is: total = nutrient_per_gram × amount_in_grams
    
    Query params:
        date_str: Date in YYYY-MM-DD format (default: today)
    """
    if not date_str:
        date_str = datetime.now().strftime("%Y-%m-%d")
    
    # Validate date format
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return {"error": "Invalid date format. Use YYYY-MM-DD"}
    
    token = await yazio_login()
    if not token:
        return {"error": "Could not get YAZIO token"}
    
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json"
    }
    
    async with httpx.AsyncClient(timeout=30.0) as client:
        
        # 1. Get list of consumed items for the day
        consumed_url = f"{YAZIO_BASE_URL}/user/consumed-items?date={date_str}"
        
        try:
            consumed_resp = await client.get(consumed_url, headers=headers)
            consumed_resp.raise_for_status()
            consumed_data = consumed_resp.json()
        except Exception as e:
            logger.error(f"Failed to fetch consumed-items: {e}")
            return {"error": f"Failed to fetch consumed-items: {str(e)}"}
        
        # Extract products list
        products_list = consumed_data.get("products", [])
        
        if not products_list:
            return {
                "date": date_str,
                "count": 0,
                "calculated_count": 0,
                "skipped_count": 0,
                "items": [],
                "totals": {"calories": 0, "protein": 0, "carbs": 0, "fat": 0}
            }
        
        # 2. For each item, fetch product details and calculate macros
        items = []
        totals = {"calories": 0, "protein": 0, "carbs": 0, "fat": 0}
        skipped = 0
        
        for consumed in products_list:
            product_id = consumed.get("product_id")
            
            if not product_id:
                skipped += 1
                continue
            
            # Fetch product details (nutrients per gram)
            product_url = f"{YAZIO_BASE_URL}/products/{product_id}"
            
            try:
                product_resp = await client.get(product_url, headers=headers)
                product_resp.raise_for_status()
                product_data = product_resp.json()
            except Exception as e:
                logger.warning(f"Failed to fetch product {product_id}: {e}")
                items.append({
                    "yazio_id": consumed.get("id"),
                    "product_id": product_id,
                    "error": f"Could not fetch product: {str(e)}"
                })
                skipped += 1
                continue
            
            # 3. Extract data
            amount = consumed.get("amount", 0) or 0  # Already in grams
            serving = consumed.get("serving") or "gram"
            serving_quantity = consumed.get("serving_quantity")
            if serving_quantity is None:
                serving_quantity = amount if amount else 1
            
            nutrients = product_data.get("nutrients", {})
            
            # =============================================
            # CRITICAL FIX: Nutrients are PER GRAM
            # =============================================
            
            energy_per_g = nutrients.get("energy.energy", 0) or 0
            protein_per_g = nutrients.get("nutrient.protein", 0) or 0
            carbs_per_g = nutrients.get("nutrient.carb", 0) or 0
            fat_per_g = nutrients.get("nutrient.fat", 0) or 0
            
            # 4. Calculate actual macros (per_gram × amount)
            calories = round(energy_per_g * amount, 1)
            protein = round(protein_per_g * amount, 2)
            carbs = round(carbs_per_g * amount, 2)
            fat = round(fat_per_g * amount, 2)
            
            # 5. Build reference string
            try:
                if serving == "gram":
                    reference = f"{int(amount)}g"
                else:
                    if serving_quantity == int(serving_quantity):
                        qty_str = str(int(serving_quantity))
                    else:
                        qty_str = str(serving_quantity)
                    reference = f"{qty_str} {serving.capitalize()} ({int(amount)}g)"
            except (ValueError, TypeError):
                reference = f"{amount}g"
            
            # 6. Map meal (daytime → Notion format)
            meal_map = {
                "breakfast": "Breakfast",
                "lunch": "Lunch",
                "dinner": "Diner",
                "snack": "Snack"
            }
            meal = meal_map.get(consumed.get("daytime", ""), "Snack")
            
            # 7. Build item with debug info
            item = {
                "yazio_id": consumed.get("id"),
                "product_id": product_id,
                "name": product_data.get("name", "Unknown"),
                "meal": meal,
                "amount": amount,
                "unit": "g",
                "reference": reference,
                "calories": calories,
                "protein": protein,
                "carbs": carbs,
                "fat": fat,
                "consumed_at": consumed.get("date"),
                "serving_info": {
                    "serving": serving,
                    "serving_quantity": serving_quantity,
                },
                "nutrients_raw": {
                    "energy": energy_per_g,
                    "protein": protein_per_g,
                    "carbs": carbs_per_g,
                    "fat": fat_per_g,
                },
                "nutrient_basis": "per_gram",
            }
            
            items.append(item)
            
            # Accumulate totals
            totals["calories"] += calories
            totals["protein"] += protein
            totals["carbs"] += carbs
            totals["fat"] += fat
        
        # Round totals
        totals = {k: round(v, 1) for k, v in totals.items()}
        
        return {
            "date": date_str,
            "count": len(items),
            "calculated_count": len(items),
            "skipped_count": skipped,
            "items": items,
            "totals": totals,
            "totals_note": None,
        }


# ============================================
# SLEEP WEEK ENDPOINT (for live charts)
# ============================================

@app.get("/sleep-week")
async def sleep_week_endpoint(days: int = 7):
    """Returns sleep score and duration for the last N days (default 7).
    Used by embedded Notion charts for real-time visualization."""
    global garmin_client
    
    if not garmin_client:
        garmin_client = init_garmin()
    
    if not garmin_client:
        return {"error": "Garmin not connected", "data": []}
    
    today = date.today()
    results = []
    
    for i in range(days - 1, -1, -1):  # oldest first
        target = (today - timedelta(days=i)).isoformat()
        entry = {
            "date": target,
            "sleep_score": 0,
            "sleep_hours": 0.0,
            "sleep_duration": "0h 00",
        }
        
        try:
            sleep_data = garmin_client.get_sleep_data(target)
            if sleep_data and isinstance(sleep_data, dict):
                daily_sleep = sleep_data.get("dailySleepDTO", {})
                
                # Duration
                sleep_seconds = daily_sleep.get("sleepTimeSeconds", 0) or 0
                if sleep_seconds > 0:
                    hours = int(sleep_seconds // 3600)
                    minutes = int((sleep_seconds % 3600) // 60)
                    entry["sleep_duration"] = f"{hours}h {minutes:02d}"
                    entry["sleep_hours"] = round(sleep_seconds / 3600, 2)
                
                # Score
                entry["sleep_score"] = daily_sleep.get("sleepScores", {}).get("overall", {}).get("value", 0) or 0
        except Exception as e:
            logger.warning(f"Sleep data error for {target}: {e}")
        
        results.append(entry)
    
    # Compute stats
    scores = [r["sleep_score"] for r in results if r["sleep_score"] > 0]
    hours = [r["sleep_hours"] for r in results if r["sleep_hours"] > 0]
    
    return {
        "days": days,
        "data": results,
        "stats": {
            "avg_score": round(sum(scores) / len(scores), 1) if scores else 0,
            "max_score": max(scores) if scores else 0,
            "min_score": min(scores) if scores else 0,
            "avg_hours": round(sum(hours) / len(hours), 2) if hours else 0,
            "max_hours": round(max(hours), 2) if hours else 0,
            "min_hours": round(min(hours), 2) if hours else 0,
            "goal_met": sum(1 for h in hours if h >= 7),
            "total_days": len(hours),
        }
    }


# ============================================
# QUICK NOTES ENDPOINTS
# ============================================

@app.get("/notes/courses")
async def get_note_courses():
    """Fetch course options dynamically from the Notion DB schema."""
    db_id = os.getenv("NOTES_DB_ID", "7a8948775f0043f4823d83c004951b93")
    headers = get_notion_headers()

    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"{NOTION_BASE}/databases/{db_id}",
            headers=headers, timeout=10
        )
        if r.status_code != 200:
            raise HTTPException(status_code=r.status_code, detail=r.text)

        data = r.json()
        course_prop = data.get("properties", {}).get("Course", {})
        options = course_prop.get("select", {}).get("options", [])

        courses = [{"name": o["name"], "color": o.get("color", "gray")} for o in options]
        return {"courses": courses}


@app.get("/notes")
async def get_notes():
    """Fetch all active (not done) notes, sorted by creation date."""
    db_id = os.getenv("NOTES_DB_ID", "7a8948775f0043f4823d83c004951b93")
    headers = get_notion_headers()

    body = {
        "sorts": [{"property": "Created", "direction": "descending"}],
        "filter": {
            "property": "Done",
            "checkbox": {"equals": False}
        }
    }

    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"{NOTION_BASE}/databases/{db_id}/query",
            headers=headers, json=body, timeout=10
        )
        if r.status_code != 200:
            raise HTTPException(status_code=r.status_code, detail=r.text)

        data = r.json()
        notes = []
        for page in data.get("results", []):
            props = page["properties"]
            notes.append({
                "id": page["id"],
                "note": props["Note"]["title"][0]["plain_text"] if props["Note"]["title"] else "",
                "course": props["Course"]["select"]["name"] if props["Course"].get("select") else "General",
                "created": props["Created"]["created_time"],
                "done": props["Done"]["checkbox"]
            })

        return {"notes": notes}


@app.post("/notes")
async def create_note(payload: NoteCreate):
    """Create a new note in the Notion DB."""
    db_id = os.getenv("NOTES_DB_ID", "7a8948775f0043f4823d83c004951b93")
    headers = get_notion_headers()

    body = {
        "parent": {"database_id": db_id},
        "properties": {
            "Note": {"title": [{"text": {"content": payload.note}}]},
            "Course": {"select": {"name": payload.course or "General"}},
            "Done": {"checkbox": False}
        }
    }

    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"{NOTION_BASE}/pages", headers=headers, json=body, timeout=10
        )
        if r.status_code != 200:
            raise HTTPException(status_code=r.status_code, detail=r.text)

        page = r.json()
        return {
            "id": page["id"],
            "note": payload.note,
            "course": payload.course or "General",
            "created": page["created_time"],
            "done": False
        }


@app.patch("/notes/{note_id}")
async def toggle_note(note_id: str, payload: NoteToggle):
    """Mark a note as done/undone."""
    headers = get_notion_headers()

    body = {
        "properties": {
            "Done": {"checkbox": payload.done}
        }
    }

    async with httpx.AsyncClient() as client:
        r = await client.patch(
            f"{NOTION_BASE}/pages/{note_id}", headers=headers, json=body, timeout=10
        )
        if r.status_code != 200:
            raise HTTPException(status_code=r.status_code, detail=r.text)

        return {"id": note_id, "done": payload.done}


# ============================================
# DEBUG ENDPOINTS
# ============================================

@app.get("/debug-consumed-items")
async def debug_consumed_items(date_str: Optional[str] = None):
    """Raw consumed items from YAZIO (for debugging)."""
    if not date_str:
        date_str = datetime.now().strftime("%Y-%m-%d")
    
    token = await yazio_login()
    if not token:
        return {"error": "No YAZIO token"}
    
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(f"{YAZIO_BASE_URL}/user/consumed-items?date={date_str}", headers=headers)
        return resp.json()


@app.get("/debug-product/{product_id}")
async def debug_product(product_id: str):
    """Raw product details from YAZIO (for debugging)."""
    token = await yazio_login()
    if not token:
        return {"error": "No YAZIO token"}
    
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(f"{YAZIO_BASE_URL}/products/{product_id}", headers=headers)
        return resp.json()


# ============================================
# RUN SERVER
# ============================================

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
