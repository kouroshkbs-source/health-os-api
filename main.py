"""
Health OS API v4 - Complete version with corrected YAZIO endpoints
- Garmin: Token-based authentication (generate tokens locally first)
- YAZIO: OAuth v15 for auth + REST /user/widgets/daily-summary for data
  Endpoints discovered from juriadams/yazio npm package source code

Deploy on Railway
"""

import os
import json
import logging
import tempfile
import shutil
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from typing import Optional, List
import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

# ============================================
# CONFIGURATION
# ============================================

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Health OS API",
    description="Aggregates health data from Garmin Connect and YAZIO",
    version="4.0.0"
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

# Notion credentials (for /widget endpoint)
NOTION_TOKEN = os.getenv("NOTION_TOKEN")
NOTION_TODAY_PAGE_ID = "2deda7da-88db-81c3-bfc9-dd213ebddd77"
NOTION_JOURNAL_DB_ID = "2deda7da-88db-811f-98f5-f860d49af03d"
NOTION_API_URL = "https://api.notion.com/v1"

# YAZIO Client credentials (from env vars for security)
# Auth and Data both on yzapi.yazio.com/v15
YAZIO_BASE_URL = "https://yzapi.yazio.com/v15"
YAZIO_CLIENT_ID = os.getenv("YAZIO_CLIENT_ID")
YAZIO_CLIENT_SECRET = os.getenv("YAZIO_CLIENT_SECRET")

# Debug mode (disabled by default in production)
ENABLE_DEBUG = os.getenv("ENABLE_DEBUG", "0") == "1"

# Timezone (Railway runs in UTC, we want Belgium time)
LOCAL_TZ = ZoneInfo("Europe/Brussels")

def today_local() -> date:
    """Get today's date in local timezone (Europe/Brussels)"""
    return datetime.now(LOCAL_TZ).date()

# Token storage
garmin_client = None
yazio_token: Optional[dict] = None

# ============================================
# PYDANTIC MODELS
# ============================================

class HealthData(BaseModel):
    date: str
    # Garmin data
    bodyBattery: int = 0
    sleepScore: int = 0
    sleepDuration: str = "0h 00"
    weight: float = 0.0
    weightChange: float = 0.0
    # YAZIO data
    calories: int = 0
    caloriesGoal: int = 2000
    caloriesBurned: int = 0
    protein: int = 0
    proteinGoal: int = 150
    carbs: int = 0
    carbsGoal: int = 250
    fat: int = 0
    fatGoal: int = 70
    # Meta
    lastUpdated: str = ""
    errors: List[str] = Field(default_factory=list)
    sources: List[str] = Field(default_factory=list)

# ============================================
# HELPER FUNCTIONS
# ============================================

def safe_float(x, default=0.0) -> float:
    """Safely convert to float, returning default if None or invalid."""
    try:
        if x is None:
            return default
        return float(x)
    except Exception:
        return default

def fmt_amount(x) -> str:
    """Format amount for reference string without losing decimals."""
    try:
        x = float(x)
        if x.is_integer():
            return str(int(x))
        return f"{x:.1f}".rstrip("0").rstrip(".")
    except Exception:
        return str(x)

SUPPORTED_GRAM_UNITS = {"g", "gram", "grams"}

# ============================================
# GARMIN FUNCTIONS (Token-based)
# ============================================

def get_garmin_client():
    """Initialize Garmin client using pre-generated OAuth tokens"""
    global garmin_client
    
    if garmin_client is not None:
        return garmin_client
    
    if not GARMIN_OAUTH_TOKEN:
        logger.warning("GARMIN_OAUTH_TOKEN not set")
        return None
    
    try:
        from garminconnect import Garmin
        
        logger.info("Initializing Garmin client with OAuth tokens...")
        
        # Parse the token JSON
        token_data = json.loads(GARMIN_OAUTH_TOKEN)
        
        # Create a temporary directory to store the token files
        temp_dir = tempfile.mkdtemp()
        
        # Write oauth1_token.json with actual oauth1 data
        oauth1_file = os.path.join(temp_dir, "oauth1_token.json")
        oauth1_data = token_data.get("oauth1_token", {})
        with open(oauth1_file, 'w') as f:
            json.dump(oauth1_data, f)
        
        # Write oauth2_token.json with actual oauth2 data
        oauth2_file = os.path.join(temp_dir, "oauth2_token.json")
        oauth2_data = token_data.get("oauth2_token", {})
        with open(oauth2_file, 'w') as f:
            json.dump(oauth2_data, f)
        
        # Initialize client WITHOUT credentials
        client = Garmin()
        
        # Load the saved tokens
        client.garth.load(temp_dir)
        
        # Clean up temp directory to avoid disk bloat on Railway
        shutil.rmtree(temp_dir, ignore_errors=True)
        
        logger.info(f"oauth1_token exists: {client.garth.oauth1_token is not None}")
        logger.info(f"oauth2_token exists: {client.garth.oauth2_token is not None}")
        
        # BYPASS MODE: If GARMIN_DISPLAY_NAME is set, skip API profile fetch
        # This is needed because Garmin API returns [] for profile on server IPs
        if GARMIN_DISPLAY_NAME:
            logger.info(f"Using GARMIN_DISPLAY_NAME bypass: {GARMIN_DISPLAY_NAME}")
            client.display_name = GARMIN_DISPLAY_NAME
            client.full_name = GARMIN_DISPLAY_NAME
            logger.info(f"Client ready with forced display_name: {client.display_name}")
        else:
            # Try to fetch profile from API (may fail on server environments)
            try:
                logger.info("No GARMIN_DISPLAY_NAME set, trying API...")
                user_settings = client.garth.connectapi(
                    "/userprofile-service/userprofile/user-settings"
                )
                logger.info(f"user-settings response keys: {list(user_settings.keys()) if isinstance(user_settings, dict) else type(user_settings)}")
                
                display_name = None
                if user_settings and isinstance(user_settings, dict):
                    if 'userData' in user_settings:
                        display_name = user_settings['userData'].get('displayName')
                    else:
                        display_name = user_settings.get('displayName')
                
                if not display_name:
                    raise Exception("Could not get displayName - set GARMIN_DISPLAY_NAME env var")
                
                client.display_name = display_name
                client.full_name = display_name
                logger.info(f"Session active for: {client.display_name}")
                
            except Exception as profile_error:
                logger.error(f"Profile fetch failed: {profile_error}")
                logger.error("Set GARMIN_DISPLAY_NAME environment variable to bypass")
                return None
        
        garmin_client = client
        return client
        
    except json.JSONDecodeError as e:
        logger.error(f"Invalid GARMIN_OAUTH_TOKEN JSON: {e}")
        return None
    except Exception as e:
        logger.error(f"Garmin connection error: {type(e).__name__}: {e}")
        garmin_client = None
        return None


