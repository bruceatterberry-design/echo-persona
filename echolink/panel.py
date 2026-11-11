"""The phone's side window: weather and photos (the tablet adds its own calendar and the web sources Echo used)."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

import httpx

from echolink import home as _home

HOME = _home.load()   # config/home.json (your town and its coordinates)
CODES = {0: "Clear", 1: "Mostly clear", 2: "Partly cloudy", 3: "Cloudy", 45: "Fog", 48: "Freezing fog", 51: "Light drizzle",
         53: "Drizzle", 55: "Heavy drizzle", 61: "Light rain", 63: "Rain", 65: "Heavy rain", 66: "Freezing rain",
         67: "Freezing rain", 71: "Light snow", 73: "Snow", 75: "Heavy snow", 77: "Snow grains", 80: "Showers",
         81: "Showers", 82: "Heavy showers", 85: "Snow showers", 86: "Snow showers", 95: "Thunderstorms",
         96: "Thunderstorms, hail", 99: "Thunderstorms, hail"}
_cache: dict = {}


def weather(http=None) -> dict:
    """Now, the next 12 hours and 5 days for home (Open-Meteo: free, no key, nothing about the owner is sent)."""
    if _cache.get("w") and time.time() - _cache["w"][0] < 600:
        return _cache["w"][1]
    http = http or httpx.Client(timeout=15)
    r = http.get("https://api.open-meteo.com/v1/forecast", params={
        "latitude": HOME["lat"], "longitude": HOME["lon"], "timezone": HOME["tz"],
        "temperature_unit": "fahrenheit", "wind_speed_unit": "mph", "precipitation_unit": "inch", "forecast_days": 5,
        "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m,relative_humidity_2m",
        "hourly": "temperature_2m,weather_code,precipitation_probability",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max"})
    r.raise_for_status()
    j = r.json()
    cur, hr, dy = j["current"], j["hourly"], j["daily"]
    now_i = next((i for i, t in enumerate(hr["time"]) if t >= cur["time"][:13]), 0)
    out = {"place": HOME["name"],
           "now": {"temp": round(cur["temperature_2m"]), "feels": round(cur["apparent_temperature"]),
                   "sky": CODES.get(cur["weather_code"], "—"), "wind": round(cur["wind_speed_10m"]),
                   "humidity": cur["relative_humidity_2m"]},
           "hours": [{"time": time.strftime("%-I %p", time.strptime(hr["time"][i], "%Y-%m-%dT%H:%M")),
                      "temp": round(hr["temperature_2m"][i]), "sky": CODES.get(hr["weather_code"][i], "—"),
                      "rain": hr["precipitation_probability"][i]} for i in range(now_i, min(now_i + 12, len(hr["time"])), 2)],
           "days": [{"day": time.strftime("%a", time.strptime(d, "%Y-%m-%d")), "hi": round(dy["temperature_2m_max"][i]),
                     "lo": round(dy["temperature_2m_min"][i]), "sky": CODES.get(dy["weather_code"][i], "—"),
                     "rain": dy["precipitation_probability_max"][i]} for i, d in enumerate(dy["time"])]}
    _cache["w"] = (time.time(), out)
    return out


def photos(dataset_root: str | Path, q: str, limit: int = 12) -> dict:
    """Approved photos from Echo's collections that match what was asked ("the cats", "Christmas", "the lake")."""
    root = Path(dataset_root)
    idx = json.loads((root / "derived" / "photos" / "index-v1.json").read_text())
    cats = idx.get("categories", {})
    words = [w for w in re.findall(r"[a-z]+", (q or "").lower()) if len(w) > 2 and w not in {
        "the", "and", "our", "my", "me", "show", "pictures", "picture", "photos", "photo", "some", "any", "of", "with",
        "from", "see", "look", "pull", "please"}]
    scored = []
    for sha, p in idx.get("photos", {}).items():
        if p.get("status") != "approved":
            continue
        text = " ".join(str(p.get(k) or "") for k in ("caption", "description", "category", "taken")).lower()
        text += " " + str(cats.get(p.get("category"), "")).lower()
        score = sum(2 if w in (p.get("category") or "") else 1 for w in words if w.rstrip("s") in text)
        if score or not words:
            scored.append((score, p.get("taken") or "", sha, p))
    scored.sort(key=lambda x: x[1], reverse=True)          # newest first ...
    scored.sort(key=lambda x: -x[0])                        # ... within the best matches
    items = [{"id": sha[:16], "caption": (p.get("caption") or "").strip() or (p.get("description") or "")[:120],
              "taken": (p.get("taken") or "")[:10], "category": cats.get(p.get("category"), p.get("category"))}
             for _, _, sha, p in scored[:limit]]
    return {"query": q, "items": items, "total": len(scored)}


def photo_file(dataset_root: str | Path, pid: str) -> Path | None:
    if not re.fullmatch(r"[0-9a-f]{16}", pid or ""):
        return None
    f = Path(dataset_root) / "derived" / "photos" / "view-v1" / f"{pid}.jpg"
    return f if f.exists() else None
