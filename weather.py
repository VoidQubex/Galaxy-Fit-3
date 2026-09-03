"""
Weather source for the band.

Location comes from IP geolocation (ip-api.com) unless you set it explicitly in
LOCATION_OVERRIDE below; forecast comes from Open-Meteo (free, no API key).

Note both are third-party HTTP calls: the geolocation lookup necessarily reveals
your IP to ip-api.com, and your coordinates go to open-meteo.com. Set
LOCATION_OVERRIDE to skip the geolocation step.
"""

import json
import time
import urllib.request

# Set to e.g. {"lat": 51.5, "lon": -0.12, "city": "London", "region": "", "country": "UK"}
# to skip IP geolocation entirely.
LOCATION_OVERRIDE = None

_TIMEOUT = 8


def _get_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "fit3-server/1.0"})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8"))


def get_location() -> dict:
    if LOCATION_OVERRIDE:
        return dict(LOCATION_OVERRIDE)
    d = _get_json("http://ip-api.com/json/?fields=status,city,regionName,country,lat,lon")
    if d.get("status") != "success":
        raise RuntimeError(f"geolocation failed: {d}")
    return {"lat": d["lat"], "lon": d["lon"], "city": d.get("city", ""),
            "region": d.get("regionName", ""), "country": d.get("country", "")}


# Open-Meteo uses WMO codes; the band uses Samsung's own icon ids (0-19).
# Mapping mirrors the reference's OWM->Samsung table, via equivalent conditions.
def wmo_to_samsung_icon(code: int) -> int:
    if code == 0:
        return 0                       # clear
    if code in (1, 2):
        return 1                       # mainly/partly cloudy
    if code == 3:
        return 2                       # overcast -> cloudy
    if code in (45, 48):
        return 3                       # fog
    if code in (51, 53, 55):
        return 5                       # drizzle -> showers
    if code in (56, 57):
        return 16                      # freezing drizzle -> ice
    if code == 61:
        return 5                       # slight rain -> showers
    if code in (63, 65):
        return 4                       # rain
    if code in (66, 67):
        return 16                      # freezing rain -> ice
    if code in (71, 77):
        return 10                      # slight snow / grains -> flurries
    if code in (73, 75):
        return 13                      # snow
    if code in (80, 81, 82):
        return 5                       # rain showers
    if code in (85, 86):
        return 13                      # snow showers
    if code in (95, 96, 99):
        return 8                       # thunderstorm
    return 1                           # unknown: partly cloudy, don't claim certainty


def uv_label(uv: float) -> str:
    if uv < 3:
        return "Low"
    if uv < 6:
        return "Moderate"
    if uv < 8:
        return "High"
    if uv < 11:
        return "Very High"
    return "Extreme"


def compass(degrees: float) -> str:
    dirs = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    return dirs[round(degrees / 45.0) % 8]


def _iso_to_epoch(s: str) -> int:
    """Open-Meteo local ISO time ('2026-07-16T13:00') -> epoch seconds."""
    try:
        return int(time.mktime(time.strptime(s, "%Y-%m-%dT%H:%M")))
    except ValueError:
        try:
            return int(time.mktime(time.strptime(s, "%Y-%m-%dT%H:%M:%S")))
        except ValueError:
            return int(time.time())


def get_weather(hourly_limit: int = 6, daily_limit: int = 4) -> dict:
    """Current conditions + short hourly/daily forecast, ready for the band.

    Keeps the forecast short by default: the whole message has to cross a 512-byte
    MTU, and this band is unreliable with heavily fragmented messages.
    """
    loc = get_location()
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={loc['lat']}&longitude={loc['lon']}"
        "&current=temperature_2m,apparent_temperature,relative_humidity_2m,"
        "weather_code,is_day,wind_speed_10m,wind_direction_10m"
        "&hourly=temperature_2m,weather_code,precipitation_probability,is_day"
        "&daily=weather_code,temperature_2m_max,temperature_2m_min,sunrise,sunset,uv_index_max"
        "&timezone=auto&forecast_days=8"
    )
    d = _get_json(url)
    cur = d["current"]
    daily = d["daily"]
    hourly = d["hourly"]
    now = int(time.time())

    # Start the hourly list at the next hour rather than midnight.
    times = [_iso_to_epoch(t) for t in hourly["time"]]
    start = 0
    for i, t in enumerate(times):
        if t >= now:
            start = i
            break

    hours = []
    for i in range(start, min(start + hourly_limit, len(times))):
        hours.append({
            "t": times[i],
            "temp": round(hourly["temperature_2m"][i]),
            "icon": wmo_to_samsung_icon(hourly["weather_code"][i]),
            "precip": hourly["precipitation_probability"][i] or 0,
            "is_day": hourly["is_day"][i],
        })

    days = []
    for i in range(min(daily_limit, len(daily["time"]))):
        days.append({
            "t": _iso_to_epoch(daily["time"][i] + "T12:00"),
            "icon": wmo_to_samsung_icon(daily["weather_code"][i]),
            "max": round(daily["temperature_2m_max"][i]),
            "min": round(daily["temperature_2m_min"][i]),
        })

    return {
        "city": loc["city"], "region": loc["region"], "country": loc["country"],
        "time": now,
        "temp": round(cur["temperature_2m"]),
        "feels_like": round(cur["apparent_temperature"]),
        "humidity": round(cur["relative_humidity_2m"]),
        "icon": wmo_to_samsung_icon(cur["weather_code"]),
        "is_day": cur["is_day"],
        "wind_speed": round(cur["wind_speed_10m"]),
        "wind_dir": compass(cur["wind_direction_10m"]),
        "high": round(daily["temperature_2m_max"][0]),
        "low": round(daily["temperature_2m_min"][0]),
        "sunrise": _iso_to_epoch(daily["sunrise"][0]),
        "sunset": _iso_to_epoch(daily["sunset"][0]),
        "uv": round(daily.get("uv_index_max", [0])[0] or 0),
        "hourly": hours,
        "daily": days,
    }


def summary(w: dict) -> str:
    return (f"{w['city']}: {w['temp']}C (feels {w['feels_like']}C), "
            f"{w['low']}-{w['high']}C, wind {w['wind_speed']}km/h {w['wind_dir']}, "
            f"{len(w['hourly'])}h/{len(w['daily'])}d forecast")