def fetch_garmin_data(target_date: date) -> dict:
    """Fetch all relevant Garmin data for a specific date"""
    client = get_garmin_client()
    
    if client is None:
        return {"error": "Garmin not connected - set GARMIN_OAUTH_TOKEN"}
    
    data = {
        "bodyBattery": 0,
        "sleepScore": 0,
        "sleepDuration": "0h 00",
        "weight": 0.0,
        "weightChange": 0.0,
    }
    
    date_str = target_date.strftime("%Y-%m-%d")
    logger.info(f"Fetching Garmin data for date: {date_str}")
    
    try:
        # Body Battery
        try:
            bb_data = client.get_body_battery(date_str)
            logger.info(f"Garmin Body Battery: {len(bb_data) if isinstance(bb_data, list) else type(bb_data)} items")
            if bb_data:
                if isinstance(bb_data, list) and len(bb_data) > 0:
                    max_bb = 0
                    for item in bb_data:
                        if isinstance(item, dict):
                            val = item.get("chargedValue", 0) or item.get("bodyBatteryLevel", 0) or 0
                            if val > max_bb:
                                max_bb = val
                    data["bodyBattery"] = max_bb
                elif isinstance(bb_data, dict):
                    data["bodyBattery"] = bb_data.get("chargedValue", 0) or 0
            logger.info(f"Body Battery: {data['bodyBattery']}")
        except Exception as e:
            logger.warning(f"Could not fetch body battery: {e}")
        
        # Sleep data
        try:
            sleep_data = client.get_sleep_data(date_str)
            logger.info(f"Garmin Sleep raw response keys: {sleep_data.keys() if isinstance(sleep_data, dict) else type(sleep_data)}")
            if sleep_data:
                daily_sleep = sleep_data.get("dailySleepDTO", {})
                logger.info(f"Garmin dailySleepDTO: {daily_sleep}")
                
                # Sleep score
                sleep_scores = daily_sleep.get("sleepScores", {})
                if sleep_scores:
                    overall = sleep_scores.get("overall", {})
                    data["sleepScore"] = overall.get("value", 0) if isinstance(overall, dict) else 0
                
                # Sleep duration
                sleep_seconds = daily_sleep.get("sleepTimeSeconds", 0)
                if sleep_seconds:
                    hours = sleep_seconds // 3600
                    minutes = (sleep_seconds % 3600) // 60
                    data["sleepDuration"] = f"{hours}h {minutes:02d}"
            logger.info(f"Sleep: {data['sleepDuration']}, Score: {data['sleepScore']}")
        except Exception as e:
            logger.warning(f"Could not fetch sleep data: {e}")
        
        # Weight
        try:
            start_date = (target_date - timedelta(days=7)).strftime("%Y-%m-%d")
            weight_data = client.get_body_composition(start_date, date_str)
            
            # Extract weights (handle both dict and list responses)
            weights = []
            if isinstance(weight_data, dict):
                weights = weight_data.get("dateWeightList") or weight_data.get("weightList") or []
                logger.info(f"Garmin Weight: {len(weights)} entries (dict)")
            elif isinstance(weight_data, list):
                weights = weight_data
                logger.info(f"Garmin Weight: {len(weights)} entries (list)")
            else:
                logger.info(f"Garmin Weight: unexpected type {type(weight_data)}")
            
            if weights and len(weights) > 0:
                latest = weights[-1]
                weight_grams = latest.get("weight", 0) if isinstance(latest, dict) else 0
                if weight_grams > 0:
                    data["weight"] = round(weight_grams / 1000, 1)
                    
                    if len(weights) > 1:
                        first = weights[0]
                        first_weight = (first.get("weight", 0) if isinstance(first, dict) else 0) / 1000
                        data["weightChange"] = round(data["weight"] - first_weight, 1)
            logger.info(f"Weight: {data['weight']} kg")
        except Exception as e:
            logger.warning(f"Could not fetch weight data: {e}")
        
        return data
        
    except Exception as e:
        logger.error(f"Error fetching Garmin data: {type(e).__name__}: {e}")
        return {"error": str(e)}

# ============================================
# YAZIO FUNCTIONS (REST API)
# ============================================

