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
    version="3.0.0"
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
# Auth and Data both on yzapi.yazio.com/v15
YAZIO_BASE_URL = "https://yzapi.yazio.com/v15"
YAZIO_CLIENT_ID = "1_4hiybetvfksgw40o0sog4s884kwc840wwso8go4k8c04goo4c"
YAZIO_CLIENT_SECRET = "6rok2m65xuskgkgogw40wkkk8sw0osg84s8cggsc4woos4s8o"

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
    protein: int = 0
    proteinGoal: int = 150
    carbs: int = 0
    carbsGoal: int = 250
    fat: int = 0
    fatGoal: int = 70
    # Meta
    lastUpdated: str = ""
    errors: list = []
    sources: list = []

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
                logger.info(f"user-settings response: {user_settings}")
                
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
            logger.info(f"Garmin Body Battery raw response: {bb_data}")
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
            logger.info(f"Garmin Weight raw response: {weight_data}")
            
            if weight_data:
                weights = weight_data.get("dateWeightList", []) or weight_data.get("weightList", [])
                if weights and len(weights) > 0:
                    latest = weights[-1]
                    weight_grams = latest.get("weight", 0)
                    if weight_grams > 0:
                        data["weight"] = round(weight_grams / 1000, 1)
                        
                        if len(weights) > 1:
                            first_weight = weights[0].get("weight", 0) / 1000
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
    
    # Check if we have a valid cached token
    if yazio_token:
        expires_at = yazio_token.get("expires_at", 0)
        if expires_at > datetime.now().timestamp():
            logger.info("Using cached YAZIO token")
            return yazio_token.get("access_token")
    
    try:
        logger.info(f"Logging into YAZIO with {YAZIO_EMAIL}...")
        
        # Request body as JSON
        payload = {
            "client_id": YAZIO_CLIENT_ID,
            "client_secret": YAZIO_CLIENT_SECRET,
            "username": YAZIO_EMAIL,
            "password": YAZIO_PASSWORD,
            "grant_type": "password"
        }
        
        async with httpx.AsyncClient() as client:
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
    }
    
    date_str = target_date.strftime("%Y-%m-%d")
    
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    
    try:
        async with httpx.AsyncClient() as client:
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
                        data["caloriesGoal"] = int(goals.get("energy.energy", 2000) or 2000)
                        data["proteinGoal"] = int(goals.get("nutrient.protein", 150) or 150)
                        data["carbsGoal"] = int(goals.get("nutrient.carb", 250) or 250)
                        data["fatGoal"] = int(goals.get("nutrient.fat", 70) or 70)
                    
                    # Calculate totals from meals (breakfast, lunch, dinner, snack)
                    meals = day_data.get("meals", {})
                    total_energy = 0
                    total_protein = 0
                    total_carbs = 0
                    total_fat = 0
                    
                    for meal_type in ["breakfast", "lunch", "dinner", "snack"]:
                        meal = meals.get(meal_type, {})
                        nutrients = meal.get("nutrients", {})
                        total_energy += nutrients.get("energy.energy", 0) or 0
                        total_protein += nutrients.get("nutrient.protein", 0) or 0
                        total_carbs += nutrients.get("nutrient.carb", 0) or 0
                        total_fat += nutrients.get("nutrient.fat", 0) or 0
                    
                    data["calories"] = int(total_energy)
                    data["protein"] = int(total_protein)
                    data["carbs"] = int(total_carbs)
                    data["fat"] = int(total_fat)
                
                logger.info(f"YAZIO: {data['calories']}/{data['caloriesGoal']} kcal, P:{data['protein']}g, C:{data['carbs']}g, F:{data['fat']}g")
                
            elif response.status_code == 404:
                # Fallback: try /user/consumed-items and /user/goals
                logger.info("Trying fallback: /user/consumed-items + /user/goals")
                
                # Get goals
                goals_url = f"{YAZIO_BASE_URL}/user/goals/unmodified?date={date_str}"
                goals_resp = await client.get(goals_url, headers=headers)
                if goals_resp.status_code == 200:
                    goals = goals_resp.json()
                    data["caloriesGoal"] = int(goals.get("energy.energy", 2000) or 2000)
                    data["proteinGoal"] = int(goals.get("nutrient.protein", 150) or 150)
                    data["carbsGoal"] = int(goals.get("nutrient.carb", 250) or 250)
                    data["fatGoal"] = int(goals.get("nutrient.fat", 70) or 70)
                
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
        "version": "3.0.0",
        "timestamp": datetime.now().isoformat(),
        "config": {
            "garmin_token_set": bool(GARMIN_OAUTH_TOKEN),
            "yazio_credentials_set": bool(YAZIO_EMAIL and YAZIO_PASSWORD),
        }
    }


