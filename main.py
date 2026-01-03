"""
Health OS API - Fetches data from Garmin Connect and YAZIO
Deploy on Railway, called by N8N daily
"""

import os
import json
import logging
from datetime import datetime, date
from typing import Optional
import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from garminconnect import Garmin, GarminConnectAuthenticationError

# ============================================
# CONFIGURATION
# ============================================

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Health OS API",
    description="Aggregates health data from Garmin Connect and YAZIO",
    version="1.0.0"
)

# CORS for testing
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

GARMIN_EMAIL = os.getenv("GARMIN_EMAIL")
GARMIN_PASSWORD = os.getenv("GARMIN_PASSWORD")
YAZIO_EMAIL = os.getenv("YAZIO_EMAIL")
YAZIO_PASSWORD = os.getenv("YAZIO_PASSWORD")

# Token storage (in-memory, will reset on restart)
# For production, consider using Redis or a file
garmin_client: Optional[Garmin] = None
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

# ============================================
# GARMIN FUNCTIONS
# ============================================

def get_garmin_client() -> Optional[Garmin]:
    """Initialize or return cached Garmin client"""
    global garmin_client
    
    if not GARMIN_EMAIL or not GARMIN_PASSWORD:
        logger.warning("Garmin credentials not set")
        return None
    
    if garmin_client is not None:
        return garmin_client
    
    try:
        logger.info("Logging into Garmin Connect...")
        client = Garmin(GARMIN_EMAIL, GARMIN_PASSWORD)
        client.login()
        garmin_client = client
        logger.info("Garmin login successful")
        return client
    except GarminConnectAuthenticationError as e:
        logger.error(f"Garmin authentication failed: {e}")
        return None
    except Exception as e:
        logger.error(f"Garmin connection error: {e}")
        return None


def fetch_garmin_data(target_date: date) -> dict:
    """Fetch all relevant Garmin data for a specific date"""
    client = get_garmin_client()
    
    if client is None:
        return {"error": "Garmin not connected"}
    
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
            if bb_data and len(bb_data) > 0:
                # Get the latest body battery value
                latest = bb_data[-1] if isinstance(bb_data, list) else bb_data
                if isinstance(latest, dict) and "chargedValue" in latest:
                    data["bodyBattery"] = latest.get("chargedValue", 0)
                elif isinstance(bb_data, list) and len(bb_data) > 0:
                    # Sometimes it's a list of values
                    for item in reversed(bb_data):
                        if isinstance(item, dict) and item.get("chargedValue"):
                            data["bodyBattery"] = item["chargedValue"]
                            break
            logger.info(f"Body Battery: {data['bodyBattery']}")
        except Exception as e:
            logger.warning(f"Could not fetch body battery: {e}")
        
        # Sleep data
        try:
            sleep_data = client.get_sleep_data(date_str)
            if sleep_data:
                # Sleep score
                daily_sleep = sleep_data.get("dailySleepDTO", {})
                data["sleepScore"] = daily_sleep.get("sleepScores", {}).get("overall", {}).get("value", 0)
                
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
            # Get weight for the past 7 days to calculate change
            from datetime import timedelta
            start_date = (target_date - timedelta(days=7)).strftime("%Y-%m-%d")
            weight_data = client.get_body_composition(start_date, date_str)
            
            if weight_data and "dateWeightList" in weight_data:
                weights = weight_data["dateWeightList"]
                if len(weights) > 0:
                    # Latest weight (in grams, convert to kg)
                    latest_weight = weights[-1].get("weight", 0) / 1000
                    data["weight"] = round(latest_weight, 1)
                    
                    # Calculate change if we have previous data
                    if len(weights) > 1:
                        previous_weight = weights[0].get("weight", 0) / 1000
                        data["weightChange"] = round(latest_weight - previous_weight, 1)
            logger.info(f"Weight: {data['weight']} kg, Change: {data['weightChange']} kg")
        except Exception as e:
            logger.warning(f"Could not fetch weight data: {e}")
        
        return data
        
    except Exception as e:
        logger.error(f"Error fetching Garmin data: {e}")
        return {"error": str(e)}