async def yazio_login() -> Optional[str]:
    """Login to YAZIO and get access token"""
    global yazio_token
    
    if not YAZIO_EMAIL or not YAZIO_PASSWORD:
        logger.warning("YAZIO credentials not set")
        return None
    
    # Check client credentials
    if not YAZIO_CLIENT_ID or not YAZIO_CLIENT_SECRET:
        logger.warning("YAZIO client credentials (CLIENT_ID/CLIENT_SECRET) not set")
        return None
    
    # Check if we have a valid cached token
    if yazio_token:
        expires_at = yazio_token.get("expires_at", 0)
        if expires_at > datetime.now().timestamp():
            logger.info("Using cached YAZIO token")
            return yazio_token.get("access_token")
    
    try:
        logger.info("Logging into YAZIO...")
        
        # Request body as JSON
        payload = {
            "client_id": YAZIO_CLIENT_ID,
            "client_secret": YAZIO_CLIENT_SECRET,
            "username": YAZIO_EMAIL,
            "password": YAZIO_PASSWORD,
            "grant_type": "password"
        }
        
        async with httpx.AsyncClient(timeout=15.0) as client:
            # Auth on v15 endpoint
            response = await client.post(
                f"{YAZIO_BASE_URL}/oauth/token",
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                }
            )
            
            logger.info(f"YAZIO login response status: {response.status_code}")
            
            if response.status_code == 200:
                token_data = response.json()
                expires_in = token_data.get("expires_in", 172800)  # Default 48h
                token_data["expires_at"] = datetime.now().timestamp() + expires_in
                yazio_token = token_data
                logger.info("YAZIO login successful!")
                return token_data.get("access_token")
            else:
                logger.error(f"YAZIO login failed: {response.status_code} - {response.text}")
                return None
                
    except Exception as e:
        logger.error(f"YAZIO login error: {type(e).__name__}: {e}")
        return None


async def fetch_yazio_data(target_date: date) -> dict:
    """Fetch nutrition data from YAZIO using REST API with /user/widgets/daily-summary"""
    token = await yazio_login()
    
    if token is None:
        return {"error": "YAZIO not connected"}
    
    data = {
        "calories": 0,
        "caloriesGoal": 2000,
        "protein": 0,
        "proteinGoal": 150,
        "carbs": 0,
        "carbsGoal": 250,
        "fat": 0,
        "fatGoal": 70,
        "caloriesBurned": 0,
    }
    
    date_str = target_date.strftime("%Y-%m-%d")
    
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "Yazio/7.3.10 (iPhone; iOS 16.2; Scale/3.00)",
    }
    
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            # Use correct endpoint: /user/widgets/daily-summary
            url = f"{YAZIO_BASE_URL}/user/widgets/daily-summary?date={date_str}"
            logger.info(f"YAZIO endpoint: {url}")
            
            response = await client.get(url, headers=headers)
            logger.info(f"YAZIO daily-summary response status: {response.status_code}")
            
            if response.status_code == 200:
                day_data = response.json()
                logger.info(f"YAZIO response keys: {list(day_data.keys()) if isinstance(day_data, dict) else type(day_data)}")
                
                # Parse goals from response
                # Structure: { goals: { "energy.energy": N, "nutrient.protein": N, ... }, meals: {...} }
                if isinstance(day_data, dict):
                    goals = day_data.get("goals", {})
                    if goals:
                        data["caloriesGoal"] = int(round(safe_float(goals.get("energy.energy"), 2000)))
                        data["proteinGoal"] = int(round(safe_float(goals.get("nutrient.protein"), 150)))
                        data["carbsGoal"] = int(round(safe_float(goals.get("nutrient.carb"), 250)))
                        data["fatGoal"] = int(round(safe_float(goals.get("nutrient.fat"), 70)))
                    
                    # Calculate totals from meals (breakfast, lunch, dinner, snack)
                    meals = day_data.get("meals", {})
                    total_energy = 0.0
                    total_protein = 0.0
                    total_carbs = 0.0
                    total_fat = 0.0
                    
                    for meal_type in ["breakfast", "lunch", "dinner", "snack"]:
                        meal = meals.get(meal_type, {})
                        nutrients = meal.get("nutrients", {})
                        total_energy += safe_float(nutrients.get("energy.energy"), 0.0)
                        total_protein += safe_float(nutrients.get("nutrient.protein"), 0.0)
                        total_carbs += safe_float(nutrients.get("nutrient.carb"), 0.0)
                        total_fat += safe_float(nutrients.get("nutrient.fat"), 0.0)
                    
                    data["calories"] = int(round(total_energy))
                    data["protein"] = int(round(total_protein))
                    data["carbs"] = int(round(total_carbs))
                    data["fat"] = int(round(total_fat))
                    
                    # Get burned calories from activities
                    activities = day_data.get("activities", {})
                    if activities:
                        activity_nutrients = activities.get("nutrients", {})
                        data["caloriesBurned"] = int(round(safe_float(activity_nutrients.get("energy.energy"), 0.0)))
                    
                    logger.info(f"YAZIO activities: {len(activities) if isinstance(activities, list) else 'present'}")
                
                logger.info(f"YAZIO: {data['calories']}/{data['caloriesGoal']} kcal, Burned: {data['caloriesBurned']}, P:{data['protein']}g, C:{data['carbs']}g, F:{data['fat']}g")
                
            elif response.status_code == 404:
                # Fallback: try /user/consumed-items and /user/goals
                logger.info("Trying fallback: /user/consumed-items + /user/goals")
                
                # Get goals
                goals_url = f"{YAZIO_BASE_URL}/user/goals/unmodified?date={date_str}"
                goals_resp = await client.get(goals_url, headers=headers)
                if goals_resp.status_code == 200:
                    goals = goals_resp.json()
                    data["caloriesGoal"] = int(round(safe_float(goals.get("energy.energy"), 2000)))
                    data["proteinGoal"] = int(round(safe_float(goals.get("nutrient.protein"), 150)))
                    data["carbsGoal"] = int(round(safe_float(goals.get("nutrient.carb"), 250)))
                    data["fatGoal"] = int(round(safe_float(goals.get("nutrient.fat"), 70)))
                
                # Get consumed items - need to fetch each product's nutrients
                items_url = f"{YAZIO_BASE_URL}/user/consumed-items?date={date_str}"
                items_resp = await client.get(items_url, headers=headers)
                if items_resp.status_code == 200:
                    items = items_resp.json()
                    logger.info(f"YAZIO consumed items: {len(items) if isinstance(items, list) else 'not a list'}")
                    
            else:
                error_text = response.text[:500] if response.text else "No response body"
                logger.error(f"YAZIO fetch failed: {response.status_code} - {error_text}")
                return {"error": f"YAZIO API error: {response.status_code}"}
                
        return data
        
    except Exception as e:
        logger.error(f"Error fetching YAZIO data: {type(e).__name__}: {e}")
        return {"error": str(e)}

