from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import List, Optional
from datetime import datetime, timedelta, time as dt_time
import re
import requests 

app = FastAPI(
    title="Simple Smart Hub API",
    description="API for controlling IoT devices and viewing sensor data.",
    version="2.0.0" 
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://simple-smart-hub-client.netlify.app", "http://localhost:3000", "*"], # Be more specific in production than "*"
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

LATITUDE = 17.9696 
LONGITUDE = -76.7936 
MAX_HISTORY_SIZE = 100 

current_settings = {
    "_id": "default_settings_id_123",
    "user_temp": 25.0,               
    "user_light_input": "18:00:00", 
    "light_duration_input": "2h",     
    "light_time_on_actual_utc": "18:00:00", 
    "light_time_off_actual_utc": "20:00:00" 
}

sensor_history: deque = deque(maxlen=MAX_HISTORY_SIZE) 


class SettingsInput(BaseModel):
    user_temp: float = Field(..., example=30.0, description="Temperature threshold for fan (Celsius)")
    user_light: str = Field(..., example="18:30:00 or sunset", description="Time to turn lights on (HH:MM:SS or 'sunset')")
    light_duration: str = Field(..., example="4h", description="Duration for lights to stay on (e.g., 2h, 30m)")

class SettingsOutput(BaseModel):
    _id: str
    user_temp: float
    user_light: str 
    light_time_off: str 

class SensorReadingForGraph(BaseModel):
    temperature: Optional[float] = Field(None, example=29.3)
    presence: bool = Field(example=True)
    datetime: str = Field(example="2023-02-23T18:22:28Z") 

class ESP32SensorInput(BaseModel): 
    temperature: Optional[float] = None 
    presence: bool

class ESP32CommandOutput(BaseModel): 
    light_on: bool
    fan_on: bool


regex_duration = re.compile(r'((?P<hours>\d+?)h)?((?P<minutes>\d+?)m)?((?P<seconds>\d+?)s)?')
def parse_duration_to_timedelta(time_str: str) -> timedelta:
    parts = regex_duration.match(time_str)
    if not parts:
        raise ValueError("Invalid time duration string format. Use 'XhYmZs'.")
    parts = parts.groupdict()
    time_params = {}
    for name, param in parts.items():
        if param:
            time_params[name] = int(param)
    if not time_params:
        raise ValueError("Empty time duration string. Use 'XhYmZs'.")
    return timedelta(**time_params)


def get_actual_sunset_time_utc_str(lat: float, lng: float) -> Optional[str]:
    """Fetches sunset time from api.sunrise-sunset.org and returns as HH:MM:SS string (UTC)."""
    
    url = f"https://api.sunrise-sunset.org/json?lat={lat}&lng={lng}&formatted=0" 
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status() 
        data = response.json()
        if data.get("status") == "OK":
            sunset_utc_iso = data["results"]["sunset"] 
            
            sunset_dt_object = datetime.fromisoformat(sunset_utc_iso) 
            return sunset_dt_object.strftime("%H:%M:%S")
        else:
            print(f"Error from sunset API: {data.get('status')}")
            return None
    except requests.exceptions.RequestException as e:
        print(f"Could not fetch sunset time from API: {e}")
        return None
    except (KeyError, ValueError) as e:
        print(f"Unexpected response format or value from sunset API: {e}")
        return None


def make_control_decisions(temperature_c: Optional[float], presence_detected: bool) -> ESP32CommandOutput:
    global current_settings 

    light_should_be_on = False
    fan_should_be_on = False

    if presence_detected:
        try:
            
            light_on_setting_utc_str = current_settings["light_time_on_actual_utc"]
            light_off_setting_utc_str = current_settings["light_time_off_actual_utc"]

            h_on, m_on, s_on = map(int, light_on_setting_utc_str.split(':'))
            time_on_target_utc = dt_time(h_on, m_on, s_on, tzinfo=None) 

            h_off, m_off, s_off = map(int, light_off_setting_utc_str.split(':'))
            time_off_target_utc = dt_time(h_off, m_off, s_off, tzinfo=None) 

            current_utc_time_naive = datetime.utcnow().time() 

            
            if time_on_target_utc > time_off_target_utc: 
                if current_utc_time_naive >= time_on_target_utc or current_utc_time_naive < time_off_target_utc:
                    light_should_be_on = True
            else: 
                if time_on_target_utc <= current_utc_time_naive < time_off_target_utc:
                    light_should_be_on = True
        except Exception as e:
            print(f"Error in light decision logic: {e}")
            light_should_be_on = False 

        if presence_detected and temperature_c is not None: 
        
                 if temperature_c > current_settings["user_temp"]:
                     fan_should_be_on = True
    
    return ESP32CommandOutput(light_on=light_should_be_on, fan_on=fan_should_be_on)


@app.put("/settings", response_model=SettingsOutput, tags=["User Settings"])
async def update_user_settings_endpoint(new_settings: SettingsInput):
    global current_settings

    actual_light_on_utc_str = ""
    if new_settings.user_light.lower() == "sunset":
        
        sunset_time_utc = get_actual_sunset_time_utc_str(LATITUDE, LONGITUDE)
        if sunset_time_utc:
            actual_light_on_utc_str = sunset_time_utc
        else:
            
            actual_light_on_utc_str = "18:00:00" 
            print(f"Warning: Failed to fetch sunset time for lat={LATITUDE},lng={LONGITUDE}. Using fallback {actual_light_on_utc_str} UTC.")
    else: 
         try:
             dt_time.fromisoformat(new_settings.user_light) 
             actual_light_on_utc_str = new_settings.user_light
         except ValueError:
            raise HTTPException(status_code=400, detail="Invalid user_light time format. Use HH:MM:SS or 'sunset'.")

    
    try:
        
        h, m, s = map(int, actual_light_on_utc_str.split(':'))
        
        light_on_dt_utc = datetime.combine(datetime.utcnow().date(), dt_time(h, m, s))

        duration_td = parse_duration_to_timedelta(new_settings.light_duration)
        light_off_dt_utc = light_on_dt_utc + duration_td
        light_time_off_actual_utc_str = light_off_dt_utc.strftime("%H:%M:%S")

    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid time format or duration: {e}")

    
    current_settings["user_temp"] = new_settings.user_temp
    current_settings["user_light_input"] = new_settings.user_light 
    current_settings["light_duration_input"] = new_settings.light_duration
    current_settings["light_time_on_actual_utc"] = actual_light_on_utc_str
    current_settings["light_time_off_actual_utc"] = light_time_off_actual_utc_str

    print(f"Updated Hub Settings: {current_settings}")

    return SettingsOutput(
        _id=current_settings["_id"],
        user_temp=current_settings["user_temp"],
        user_light=current_settings["light_time_on_actual_utc"], 
        light_time_off=current_settings["light_time_off_actual_utc"]
    )


@app.post("/device_state_update", response_model=ESP32CommandOutput, tags=["ESP32 Communication"])
async def process_esp32_data_and_return_commands(data: ESP32SensorInput):
    reading_for_graph = SensorReadingForGraph(
        temperature=data.temperature,
        presence=data.presence,
        datetime=datetime.utcnow().isoformat(timespec='seconds') + "Z" 
    )
    sensor_history.append(reading_for_graph.model_dump()) 
    print(f"Received from ESP32: Temp={data.temperature}, Presence={data.presence}")

    
    commands_to_esp32 = make_control_decisions(data.temperature, data.presence)
    print(f"Sending commands to ESP32: Light={commands_to_esp32.light_on}, Fan={commands_to_esp32.fan_on}")

    return commands_to_esp32

@app.get("/graph", response_model=List[SensorReadingForGraph], tags=["Sensor Data"])
async def get_graph_data_endpoint(size: int = Query(10, gt=0, le=MAX_HISTORY_SIZE)):
    """Returns the 'n' most recent sensor readings for plotting graphs."""
    return list(sensor_history)[-size:] 

@app.get("/", tags=["General"])
async def read_root():
    return {"message": "Welcome to the Simple Smart Hub API! (v2.0.0)"}

@app.get("/debug_info", tags=["Debugging"]) 
async def get_debug_information():
    return {
        "current_system_settings": current_settings,
        "current_utc_time": datetime.utcnow().isoformat() + "Z",
        "sensor_history_count": len(sensor_history),
        "latest_sensor_reading_for_graph": list(sensor_history)[-1] if sensor_history else None
    }