# ============================================
# YAZIO FUNCTIONS
# ============================================

YAZIO_BASE_URL = "https://yzapi.yazio.com"
YAZIO_CLIENT_ID = "de.yazio.mobile.v8"
YAZIO_CLIENT_SECRET = "b6dfc6b4c76f8285ac94de67c52ca5"  # Public client secret from app

async def yazio_login() -> Optional[dict]:
    """Login to YAZIO and get access token"""
    global yazio_token
    
    if not YAZIO_EMAIL or not YAZIO_PASSWORD:
        logger.warning("YAZIO credentials not set")
        return None
    
    # Check if we have a valid cached token
    if yazio_token and yazio_token.get("expires_at", 0) > datetime.now().timestamp():
        return yazio_token
    
    try:
        logger.info("Logging into YAZIO...")
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{YAZIO_BASE_URL}/v10/oauth/token",
                data={
                    "grant_type": "password",
                    "username": YAZIO_EMAIL,
                    "password": YAZIO_PASSWORD,
                    "client_id": YAZIO_CLIENT_ID,
                    "client_secret": YAZIO_CLIENT_SECRET,
                },
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                }
            )
            
            if response.status_code == 200:
                token_data = response.json()
                # Calculate expiration time
                expires_in = token_data.get("expires_in", 3600)
                token_data["expires_at"] = datetime.now().timestamp() + expires_in
                yazio_token = token_data
                logger.info("YAZIO login successful")
                return token_data
            else:
                logger.error(f"YAZIO login failed: {response.status_code} - {response.text}")
                return None
                
    except Exception as e:
        logger.error(f"YAZIO login error: {e}")
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
    
    try:
        async with httpx.AsyncClient() as client:
            # Fetch daily summary
            response = await client.get(
                f"{YAZIO_BASE_URL}/v10/user/day/{date_str}",
                headers={
                    "Authorization": f"Bearer {token['access_token']}",
                    "Accept": "application/json",
                }
            )
            
            if response.status_code == 200:
                day_data = response.json()
                
                # Extract consumed values
                consumed = day_data.get("consumed", {})
                data["calories"] = int(consumed.get("energy_kcal", 0))
                data["protein"] = int(consumed.get("proteins", 0))
                data["carbs"] = int(consumed.get("carbohydrates", 0))
                data["fat"] = int(consumed.get("fat", 0))
                
                # Extract goals
                goal = day_data.get("goal", {})
                data["caloriesGoal"] = int(goal.get("energy_kcal", 2000))
                data["proteinGoal"] = int(goal.get("proteins", 150))
                data["carbsGoal"] = int(goal.get("carbohydrates", 250))
                data["fatGoal"] = int(goal.get("fat", 70))
                
                logger.info(f"YAZIO data: {data['calories']} kcal, P:{data['protein']}g, C:{data['carbs']}g, F:{data['fat']}g")
            else:
                logger.error(f"YAZIO fetch failed: {response.status_code} - {response.text}")
                return {"error": f"YAZIO API error: {response.status_code}"}
                
        return data
        
    except Exception as e:
        logger.error(f"Error fetching YAZIO data: {e}")
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
        "version": "1.0.0",
        "timestamp": datetime.now().isoformat()
    }


@app.get("/sync", response_model=HealthData)
async def sync_health_data(date_str: Optional[str] = None):
    """
    Fetch and aggregate health data from all sources.
    
    - **date_str**: Optional date in YYYY-MM-DD format. Defaults to today.
    """
    # Parse date
    if date_str:
        try:
            target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD")
    else:
        target_date = date.today()
    
    errors = []
    
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
    
    health_data.errors = errors
    
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


# ============================================
# RUN SERVER
# ============================================

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