# ============================================
# API ENDPOINTS
# ============================================

@app.get("/")
async def root():
    """Health check endpoint"""
    return {
        "status": "ok",
        "service": "Health OS API",
        "version": "4.0.0",
        "timestamp": datetime.now(LOCAL_TZ).isoformat(),
        "config": {
            "garmin_token_set": bool(GARMIN_OAUTH_TOKEN),
            "yazio_credentials_set": bool(YAZIO_EMAIL and YAZIO_PASSWORD),
            "yazio_client_set": bool(YAZIO_CLIENT_ID and YAZIO_CLIENT_SECRET),
        }
    }


@app.get("/sync", response_model=HealthData)
async def sync_health_data(date_str: Optional[str] = None):
    """
    Fetch and aggregate health data from all sources.
    
    - **date_str**: Optional date in YYYY-MM-DD format. Defaults to today.
    """
    if date_str:
        try:
            target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD")
    else:
        target_date = today_local()
    
    errors = []
    sources = []
    
    # Initialize response
    health_data = HealthData(
        date=target_date.strftime("%Y-%m-%d"),
        lastUpdated=datetime.now(LOCAL_TZ).isoformat()
    )
    
    # Fetch Garmin data
    garmin_data = fetch_garmin_data(target_date)
    if "error" in garmin_data:
        errors.append(f"Garmin: {garmin_data['error']}")
    else:
        health_data.bodyBattery = garmin_data.get("bodyBattery", 0)
        health_data.sleepScore = garmin_data.get("sleepScore", 0)
        health_data.sleepDuration = garmin_data.get("sleepDuration", "0h 00")
        health_data.weight = garmin_data.get("weight", 0.0)
        health_data.weightChange = garmin_data.get("weightChange", 0.0)
        sources.append("garmin")
    
    # Fetch YAZIO data
    yazio_data = await fetch_yazio_data(target_date)
    if "error" in yazio_data:
        errors.append(f"YAZIO: {yazio_data['error']}")
    else:
        health_data.calories = yazio_data.get("calories", 0)
        health_data.caloriesGoal = yazio_data.get("caloriesGoal", 2000)
        health_data.caloriesBurned = yazio_data.get("caloriesBurned", 0)
        health_data.protein = yazio_data.get("protein", 0)
        health_data.proteinGoal = yazio_data.get("proteinGoal", 150)
        health_data.carbs = yazio_data.get("carbs", 0)
        health_data.carbsGoal = yazio_data.get("carbsGoal", 250)
        health_data.fat = yazio_data.get("fat", 0)
        health_data.fatGoal = yazio_data.get("fatGoal", 70)
        sources.append("yazio")
    
    health_data.errors = errors
    health_data.sources = sources
    
    return health_data


@app.get("/garmin")
async def garmin_only(date_str: Optional[str] = None):
    """Fetch only Garmin data (for testing)"""
    if date_str:
        try:
            target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD")
    else:
        target_date = today_local()
    
    return fetch_garmin_data(target_date)


@app.get("/yazio")
async def yazio_only(date_str: Optional[str] = None):
    """Fetch only YAZIO data (for testing)"""
    if date_str:
        try:
            target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD")
    else:
        target_date = today_local()
    
    return await fetch_yazio_data(target_date)


@app.get("/test")
async def test_connections():
    """Test both connections and return debug info"""
    result = {
        "garmin": {
            "token_configured": bool(GARMIN_OAUTH_TOKEN),
            "status": "unknown",
            "user": None,
            "error": None
        },
        "yazio": {
            "credentials_configured": bool(YAZIO_EMAIL and YAZIO_PASSWORD),
            "status": "unknown",
            "error": None
        }
    }
    
    # Test Garmin
    try:
        client = get_garmin_client()
        if client:
            result["garmin"]["status"] = "connected"
            # Safely get user name (API varies by version)
            try:
                name = client.get_full_name()
            except:
                name = getattr(client, "full_name", None) or getattr(client, "display_name", None)
            result["garmin"]["user"] = name
        elif GARMIN_OAUTH_TOKEN:
            # Token is configured but client failed to initialize
            result["garmin"]["status"] = "error"
            result["garmin"]["error"] = "Token configured but client initialization failed (check logs)"
        else:
            result["garmin"]["status"] = "not_configured"
    except Exception as e:
        result["garmin"]["status"] = "error"
        result["garmin"]["error"] = f"{type(e).__name__}: {str(e)}"
    
    # Test YAZIO
    try:
        token = await yazio_login()
        if token:
            result["yazio"]["status"] = "connected"
            result["yazio"]["token_preview"] = token[:20] + "..."
        else:
            result["yazio"]["status"] = "login_failed"
    except Exception as e:
        result["yazio"]["status"] = "error"
        result["yazio"]["error"] = f"{type(e).__name__}: {str(e)}"
    
    return result


