"""
Health OS API v3 - Complete version
- Garmin: Token-based authentication (generate tokens locally first)
- YAZIO: OAuth with correct client credentials (Auth v15, Data v1)

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

# YAZIO credentials
YAZIO_EMAIL = os.getenv("YAZIO_EMAIL")
YAZIO_PASSWORD = os.getenv("YAZIO_PASSWORD")

# YAZIO Client credentials
# Auth is on v15 (yzapi.yazio.com), but data endpoints are on v1 (api.yazio.com)!
YAZIO_AUTH_URL = "https://yzapi.yazio.com/v15"
YAZIO_DATA_URL = "https://api.yazio.com/v1"
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
        token_file = os.path.join(temp_dir, "oauth2_token.json")
        
        with open(token_file, 'w') as f:
            json.dump(token_data, f)
        
        # Also create oauth1_token.json (can be empty)
        oauth1_file = os.path.join(temp_dir, "oauth1_token.json")
        with open(oauth1_file, 'w') as f:
            json.dump({}, f)
        
        # Initialize client WITHOUT credentials
        client = Garmin()
        
        # Load the saved tokens
        client.garth.load(temp_dir)
        
        # Verify the session works
        logger.info("Verifying Garmin session...")
        name = client.get_full_name()
        logger.info(f"Garmin connected as: {name}")
        
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
    
    try:
        # Body Battery
        try:
            bb_data = client.get_body_battery(date_str)
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
            if sleep_data:
                daily_sleep = sleep_data.get("dailySleepDTO", {})
                
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
# YAZIO FUNCTIONS (Auth v15, Data v1)
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
            # Auth uses v15 endpoint on yzapi.yazio.com
            response = await client.post(
                f"{YAZIO_AUTH_URL}/oauth/token",
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
    """Fetch nutrition data from YAZIO for a specific date"""
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
    
    # User ID - can be retrieved from /v15/user endpoint or hardcoded
    user_id = os.getenv("YAZIO_USER_ID", "b0a3c6b18841")
    
    try:
        async with httpx.AsyncClient() as client:
            # Data uses v1 endpoint on api.yazio.com (NOT v15!)
            response = await client.get(
                f"{YAZIO_DATA_URL}/users/{user_id}/diary/{date_str}/summary",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                    "User-Agent": "Yazio/7.3.10 (iPhone; iOS 16.2; Scale/3.00)",
                }
            )
            
            logger.info(f"YAZIO day data response status: {response.status_code}")
            
            if response.status_code == 200:
                day_data = response.json()
                logger.info(f"YAZIO day data keys: {list(day_data.keys())}")
                logger.info(f"YAZIO raw response: {day_data}")
                
                # Try nutrition_daily structure first
                nutrition = day_data.get("nutrition_daily", {})
                if nutrition:
                    data["calories"] = int(nutrition.get("calories", 0) or nutrition.get("energy", 0) or 0)
                    data["protein"] = int(nutrition.get("protein", 0) or nutrition.get("proteins", 0) or 0)
                    data["carbs"] = int(nutrition.get("carbohydrates", 0) or nutrition.get("carbs", 0) or 0)
                    data["fat"] = int(nutrition.get("fat", 0) or 0)
                else:
                    # Fallback to consumed structure
                    consumed = day_data.get("consumed", {})
                    data["calories"] = int(consumed.get("energy_kcal", 0) or consumed.get("calories", 0) or 0)
                    data["protein"] = int(consumed.get("proteins", 0) or consumed.get("protein", 0) or 0)
                    data["carbs"] = int(consumed.get("carbohydrates", 0) or consumed.get("carbs", 0) or 0)
                    data["fat"] = int(consumed.get("fat", 0) or 0)
                
                # Extract goals if available
                goal = day_data.get("goal", {}) or day_data.get("nutrition_goal", {})
                if goal:
                    data["caloriesGoal"] = int(goal.get("calories", 2000) or goal.get("energy_kcal", 2000) or 2000)
                    data["proteinGoal"] = int(goal.get("protein", 150) or goal.get("proteins", 150) or 150)
                    data["carbsGoal"] = int(goal.get("carbohydrates", 250) or goal.get("carbs", 250) or 250)
                    data["fatGoal"] = int(goal.get("fat", 70) or 70)
                
                logger.info(f"YAZIO: {data['calories']} kcal, P:{data['protein']}g, C:{data['carbs']}g, F:{data['fat']}g")
                
            else:
                logger.error(f"YAZIO fetch failed: {response.status_code} - {response.text}")
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


# ============================================
# RUN SERVER
# ============================================

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