@app.get("/sync", response_model=HealthData)
async def sync_health_data(date: Optional[str] = None):
    """
    Fetch and aggregate health data from all sources.
    
    - **date**: Optional date in YYYY-MM-DD format. Defaults to today.
    """
    if date:
        try:
            target_date = datetime.strptime(date, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD")
    else:
        target_date = date.today()
    
    errors = []
    sources = []
    
    # Initialize response
    health_data = HealthData(
        date=target_date.strftime("%Y-%m-%d"),
        lastUpdated=datetime.now().isoformat()
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
        target_date = date.today()
    
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
        target_date = date.today()
    
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
            result["garmin"]["user"] = client.get_full_name()
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
    if date_str is None:
        date_str = date.today().isoformat()
    
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
    
    async with httpx.AsyncClient() as client:
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
    if date_str is None:
        date_str = date.today().isoformat()
    
    token = await yazio_login()
    if not token:
        return {"error": "Could not get YAZIO token"}
    
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    
    async with httpx.AsyncClient() as client:
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
    if date_str is None:
        date_str = date.today().isoformat()
    
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
    async with httpx.AsyncClient() as client:
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
    async with httpx.AsyncClient() as client:
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
        date_str = date.today().isoformat()
    
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
        "Accept": "application/json"
    }
    
    async with httpx.AsyncClient(timeout=30.0) as client:
        
        # 1. Get list of consumed items
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
                "items": [],
                "totals": {"calories": 0, "protein": 0, "carbs": 0, "fat": 0}
            }
        
        # 2. For each item, fetch product details and calculate macros
        items = []
        totals = {"calories": 0, "protein": 0, "carbs": 0, "fat": 0}
        
        for consumed in products_list:
            product_id = consumed.get("product_id")
            
            if not product_id:
                continue
            
            # Fetch product details
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
                continue
            
            # 3. Extract data
            amount = consumed.get("amount", 0)  # Already in grams
            serving = consumed.get("serving", "gram")
            serving_quantity = consumed.get("serving_quantity", amount)
            
            nutrients = product_data.get("nutrients", {})
            
            # Macros per gram (decimal values)
            energy_per_g = nutrients.get("energy.energy", 0) or 0
            protein_per_g = nutrients.get("nutrient.protein", 0) or 0
            carbs_per_g = nutrients.get("nutrient.carb", 0) or 0
            fat_per_g = nutrients.get("nutrient.fat", 0) or 0
            
            # 4. Calculate actual macros
            calories = round(energy_per_g * amount, 1)
            protein = round(protein_per_g * amount, 2)
            carbs = round(carbs_per_g * amount, 2)
            fat = round(fat_per_g * amount, 2)
            
            # 5. Build reference string
            if serving == "gram":
                reference = f"{int(amount)}g"
            else:
                if serving_quantity == int(serving_quantity):
                    qty_str = str(int(serving_quantity))
                else:
                    qty_str = str(serving_quantity)
                reference = f"{qty_str} {serving.capitalize()} ({int(amount)}g)"
            
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
                "unit": "g",
                "reference": reference,
                "calories": calories,
                "protein": protein,
                "carbs": carbs,
                "fat": fat,
                "consumed_at": consumed.get("date"),
                "serving_info": {
                    "serving": serving,
                    "serving_quantity": serving_quantity
                }
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
            "items": items,
            "totals": totals
        }


# ============================================
# RUN SERVER
# ============================================

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