@app.get("/test-graphql")
async def test_graphql(date_str: Optional[str] = None):
    """
    Test YAZIO GraphQL endpoint to get individual food items.
    """
    if not ENABLE_DEBUG:
        raise HTTPException(status_code=404, detail="Debug endpoints disabled in production")
    
    if date_str is None:
        date_str = today_local().isoformat()
    
    token = await yazio_login()
    if not token:
        return {"error": "Could not get YAZIO token"}
    
    query = """
    query GetDiaryDetail($date: Date!) {
      me {
        diary(date: $date) {
          meals {
            name
            consumedItems {
              name
              amount
              unit
              nutritionSummary {
                calories { current }
                protein { current }
                carbohydrates { current }
                fat { current }
              }
            }
          }
        }
      }
    }
    """
    
    endpoints = [
        "https://yzapi.yazio.com/graphql",
        "https://yzapi.yazio.com/v1/graphql",
        "https://live.yazio.com/graphql",
    ]
    
    results = {}
    
    async with httpx.AsyncClient(timeout=15.0) as client:
        for endpoint in endpoints:
            try:
                response = await client.post(
                    endpoint,
                    json={"query": query, "variables": {"date": date_str}},
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                        "User-Agent": "Yazio/7.3.10 (iPhone; iOS 16.2; Scale/3.00)"
                    },
                    timeout=10
                )
                
                results[endpoint] = {
                    "status": response.status_code,
                    "response": response.json() if response.status_code == 200 else response.text[:500]
                }
                
                if response.status_code == 200:
                    data = response.json()
                    if data.get("data") and not data.get("errors"):
                        meals_data = data.get("data", {}).get("me", {}).get("diary", {}).get("meals", [])
                        flat_items = []
                        for meal in meals_data:
                            meal_name = meal.get("name", "Unknown")
                            for item in meal.get("consumedItems", []):
                                macros = item.get("nutritionSummary", {})
                                flat_items.append({
                                    "name": item.get("name", "Unknown"),
                                    "meal_type": meal_name,
                                    "amount": item.get("amount", 0),
                                    "unit": item.get("unit", ""),
                                    "calories": macros.get("calories", {}).get("current", 0),
                                    "protein": macros.get("protein", {}).get("current", 0),
                                    "carbs": macros.get("carbohydrates", {}).get("current", 0),
                                    "fat": macros.get("fat", {}).get("current", 0)
                                })
                        results[endpoint]["parsed_items"] = flat_items
                        results[endpoint]["items_count"] = len(flat_items)
                        
            except Exception as e:
                results[endpoint] = {"status": "error", "error": f"{type(e).__name__}: {str(e)}"}
    
    return {"date": date_str, "token_preview": token[:20] + "...", "endpoints_tested": results}


@app.get("/debug-meals")
async def debug_meals(date_str: Optional[str] = None):
    """Debug endpoint to see the full meals response structure."""
    if not ENABLE_DEBUG:
        raise HTTPException(status_code=404, detail="Debug endpoints disabled in production")
    
    if date_str is None:
        date_str = today_local().isoformat()
    
    token = await yazio_login()
    if not token:
        return {"error": "Could not get YAZIO token"}
    
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.get(
            f"{YAZIO_BASE_URL}/user/widgets/daily-summary?date={date_str}",
            headers=headers
        )
        
        if response.status_code == 200:
            data = response.json()
            return {
                "date": date_str,
                "top_level_keys": list(data.keys()) if isinstance(data, dict) else type(data).__name__,
                "meals_structure": data.get("meals", "NOT FOUND"),
                "full_response": data
            }
        else:
            return {"error": f"Status {response.status_code}", "response": response.text[:1000]}


@app.get("/debug-consumed-items")
async def debug_consumed_items(date_str: Optional[str] = None):
    """Test various endpoints for individual food items"""
    if not ENABLE_DEBUG:
        raise HTTPException(status_code=404, detail="Debug endpoints disabled in production")
    
    if date_str is None:
        date_str = today_local().isoformat()
    
    token = await yazio_login()
    if not token:
        return {"error": "Could not get YAZIO token"}
    
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    
    endpoints_to_try = [
        f"{YAZIO_BASE_URL}/user/consumed-items?date={date_str}",
        f"{YAZIO_BASE_URL}/user/diary?date={date_str}",
        f"{YAZIO_BASE_URL}/user/day?date={date_str}",
        f"{YAZIO_BASE_URL}/user/meals?date={date_str}",
        f"{YAZIO_BASE_URL}/user/food-diary?date={date_str}",
        f"{YAZIO_BASE_URL}/diary?date={date_str}",
    ]
    
    results = {}
    async with httpx.AsyncClient(timeout=15.0) as client:
        for url in endpoints_to_try:
            try:
                response = await client.get(url, headers=headers)
                if response.status_code == 200:
                    try:
                        json_response = response.json()
                        has_items = False
                        if isinstance(json_response, list) and len(json_response) > 0:
                            has_items = True
                        elif isinstance(json_response, dict):
                            for key in ["items", "consumed_items", "foods", "entries", "products"]:
                                if key in json_response:
                                    has_items = True
                                    break
                        results[url] = {
                            "status": response.status_code,
                            "has_individual_items": has_items,
                            "response_type": type(json_response).__name__,
                            "response": json_response
                        }
                    except:
                        results[url] = {"status": response.status_code, "response": response.text[:500]}
                else:
                    results[url] = {
                        "status": response.status_code,
                        "response": response.text[:200] if response.text else "empty"
                    }
            except Exception as e:
                results[url] = {"error": str(e)}
    
    return {"date": date_str, "results": results}


@app.get("/debug-product")
async def debug_product(product_id: str):
    """Test fetching individual product details (name, macros, etc.)"""
    if not ENABLE_DEBUG:
        raise HTTPException(status_code=404, detail="Debug endpoints disabled in production")
    
    token = await yazio_login()
    if not token:
        return {"error": "Could not get YAZIO token"}
    
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    
    # Try different possible endpoints for product details
    endpoints_to_try = [
        f"{YAZIO_BASE_URL}/products/{product_id}",
        f"{YAZIO_BASE_URL}/user/products/{product_id}",
        f"https://yzapi.yazio.com/v7/products/{product_id}",
        f"https://yzapi.yazio.com/v8/products/{product_id}",
        f"https://yzapi.yazio.com/v10/products/{product_id}",
        f"https://yzapi.yazio.com/v12/products/{product_id}",
    ]
    
    results = {}
    async with httpx.AsyncClient(timeout=15.0) as client:
        for url in endpoints_to_try:
            try:
                response = await client.get(url, headers=headers)
                if response.status_code == 200:
                    results[url] = {"status": 200, "response": response.json()}
                else:
                    results[url] = {"status": response.status_code, "response": response.text[:300]}
            except Exception as e:
                results[url] = {"error": str(e)}
    
    return {"product_id": product_id, "results": results}


# ============================================
# FOOD ITEMS ENDPOINT (for Notion sync)
# ============================================

@app.get("/food-items")
async def get_food_items(date_str: Optional[str] = None):
    """
    Get all consumed food items with calculated macros.
    Combines /user/consumed-items + /products/{id} to return complete food data.
    
    Returns:
        - List of foods with name, meal, amount, calculated macros
        - Ready for N8N sync to Notion
    """
    if date_str is None:
        date_str = today_local().isoformat()
    
    # Validate date format
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD")
    
    token = await yazio_login()
    if not token:
        return {"error": "Could not get YAZIO token"}
    
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": "Yazio/7.3.10 (iPhone; iOS 16.2; Scale/3.00)",
    }
    
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        
        # 1. Get list of consumed items
        consumed_url = f"{YAZIO_BASE_URL}/user/consumed-items?date={date_str}"
        
        try:
            consumed_resp = await client.get(consumed_url, headers=headers)
            consumed_resp.raise_for_status()
            consumed_data = consumed_resp.json()
        except Exception as e:
            logger.error(f"Failed to fetch consumed-items: {e}")
            return {"error": f"Failed to fetch consumed-items: {str(e)}"}
        
        # Extract products list (handle both dict and list responses)
        if isinstance(consumed_data, dict):
            # Try multiple possible keys
            products_list = (
                consumed_data.get("products") or 
                consumed_data.get("items") or 
                consumed_data.get("consumed_items") or 
                consumed_data.get("entries") or 
                []
            )
        elif isinstance(consumed_data, list):
            products_list = consumed_data
        else:
            products_list = []
            logger.warning(f"Unexpected consumed_data type: {type(consumed_data)}")
        
        if not products_list:
            return {
                "date": date_str,
                "count": 0,
                "items": [],
                "totals": {"calories": 0, "protein": 0, "carbs": 0, "fat": 0}
            }
        
        # 2. For each item, fetch product details and calculate macros
        items = []
        totals = {"calories": 0, "protein": 0, "carbs": 0, "fat": 0}
        product_cache = {}  # Cache to avoid duplicate API calls for same product
        
        for consumed in products_list:
            # Skip non-dict items (some APIs return strings/ids)
            if not isinstance(consumed, dict):
                logger.warning(f"Skipping non-dict item in products_list: {type(consumed)}")
                continue
            
            # Robust product_id extraction (API can use different keys)
            product_obj = consumed.get("product")
            product_id = (
                consumed.get("product_id")
                or consumed.get("productId")
                or (product_obj.get("id") if isinstance(product_obj, dict) else None)
                or consumed.get("id")
            )
            
            if not product_id:
                continue
            
            # Check cache first
            if product_id in product_cache:
                product_data = product_cache[product_id]
            else:
                # Fetch product details
                product_url = f"{YAZIO_BASE_URL}/products/{product_id}"
                
                try:
                    product_resp = await client.get(product_url, headers=headers)
                    product_resp.raise_for_status()
                    product_data = product_resp.json()
                    product_cache[product_id] = product_data  # Cache it
                except Exception as e:
                    logger.warning(f"Failed to fetch product {product_id}: {e}")
                    items.append({
                        "yazio_id": consumed.get("id"),
                        "product_id": product_id,
                        "error": f"Could not fetch product: {str(e)}"
                    })
                    continue
            
            # 3. Extract data
            amount = safe_float(consumed.get("amount"), 0.0)  # Amount in unit provided by API (see unit field)
            serving = consumed.get("serving") or "gram"  # Default to gram if None
            serving_quantity = consumed.get("serving_quantity")
            # Handle None or missing serving_quantity (use 1, not amount)
            if serving_quantity is None:
                serving_quantity = 1
            
            # Raw nutrient values from API (use safe_float for robustness)
            nutrients = product_data.get("nutrients") or {}
            energy_raw = safe_float(nutrients.get("energy.energy"), 0.0)
            protein_raw = safe_float(nutrients.get("nutrient.protein"), 0.0)
            carbs_raw = safe_float(nutrients.get("nutrient.carb"), 0.0)
            fat_raw = safe_float(nutrients.get("nutrient.fat"), 0.0)
            
            # Get unit from consumed data (may be g, ml, portion, etc.) - normalize
            unit = (consumed.get("unit") or "g").strip()
            
            # 4. Calculate actual macros
            # STRICT MODE: Only calculate if unit is in grams (reliable)
            # For other units (ml, portion, etc.), we can't reliably calculate
            if unit.lower() not in SUPPORTED_GRAM_UNITS:
                # Unit not in grams - can't reliably calculate macros
                # Return None to indicate "unknown" (not "zero")
                calories = None
                protein = None
                carbs = None
                fat = None
                nutrient_format = "unsupported_unit"
                calc_skipped = True
            else:
                calc_skipped = False
                # Always use per-100g (nutrition standard)
                # The per-gram heuristic was unreliable (e.g., cucumber has <10 kcal/100g)
                factor = amount / 100.0 if amount > 0 else 0
                calories = round(energy_raw * factor, 1)
                protein = round(protein_raw * factor, 2)
                carbs = round(carbs_raw * factor, 2)
                fat = round(fat_raw * factor, 2)
                nutrient_format = "per_100g"
            
            # 5. Build reference string (use actual unit, preserve decimals)
            if not serving or serving.lower() in ("gram", "grams"):
                # Simple format: amount + unit
                reference = f"{fmt_amount(amount)}{unit}"
            else:
                # Serving format: qty × serving (amount + unit)
                if serving_quantity is not None and serving_quantity == int(serving_quantity):
                    qty_str = str(int(serving_quantity))
                else:
                    qty_str = str(serving_quantity) if serving_quantity else "1"
                serving_name = serving.capitalize() if serving else "Serving"
                reference = f"{qty_str} {serving_name} ({fmt_amount(amount)}{unit})"
            
            # 6. Map meal (daytime → Notion format)
            meal_map = {
                "breakfast": "Breakfast",
                "lunch": "Lunch",
                "dinner": "Diner",  # Match Notion schema spelling
                "snack": "Snack"
            }
            meal = meal_map.get(consumed.get("daytime", ""), "Snack")
            
            # 7. Build item
            item = {
                "yazio_id": consumed.get("id"),
                "product_id": product_id,
                "name": product_data.get("name", "Unknown"),
                "meal": meal,
                "amount": amount,
                "unit": unit,
                "reference": reference,
                "calories": calories,
                "protein": protein,
                "carbs": carbs,
                "fat": fat,
                "consumed_at": consumed.get("date"),
                "serving_info": {
                    "serving": serving,
                    "serving_quantity": serving_quantity
                },
                # Debug info for nutrient calculation
                "nutrient_format": nutrient_format,
                "calc_skipped": calc_skipped,
                "nutrients_raw": {
                    "energy": energy_raw,
                    "protein": protein_raw,
                    "carbs": carbs_raw,
                    "fat": fat_raw
                }
            }
            
            items.append(item)
            
            # Accumulate totals (only if calculation was performed and not None)
            if not calc_skipped and calories is not None:
                totals["calories"] += calories
                totals["protein"] += (protein or 0)
                totals["carbs"] += (carbs or 0)
                totals["fat"] += (fat or 0)
        
        # Round totals
        totals = {k: round(v, 1) for k, v in totals.items()}
        
        # Count items with reliable calculations
        calculated_count = sum(1 for it in items if (not it.get("calc_skipped")) and it.get("calories") is not None)
        skipped_count = sum(1 for it in items if it.get("calc_skipped", False))
        
        return {
            "date": date_str,
            "count": len(items),
            "calculated_count": calculated_count,
            "skipped_count": skipped_count,
            "items": items,
            "totals": totals,
            "totals_note": "Totals only include items with supported units (grams)" if skipped_count > 0 else None
        }


# ============================================
# WIDGET ENDPOINT (reads from Notion)
# ============================================

def notion_headers():
    """Get headers for Notion API requests."""
    return {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28"
    }


async def fetch_notion_today_page() -> dict:
    """Fetch Today page from Notion to get macro totals."""
    if not NOTION_TOKEN:
        return {"error": "NOTION_TOKEN not set"}
    
    url = f"{NOTION_API_URL}/pages/{NOTION_TODAY_PAGE_ID}"
    
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.get(url, headers=notion_headers())
        
        if response.status_code == 200:
            data = response.json()
            props = data.get("properties", {})
            
            # Parse formula strings like "🍴 762.9 kcal" → 762.9
            def parse_formula(prop_name: str) -> float:
                prop = props.get(prop_name, {})
                if prop.get("type") == "formula":
                    formula = prop.get("formula", {})
                    if formula.get("type") == "string":
                        text = formula.get("string", "")
                        # Extract number from string
                        import re
                        match = re.search(r'[\d.]+', text)
                        if match:
                            return float(match.group())
                return 0.0
            
            return {
                "calories": parse_formula("Total Calories"),
                "protein": parse_formula("Total Protein"),
                "carbs": parse_formula("Total Carbs"),
                "fat": parse_formula("Total Fat"),
            }
        else:
            logger.error(f"Notion Today page error: {response.status_code} - {response.text}")
            return {"error": f"Notion error: {response.status_code}"}


async def fetch_notion_journal_entry(target_date: str) -> dict:
    """Fetch Journal entry from Notion to get Garmin data."""
    if not NOTION_TOKEN:
        return {"error": "NOTION_TOKEN not set"}
    
    url = f"{NOTION_API_URL}/databases/{NOTION_JOURNAL_DB_ID}/query"
    
    payload = {
        "filter": {
            "property": "Date",
            "date": {"equals": target_date}
        }
    }
    
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(url, headers=notion_headers(), json=payload)
        
        if response.status_code == 200:
            data = response.json()
            results = data.get("results", [])
            
            if results:
                props = results[0].get("properties", {})
                
                # Parse number properties
                def get_number(prop_name: str) -> float:
                    prop = props.get(prop_name, {})
                    if prop.get("type") == "number":
                        return prop.get("number") or 0
                    return 0
                
                # Parse rich_text properties
                def get_text(prop_name: str) -> str:
                    prop = props.get(prop_name, {})
                    if prop.get("type") == "rich_text":
                        texts = prop.get("rich_text", [])
                        if texts:
                            return texts[0].get("plain_text", "")
                    return ""
                
                return {
                    "weight": get_number("Weight"),
                    "sleepScore": int(get_number("Sleep score") or 0),
                    "bodyBattery": int(get_number("Body battery") or 0),
                    "sleepDuration": get_text("Sleep duration") or "0h 00",
                    "steps": int(get_number("Steps") or 0),
                }
            else:
                return {
                    "weight": 0,
                    "sleepScore": 0,
                    "bodyBattery": 0,
                    "sleepDuration": "0h 00",
                    "steps": 0,
                }
        else:
            logger.error(f"Notion Journal query error: {response.status_code} - {response.text}")
            return {"error": f"Notion error: {response.status_code}"}


@app.get("/widget")
async def widget_data():
    """
    Widget endpoint - aggregates data from multiple sources.
    
    Returns all data needed by the Health OS widget:
    - Garmin data (from Notion Journal, synced by GitHub Actions)
    - YAZIO data (directly from YAZIO API for real-time goals)
    """
    today = today_local()
    today_str = today.isoformat()
    
    # Format date for display (e.g., "LUN 05 JAN")
    days_fr = ['LUN', 'MAR', 'MER', 'JEU', 'VEN', 'SAM', 'DIM']
    months_fr = ['JAN', 'FÉV', 'MAR', 'AVR', 'MAI', 'JUN', 'JUL', 'AOÛ', 'SEP', 'OCT', 'NOV', 'DÉC']
    date_display = f"{days_fr[today.weekday()]} {today.day:02d} {months_fr[today.month - 1]}"
    
    errors = []
    
    # Fetch Garmin data from Notion Journal
    garmin_data = await fetch_notion_journal_entry(today_str)
    if "error" in garmin_data:
        errors.append(garmin_data["error"])
        garmin_data = {"weight": 0, "sleepScore": 0, "bodyBattery": 0, "sleepDuration": "0h 00"}
    
    # Fetch YAZIO data directly from API (for dynamic goals)
    yazio_data = await fetch_yazio_data(today)
    if "error" in yazio_data:
        errors.append(yazio_data["error"])
        yazio_data = {
            "calories": 0, "caloriesGoal": 2000,
            "protein": 0, "proteinGoal": 150,
            "carbs": 0, "carbsGoal": 250,
            "fat": 0, "fatGoal": 70,
            "caloriesBurned": 0
        }
    
    # ============================================
    # ADJUST GOALS BASED ON CALORIES BURNED
    # Reproduces YAZIO's proportional scaling logic
    # Ref: https://help.yazio.com/hc/en-us/articles/360002474498
    # ============================================
    
    # Config: ratio cap from env var (default 2.0)
    RATIO_CAP = float(os.getenv("YAZIO_GOAL_RATIO_CAP", "2.0"))
    
    # 1. Extract and sanitize base values
    # - Clamp burned to 0 (avoid negative from sync corrections)
    # - Clamp base to 1 minimum (avoid division by zero / negative cap)
    # - Ensure proper int conversion with None handling
    calories_burned_raw = max(0, int(yazio_data.get("caloriesBurned") or 0))
    base_calories_goal = max(1, int(yazio_data.get("caloriesGoal") or 2000))
    base_protein_goal = max(1, int(yazio_data.get("proteinGoal") or 150))
    base_carbs_goal = max(1, int(yazio_data.get("carbsGoal") or 250))
    base_fat_goal = max(1, int(yazio_data.get("fatGoal") or 70))
    
    # 2. Apply safety cap on burned calories
    # Cap at RATIO_CAP × base goal to prevent UI explosion from GPS bugs
    # Example: base=2000, cap=2.0 → max_burned=2000 → max adjusted=4000
    max_burned_allowed = int(round(base_calories_goal * (RATIO_CAP - 1)))
    calories_burned_used = max(0, min(calories_burned_raw, max_burned_allowed))
    
    # 3. Calculate adjusted calorie goal (capped)
    adjusted_calories_goal = base_calories_goal + calories_burned_used
    
    # 4. Calculate adjustment ratio (base >= 1 is guaranteed, no division by zero)
    adjustment_ratio = adjusted_calories_goal / base_calories_goal
    
    # 5. Apply proportional scaling to macros with proper rounding
    # Using round() instead of int() to avoid systematic truncation bias
    adjusted_protein_goal = int(round(base_protein_goal * adjustment_ratio))
    adjusted_carbs_goal = int(round(base_carbs_goal * adjustment_ratio))
    adjusted_fat_goal = int(round(base_fat_goal * adjustment_ratio))
    
    # 6. Log for debugging (includes raw vs used burned for cap detection)
    was_capped = calories_burned_raw > calories_burned_used
    logger.info(
        f"Goals adjustment: burned_raw={calories_burned_raw}, burned_used={calories_burned_used}, "
        f"capped={was_capped}, ratio={adjustment_ratio:.2f}, "
        f"calories={base_calories_goal}→{adjusted_calories_goal}, "
        f"protein={base_protein_goal}→{adjusted_protein_goal}, "
        f"carbs={base_carbs_goal}→{adjusted_carbs_goal}, "
        f"fat={base_fat_goal}→{adjusted_fat_goal}"
    )
    
    # Build response
    response = {
        "date": date_display,
        "dateISO": today_str,
        
        # Garmin data (from Notion Journal)
        "bodyBattery": garmin_data.get("bodyBattery", 0),
        "sleepScore": garmin_data.get("sleepScore", 0),
        "sleepDuration": garmin_data.get("sleepDuration", "0h 00"),
        "weight": garmin_data.get("weight", 0),
        "weightChange": 0,  # TODO: calculate from previous days
        "steps": garmin_data.get("steps", 0),
        
        # YAZIO data with ADJUSTED goals based on calories burned
        "calories": yazio_data.get("calories", 0),
        "caloriesGoal": adjusted_calories_goal,
        "caloriesGoalBase": base_calories_goal,
        "caloriesBurnedUsed": calories_burned_used,  # Value used for calculation (may be capped)
        "caloriesBurnedRaw": calories_burned_raw,    # Original value from YAZIO before cap
        
        "protein": yazio_data.get("protein", 0),
        "proteinGoal": adjusted_protein_goal,
        "proteinGoalBase": base_protein_goal,
        
        "carbs": yazio_data.get("carbs", 0),
        "carbsGoal": adjusted_carbs_goal,
        "carbsGoalBase": base_carbs_goal,
        
        "fat": yazio_data.get("fat", 0),
        "fatGoal": adjusted_fat_goal,
        "fatGoalBase": base_fat_goal,
        
        # Meta
        "lastUpdated": datetime.now(LOCAL_TZ).isoformat(),
        "errors": errors if errors else None,
        "source": "yazio+notion",
        
        # Adjustment flags (useful for frontend/debug)
        "goalsAdjusted": calories_burned_used > 0,
        "goalsCapped": was_capped,
        "adjustmentRatio": round(adjustment_ratio, 2),
        "goalsAlgorithm": "yazio_proportional_v1"
    }
    
    return response


# ============================================
# RUN SERVER
# ============================================

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
