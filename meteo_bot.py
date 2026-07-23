#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Personal Telegram weather bot.
Data source: Open-Meteo (free, no API key, non-commercial use).

Commands:
  wx <loc> [days] -> forecast; loc = city, "lat,lon", or a saved slot number.
                     No days = 24h hourly; a number 1..16 = daily forecast.
                     "wx 3" (bare number) = 3-day forecast for all saved locations.
  soil <loc>      -> current soil moisture (depth bands) + soil/air data
  hist <loc> <start> <end> -> past daily weather for a period (YYYY-MM-DD)
  model           -> show the current model + list available models
  model <name>    -> set the default model (persisted, per chat)
  save <n> <loc>  -> save a city or "lat,lon" in slot n
  locs            -> list saved locations
  del <n>         -> delete saved location in slot n
  alerts          -> check saved locations now and report (manual test)
  set <param> <v> -> adjust an alert threshold (gust/rain/snow/heat/frost)
  units <q> <u>   -> set display units (temp/wind/rain/pressure)

The bot ALSO checks saved locations automatically in the background and sends
you a message when a threshold is crossed within the next few hours.

==============================================================================
SETUP
==============================================================================

1) Create the bot
   In Telegram, open @BotFather -> /newbot -> pick a name and a username
   ending in "bot". BotFather returns a TOKEN like:
       123456789:AAExxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

2) Provide the token via the TG_BOT_TOKEN environment variable, e.g.:
       export TG_BOT_TOKEN="123456789:AAExxxxxxxxxxxx"     # Linux shell
   or, in a systemd EnvironmentFile / .env file (no quotes, no 'export'):
       TG_BOT_TOKEN=123456789:AAExxxxxxxxxxxx

3) (Optional) Restrict the bot to your own account
   Set TG_ALLOWED_USERS to a comma-separated list of numeric Telegram user IDs.
   Only those users will get replies; everyone else is ignored.
       export TG_ALLOWED_USERS="123456789"                # only you
       export TG_ALLOWED_USERS="123456789,987654321"      # a few people
   Leave it empty/unset to let the bot answer anyone who messages it.

   How to find your numeric ID:
     - message @userinfobot in Telegram, OR
     - message this bot once and read the console/log line it prints:
           [msg] chat_id=<...> user_id=<...>: '...'
       the user_id value is what goes into TG_ALLOWED_USERS.

4) Install the dependency and run
       pip install requests
       python3 meteo_bot.py

==============================================================================
ALERTS
==============================================================================
Enabled phenomena and their thresholds are the constants below
(GUST_KMH, RAIN_MM_H, SNOW_CM_H, HEAT_C, FROST_C) - edit to taste.
The background checker runs every TG_ALERT_INTERVAL seconds (default 1800 = 30 min)
and looks ALERT_WINDOW_H hours ahead. To avoid spam, each phenomenon fires at
most once per location per day. State (model, saved locations, sent-alert marks)
is stored next to this script in bot_state.json, so run from a writable folder.
"""

import os
import time
import json
import re
import threading
import requests
from datetime import datetime, timezone, timedelta

# --- Config ---
BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
STATE_FILE = os.environ.get("TG_BOT_STATE", "bot_state.json")
ALLOWED_USERS = {
    s.strip() for s in os.environ.get("TG_ALLOWED_USERS", "").split(",") if s.strip()
}
TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

GEO_URL = "https://geocoding-api.open-meteo.com/v1/search"
FC_URL = "https://api.open-meteo.com/v1/forecast"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"  # ERA5 history
DAILY_MAX = 16  # Open-Meteo daily forecast horizon
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# --- Alert configuration (edit thresholds here) ---
CHECK_INTERVAL_SEC = int(os.environ.get("TG_ALERT_INTERVAL", "1800"))  # 30 min
ALERT_WINDOW_H = 12          # how many hours ahead to scan
GUST_KMH = 60                # strong wind gusts
RAIN_MM_H = 4.0              # heavy rain per hour
SNOW_CM_H = 1.0              # snowfall per hour
HEAT_C = 35                  # heat
FROST_C = 0                  # frost (temperature <= this)
ALERTS_ENABLED = {"gust", "rain", "snow", "heat", "frost"}  # trim if you want fewer

# Defaults above are the baseline; per-chat "set" overrides are layered on top.
DEFAULT_THRESHOLDS = {
    "gust": float(GUST_KMH), "rain": float(RAIN_MM_H), "snow": float(SNOW_CM_H),
    "heat": float(HEAT_C), "frost": float(FROST_C),
}
THRESH_UNIT = {"gust": "km/h", "rain": "mm/h", "snow": "cm/h", "heat": "\u00b0C", "frost": "\u00b0C"}

# --- Open-Meteo models: short name -> (API id, description) ---
MODELS = {
    "auto":   ("best_match",           "Auto (Open-Meteo picks the best model per location)"),
    "icon":   ("icon_seamless",        "DWD ICON seamless (falls back to ICON-EU here)"),
    "iconeu": ("icon_eu",              "DWD ICON-EU 7 km - covers Romania"),
    "ecmwf":  ("ecmwf_ifs025",         "ECMWF IFS 0.25 deg"),
    "gfs":    ("gfs_seamless",         "NOAA GFS"),
    "arpege": ("meteofrance_seamless", "Meteo-France ARPEGE/AROME"),
    "ukmo":   ("ukmo_seamless",        "UK Met Office"),
    "gem":    ("gem_seamless",         "Canada GEM"),
    "jma":    ("jma_seamless",         "Japan JMA"),
}
DEFAULT_MODEL = "auto"

# --- Display units (per-chat via the "units" command) ---
DEFAULT_UNITS = {"temp": "C", "wind": "kmh", "rain": "mm", "pressure": "hpa"}
# allowed internal value -> label shown to the user
UNIT_LABELS = {
    "temp": {"C": "\u00b0C", "F": "\u00b0F"},
    "wind": {"kmh": "km/h", "ms": "m/s", "mph": "mph", "kn": "kn"},
    "rain": {"mm": "mm", "inch": "in"},
    "pressure": {"hpa": "hPa", "mmhg": "mmHg", "inhg": "inHg"},
}
# accepted user input -> internal value
UNIT_ALIASES = {
    "temp": {"c": "C", "celsius": "C", "f": "F", "fahrenheit": "F"},
    "wind": {"kmh": "kmh", "kph": "kmh", "ms": "ms", "mps": "ms",
             "mph": "mph", "kn": "kn", "kt": "kn", "knots": "kn"},
    "rain": {"mm": "mm", "inch": "inch", "in": "inch"},
    "pressure": {"hpa": "hpa", "mbar": "hpa", "mmhg": "mmhg", "inhg": "inhg"},
}
OM_TEMP = {"C": "celsius", "F": "fahrenheit"}       # Open-Meteo temperature_unit
OM_PRECIP = {"mm": "mm", "inch": "inch"}            # Open-Meteo precipitation_unit
# Open-Meteo has no pressure unit; we convert from hPa ourselves.
PRESSURE_FACTOR = {"hpa": 1.0, "mmhg": 0.750062, "inhg": 0.0295300}

# --- Persistent state (per-chat) with a lock for the background thread ---
_lock = threading.Lock()

def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def _save_state_atomic(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_FILE)  # atomic on POSIX -> no torn reads

def update_state(mutator):
    """Load, mutate and save the state atomically under a lock."""
    with _lock:
        state = load_state()
        mutator(state)
        _save_state_atomic(state)

def get_model(chat_id):
    return load_state().get(str(chat_id), {}).get("model", DEFAULT_MODEL)

def set_model(chat_id, model_key):
    def m(state):
        state.setdefault(str(chat_id), {})["model"] = model_key
    update_state(m)

def get_thresholds(chat_id):
    t = dict(DEFAULT_THRESHOLDS)
    t.update(load_state().get(str(chat_id), {}).get("thresholds", {}))
    return t

def set_threshold(chat_id, param, value):
    def m(state):
        state.setdefault(str(chat_id), {}).setdefault("thresholds", {})[param] = value
    update_state(m)

def get_units(chat_id):
    u = dict(DEFAULT_UNITS)
    u.update(load_state().get(str(chat_id), {}).get("units", {}))
    return u

def set_unit(chat_id, dim, value):
    def m(state):
        state.setdefault(str(chat_id), {}).setdefault("units", {})[dim] = value
    update_state(m)

def fmt_pressure(hpa, unit):
    if hpa is None:
        return None
    v = hpa * PRESSURE_FACTOR[unit]
    return f"{v:.2f}" if unit == "inhg" else f"{round(v)}"

# --- WMO weather codes -> description + emoji ---
WMO = {
    0: ("Clear", "\u2600\ufe0f"), 1: ("Mostly clear", "\U0001f324\ufe0f"),
    2: ("Partly cloudy", "\u26c5"), 3: ("Overcast", "\u2601\ufe0f"),
    45: ("Fog", "\U0001f32b\ufe0f"), 48: ("Rime fog", "\U0001f32b\ufe0f"),
    51: ("Light drizzle", "\U0001f326\ufe0f"), 53: ("Drizzle", "\U0001f326\ufe0f"),
    55: ("Dense drizzle", "\U0001f326\ufe0f"), 56: ("Freezing drizzle", "\U0001f326\ufe0f"),
    57: ("Dense freezing drizzle", "\U0001f326\ufe0f"),
    61: ("Light rain", "\U0001f327\ufe0f"), 63: ("Rain", "\U0001f327\ufe0f"),
    65: ("Heavy rain", "\U0001f327\ufe0f"), 66: ("Freezing rain", "\U0001f327\ufe0f"),
    67: ("Heavy freezing rain", "\U0001f327\ufe0f"),
    71: ("Light snow", "\U0001f328\ufe0f"), 73: ("Snow", "\U0001f328\ufe0f"),
    75: ("Heavy snow", "\u2744\ufe0f"), 77: ("Snow grains", "\U0001f328\ufe0f"),
    80: ("Light showers", "\U0001f326\ufe0f"), 81: ("Showers", "\U0001f327\ufe0f"),
    82: ("Violent showers", "\u26c8\ufe0f"),
    85: ("Snow showers", "\U0001f328\ufe0f"), 86: ("Heavy snow showers", "\u2744\ufe0f"),
    95: ("Thunderstorm", "\u26c8\ufe0f"), 96: ("Thunderstorm w/ hail", "\u26c8\ufe0f"),
    99: ("Severe thunderstorm w/ hail", "\u26c8\ufe0f"),
}

def wmo_desc(code):
    return WMO.get(code, ("\u2014", "\u2753"))

def loc_label(loc):
    lbl = loc.get("name", "")
    if loc.get("country"):
        lbl += f", {loc['country']}"
    return lbl

# --- Open-Meteo: geocode city -> coordinates ---
def geocode(city):
    r = requests.get(GEO_URL, params={
        "name": city, "count": 1, "language": "en", "format": "json"
    }, timeout=15)
    r.raise_for_status()
    res = r.json().get("results")
    if not res:
        return None
    g = res[0]
    return {"name": g["name"], "country": g.get("country", ""),
            "admin": g.get("admin1", ""), "lat": g["latitude"], "lon": g["longitude"]}

def parse_coords(text):
    """Accept 'lat,lon' or 'lat lon' -> (lat, lon) or None."""
    m = re.match(r"^\s*(-?\d+(?:\.\d+)?)\s*[,\s]\s*(-?\d+(?:\.\d+)?)\s*$", text)
    if not m:
        return None
    lat, lon = float(m.group(1)), float(m.group(2))
    if -90 <= lat <= 90 and -180 <= lon <= 180:
        return (lat, lon)
    return None

def coords_or_city(text):
    """Location from raw coordinates or a city name (used by save/wx/soil/hist)."""
    c = parse_coords(text)
    if c:
        return {"name": f"{c[0]:.4f},{c[1]:.4f}", "country": "", "lat": c[0], "lon": c[1]}
    return geocode(text)

def resolve_location(text, chat_id):
    """Coordinates, a saved-slot number, or a city name -> loc dict or None."""
    text = text.strip()
    if parse_coords(text):
        return coords_or_city(text)
    if text.isdigit():
        saved = load_state().get(str(chat_id), {}).get("locations", {}).get(text)
        if saved:
            return saved
    return geocode(text)

# --- Open-Meteo: hourly forecast for the wx command ---
def forecast(lat, lon, model_id, units):
    params = {
        "latitude": lat, "longitude": lon,
        "hourly": "temperature_2m,apparent_temperature,precipitation_probability,"
                  "precipitation,weather_code,wind_speed_10m,wind_gusts_10m,surface_pressure",
        "forecast_days": 2, "timezone": "auto",
        "temperature_unit": OM_TEMP[units["temp"]],
        "wind_speed_unit": units["wind"],
        "precipitation_unit": OM_PRECIP[units["rain"]],
    }
    if model_id and model_id != "best_match":
        params["models"] = model_id
    r = requests.get(FC_URL, params=params, timeout=15)
    r.raise_for_status()
    return r.json()

def format_24h(city, data, model_label, units):
    h = data.get("hourly", {})
    times = h.get("time", [])
    if not times:
        return "The selected model returns no data for this point."
    n = len(times)
    temp_a = h.get("temperature_2m", [None] * n)
    feel_a = h.get("apparent_temperature", [None] * n)
    pop_a = h.get("precipitation_probability", [None] * n)
    prec_a = h.get("precipitation", [None] * n)
    code_a = h.get("weather_code", [None] * n)
    wind_a = h.get("wind_speed_10m", [None] * n)
    gust_a = h.get("wind_gusts_10m", [None] * n)
    pres_a = h.get("surface_pressure", [None] * n)

    tlab = UNIT_LABELS["temp"][units["temp"]]
    wlab = UNIT_LABELS["wind"][units["wind"]]
    rlab = UNIT_LABELS["rain"][units["rain"]]
    plab = UNIT_LABELS["pressure"][units["pressure"]]

    off = timedelta(seconds=data.get("utc_offset_seconds", 0))
    now_local = (datetime.now(timezone.utc) + off).replace(
        tzinfo=None, minute=0, second=0, microsecond=0)
    start = 0
    for i, t in enumerate(times):
        if datetime.fromisoformat(t) >= now_local:
            start = i
            break

    lines = [f"\U0001f4cd <b>{city}</b> \u2014 24h forecast",
             f"source: Open-Meteo \u00b7 model: <i>{model_label}</i>\n"]
    for i in range(start, min(start + 24, n)):
        t = datetime.fromisoformat(times[i])
        temp = str(round(temp_a[i])) if temp_a[i] is not None else "\u2014"
        feel = str(round(feel_a[i])) if feel_a[i] is not None else "\u2014"
        pop = pop_a[i]
        pop_s = f"{pop}%" if pop is not None else "\u2014"
        amt = prec_a[i]
        amt_s = f" {amt:g}{rlab}" if amt else ""      # only when there is precip
        wind = str(round(wind_a[i])) if wind_a[i] is not None else "\u2014"
        gust = str(round(gust_a[i])) if gust_a[i] is not None else "\u2014"
        pv = fmt_pressure(pres_a[i], units["pressure"])
        pres_s = f"  {pv} {plab}" if pv is not None else ""
        desc, emo = wmo_desc(code_a[i]) if code_a[i] is not None else ("", "")
        lines.append(
            f"{t:%H:%M} {emo} {temp}{tlab} (feels {feel}\u00b0)  "
            f"\U0001f4a7{pop_s}{amt_s}  \U0001f4a8{wind} {wlab} (gust {gust}){pres_s}  {desc}"
        )
    return "\n".join(lines)

# --- Open-Meteo: multi-day daily forecast ---
def forecast_daily(lat, lon, model_id, units, days):
    params = {
        "latitude": lat, "longitude": lon,
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,"
                 "precipitation_sum,precipitation_probability_max,"
                 "wind_speed_10m_max,wind_gusts_10m_max",
        "forecast_days": max(1, min(days, DAILY_MAX)), "timezone": "auto",
        "temperature_unit": OM_TEMP[units["temp"]],
        "wind_speed_unit": units["wind"],
        "precipitation_unit": OM_PRECIP[units["rain"]],
    }
    if model_id and model_id != "best_match":
        params["models"] = model_id
    r = requests.get(FC_URL, params=params, timeout=15)
    r.raise_for_status()
    return r.json()

def format_daily(city, data, model_label, units, days):
    d = data.get("daily", {})
    times = d.get("time", [])
    if not times:
        return "The selected model returns no daily data for this point."
    n = len(times)
    code = d.get("weather_code", [None] * n)
    tmax = d.get("temperature_2m_max", [None] * n)
    tmin = d.get("temperature_2m_min", [None] * n)
    psum = d.get("precipitation_sum", [None] * n)
    pprob = d.get("precipitation_probability_max", [None] * n)
    wmax = d.get("wind_speed_10m_max", [None] * n)
    gmax = d.get("wind_gusts_10m_max", [None] * n)
    tlab = UNIT_LABELS["temp"][units["temp"]]
    wlab = UNIT_LABELS["wind"][units["wind"]]
    rlab = UNIT_LABELS["rain"][units["rain"]]
    span = min(days, n)
    lines = [f"\U0001f4cd <b>{city}</b> \u2014 {span}-day forecast",
             f"source: Open-Meteo \u00b7 model: <i>{model_label}</i>\n"]
    for i in range(span):
        dt = datetime.fromisoformat(times[i])
        wd = WEEKDAYS[dt.weekday()]
        desc, emo = wmo_desc(code[i]) if code[i] is not None else ("", "")
        hi = str(round(tmax[i])) if tmax[i] is not None else "\u2014"
        lo = str(round(tmin[i])) if tmin[i] is not None else "\u2014"
        pr = pprob[i]
        pr_s = f"{pr}%" if pr is not None else "\u2014"
        ps = psum[i]
        ps_s = f" {ps:g}{rlab}" if ps else ""
        wnd = str(round(wmax[i])) if wmax[i] is not None else "\u2014"
        gst = str(round(gmax[i])) if gmax[i] is not None else "\u2014"
        lines.append(
            f"{wd} {dt:%d.%m} {emo} {lo}/{hi}{tlab}  "
            f"\U0001f4a7{pr_s}{ps_s}  \U0001f4a8{wnd} {wlab} (gust {gst})  {desc}"
        )
    return "\n".join(lines)


    params = {
        "latitude": lat, "longitude": lon,
        "hourly": "temperature_2m,precipitation,snowfall,weather_code,"
                  "wind_gusts_10m,wind_speed_10m",
        "forecast_days": 2, "timezone": "auto",
    }
    if model_id and model_id != "best_match":
        params["models"] = model_id
    r = requests.get(FC_URL, params=params, timeout=15)
    r.raise_for_status()
    return r.json()

def local_today(data):
    off = timedelta(seconds=data.get("utc_offset_seconds", 0))
    return (datetime.now(timezone.utc) + off).strftime("%Y-%m-%d")

def evaluate_alerts(data, thresholds):
    """Return {phenom: (peak_value, 'HH:MM')} for anything crossing a threshold
    within the next ALERT_WINDOW_H hours. `thresholds` is a dict like
    {"gust":60,"rain":4,"snow":1,"heat":35,"frost":0}."""
    h = data.get("hourly", {})
    times = h.get("time", [])
    if not times:
        return {}
    n = len(times)
    temp_a = h.get("temperature_2m", [None] * n)
    rain_a = h.get("precipitation", [None] * n)
    snow_a = h.get("snowfall", [None] * n)
    gust_a = h.get("wind_gusts_10m", [None] * n)

    off = timedelta(seconds=data.get("utc_offset_seconds", 0))
    now_local = (datetime.now(timezone.utc) + off).replace(
        tzinfo=None, minute=0, second=0, microsecond=0)
    end = now_local + timedelta(hours=ALERT_WINDOW_H)

    trig = {}
    def track(phenom, val, tstr, mode):
        cur = trig.get(phenom)
        if cur is None or (mode == "max" and val > cur[0]) or (mode == "min" and val < cur[0]):
            trig[phenom] = (val, tstr)

    for i, t in enumerate(times):
        th = datetime.fromisoformat(t)
        if th < now_local:
            continue
        if th > end:
            break
        hhmm = f"{th:%H:%M}"
        g = gust_a[i]
        if g is not None and g >= thresholds["gust"]:
            track("gust", g, hhmm, "max")
        r = rain_a[i]
        if r is not None and r >= thresholds["rain"]:
            track("rain", r, hhmm, "max")
        s = snow_a[i]
        if s is not None and s >= thresholds["snow"]:
            track("snow", s, hhmm, "max")
        tp = temp_a[i]
        if tp is not None and tp >= thresholds["heat"]:
            track("heat", tp, hhmm, "max")
        if tp is not None and tp <= thresholds["frost"]:
            track("frost", tp, hhmm, "min")
    return trig

def format_alert_line(phenom, val, tstr):
    if phenom == "gust":
        return f"\U0001f4a8 Strong gusts: up to {round(val)} km/h around {tstr}"
    if phenom == "rain":
        return f"\U0001f327\ufe0f Heavy rain: {val:.1f} mm/h around {tstr}"
    if phenom == "snow":
        return f"\u2744\ufe0f Snowfall: {val:.1f} cm/h around {tstr}"
    if phenom == "heat":
        return f"\U0001f525 Heat: {round(val)}\u00b0C around {tstr}"
    if phenom == "frost":
        return f"\U0001f9ca Frost: {round(val)}\u00b0C around {tstr}"
    return f"{phenom}: {val} at {tstr}"

def already_sent(chat_id, key):
    return key in load_state().get(str(chat_id), {}).get("alerts_sent", {})

def mark_sent(chat_id, key, today):
    def m(state):
        c = state.setdefault(str(chat_id), {})
        sent = c.setdefault("alerts_sent", {})
        for k in list(sent.keys()):     # prune marks from previous days
            if not k.endswith(today):
                del sent[k]
        sent[key] = True
    update_state(m)

# --- Commands ---
def cmd_wx(args, chat_id):
    if not args:
        return ("Usage:\n<code>wx Orsova</code> \u2014 24h hourly\n"
                "<code>wx 44.816,29.879</code> \u2014 by coordinates\n"
                "<code>wx Orsova 3</code> \u2014 3-day forecast\n"
                "<code>wx 3</code> \u2014 3-day for all saved locations")
    units = get_units(chat_id)
    model_id, model_label = MODELS.get(get_model(chat_id), MODELS[DEFAULT_MODEL])
    toks = list(args)

    # trailing number = day count (1..16)
    days = None
    if toks and toks[-1].isdigit() and 1 <= int(toks[-1]) <= DAILY_MAX:
        days = int(toks[-1])
        toks = toks[:-1]     # may become empty -> bare number -> saved locations
    loc_text = " ".join(toks).strip()

    # bare number: N-day for every saved location
    if not loc_text:
        if days is None:
            return "Usage: <code>wx Orsova</code> or <code>wx Orsova 3</code>"
        locs = load_state().get(str(chat_id), {}).get("locations", {})
        if not locs:
            return "No saved locations. Add one (<code>save 1 Orsova</code>) or use <code>wx Orsova 3</code>"
        out = []
        for slot in sorted(locs, key=int):
            loc = locs[slot]
            data = forecast_daily(loc["lat"], loc["lon"], model_id, units, days)
            out.append(format_daily(loc_label(loc), data, model_label, units, days))
        return "\n\n".join(out)

    loc = resolve_location(loc_text, chat_id)
    if not loc:
        return f"Location not found: {loc_text}"
    if days is None:
        data = forecast(loc["lat"], loc["lon"], model_id, units)
        return format_24h(loc_label(loc), data, model_label, units)
    data = forecast_daily(loc["lat"], loc["lon"], model_id, units, days)
    return format_daily(loc_label(loc), data, model_label, units, days)

def cmd_model(args, chat_id):
    current = get_model(chat_id)
    if not args:
        lines = [f"Current model: <b>{current}</b>\n", "Available models:"]
        for k, (mid, desc) in MODELS.items():
            mark = " \u2705" if k == current else ""
            lines.append(f"<code>{k}</code> \u2014 {desc}{mark}")
        lines.append("\nChange with: <code>model icon</code>")
        return "\n".join(lines)
    key = args[0].lower()
    if key not in MODELS:
        return f"Unknown model: <code>{key}</code>\nOptions: {', '.join(MODELS.keys())}"
    set_model(chat_id, key)
    return f"Default model set: <b>{key}</b> \u2014 {MODELS[key][1]}"

def cmd_save(args, chat_id):
    if len(args) < 2 or not args[0].isdigit():
        return ("Usage: <code>save 1 Orsova</code>\n"
                "or by coordinates: <code>save 1 44.816,29.879</code>")
    slot = args[0]
    loc_text = " ".join(args[1:]).strip()
    loc = coords_or_city(loc_text)
    if not loc:
        return f"Location \u201c{loc_text}\u201d not found. Check the spelling."
    entry = {"name": loc["name"], "country": loc.get("country", ""),
             "lat": loc["lat"], "lon": loc["lon"]}
    def m(state):
        state.setdefault(str(chat_id), {}).setdefault("locations", {})[slot] = entry
    update_state(m)
    return f"Saved slot <b>{slot}</b>: {loc_label(entry)}"

def cmd_locs(args, chat_id):
    locations = load_state().get(str(chat_id), {}).get("locations", {})
    if not locations:
        return "No saved locations. Add one with: <code>save 1 Orsova</code>"
    lines = ["Saved locations:"]
    for slot in sorted(locations, key=int):
        lines.append(f"<b>{slot}</b> \u2014 {loc_label(locations[slot])}")
    return "\n".join(lines)

def cmd_del(args, chat_id):
    if not args or not args[0].isdigit():
        return "Usage: <code>del 1</code>"
    slot = args[0]
    removed = [False]
    def m(state):
        locs = state.get(str(chat_id), {}).get("locations", {})
        if slot in locs:
            del locs[slot]
            removed[0] = True
    update_state(m)
    return f"Deleted slot {slot}." if removed[0] else f"Slot {slot} not found."

def cmd_alerts(args, chat_id):
    cdata = load_state().get(str(chat_id), {})
    locations = cdata.get("locations", {})
    if not locations:
        return "No saved locations. Add one with: <code>save 1 Orsova</code>"
    model_id, model_label = MODELS.get(cdata.get("model", DEFAULT_MODEL), MODELS[DEFAULT_MODEL])
    thr = get_thresholds(chat_id)
    out = []
    for slot in sorted(locations, key=int):
        loc = locations[slot]
        try:
            data = fetch_alert_forecast(loc["lat"], loc["lon"], model_id)
        except requests.RequestException:
            out.append(f"{loc_label(loc)}: fetch error")
            continue
        trig = evaluate_alerts(data, thr)
        active = {p: v for p, v in trig.items() if p in ALERTS_ENABLED}
        if not active:
            out.append(f"\u2705 {loc_label(loc)}: nothing in next {ALERT_WINDOW_H}h")
        else:
            block = [f"\u26a0\ufe0f <b>{loc_label(loc)}</b>:"]
            for p, (val, tstr) in active.items():
                block.append("  " + format_alert_line(p, val, tstr))
            out.append("\n".join(block))
    return "\n\n".join(out) + f"\n\nsource: Open-Meteo \u00b7 model: {model_label}"

def cmd_set(args, chat_id):
    thr = get_thresholds(chat_id)
    if not args:
        lines = ["Current alert thresholds:"]
        for p in ("gust", "rain", "snow", "heat", "frost"):
            lines.append(f"<code>{p}</code> {thr[p]:g} {THRESH_UNIT[p]}")
        lines.append("\nChange with: <code>set gust 70</code>")
        return "\n".join(lines)
    param = args[0].lower()
    if param not in DEFAULT_THRESHOLDS:
        return f"Unknown parameter: <code>{param}</code>\nOptions: {', '.join(DEFAULT_THRESHOLDS)}"
    if len(args) < 2:
        return f"Usage: <code>set {param} 70</code>"
    try:
        value = float(args[1].replace(",", "."))
    except ValueError:
        return f"Value must be a number, e.g. <code>set {param} 70</code>"
    set_threshold(chat_id, param, value)
    return f"Threshold set: <b>{param}</b> = {value:g} {THRESH_UNIT[param]}"

def cmd_units(args, chat_id):
    u = get_units(chat_id)
    if not args:
        lines = ["Current display units:"]
        for dim in ("temp", "wind", "rain", "pressure"):
            lines.append(f"<code>{dim}</code> {UNIT_LABELS[dim][u[dim]]}")
        lines.append("\nExamples:\n<code>units temp F</code>\n"
                     "<code>units wind ms</code>\n<code>units rain inch</code>\n"
                     "<code>units pressure mmhg</code>")
        return "\n".join(lines)
    dim = args[0].lower()
    if dim not in DEFAULT_UNITS:
        return "Unknown quantity. Options: temp, wind, rain, pressure"
    if len(args) < 2:
        opts = ", ".join(UNIT_LABELS[dim].keys())
        return f"Usage: <code>units {dim} VALUE</code>\nValues: {opts}"
    val = UNIT_ALIASES[dim].get(args[1].lower())
    if val is None:
        opts = ", ".join(UNIT_LABELS[dim].keys())
        return f"Unknown value for {dim}: <code>{args[1]}</code>\nValues: {opts}"
    set_unit(chat_id, dim, val)
    return f"Unit set: <b>{dim}</b> = {UNIT_LABELS[dim][val]}"

def fetch_soil(lat, lon, model_id):
    params = {
        "latitude": lat, "longitude": lon,
        "hourly": "soil_moisture_0_to_1cm,soil_moisture_1_to_3cm,soil_moisture_3_to_9cm,"
                  "soil_moisture_9_to_27cm,soil_moisture_27_to_81cm,"
                  "soil_temperature_0cm,relative_humidity_2m",
        "forecast_days": 1, "timezone": "auto",
    }
    if model_id and model_id != "best_match":
        params["models"] = model_id
    r = requests.get(FC_URL, params=params, timeout=15)
    r.raise_for_status()
    return r.json()

def cmd_soil(args, chat_id):
    if not args:
        return "Usage: <code>soil Orsova</code> | <code>soil 44.8,29.9</code> | <code>soil 1</code>"
    loc = resolve_location(" ".join(args), chat_id)
    if not loc:
        return "Location not found."
    model_id = MODELS.get(get_model(chat_id), MODELS[DEFAULT_MODEL])[0]
    data = fetch_soil(loc["lat"], loc["lon"], model_id)
    h = data.get("hourly", {})
    times = h.get("time", [])
    if not times:
        return "The selected model has no soil data for this point (try model auto)."
    off = timedelta(seconds=data.get("utc_offset_seconds", 0))
    now = (datetime.now(timezone.utc) + off).replace(tzinfo=None, minute=0, second=0, microsecond=0)
    idx = 0
    for i, t in enumerate(times):
        if datetime.fromisoformat(t) >= now:
            idx = i
            break

    def val(key, scale=1.0, unit="", dec=0):
        a = h.get(key)
        if not a or idx >= len(a) or a[idx] is None:
            return "\u2014"
        v = a[idx] * scale
        return f"{v:.{dec}f}{unit}"

    lines = [f"\U0001f4cd <b>{loc_label(loc)}</b> \u2014 soil & moisture",
             f"time: {times[idx][-5:]} \u00b7 source: Open-Meteo\n",
             f"\U0001f321 soil temp 0cm: {val('soil_temperature_0cm', 1, '\u00b0C', 1)}",
             f"\U0001f4a7 air humidity: {val('relative_humidity_2m', 1, '%')}",
             "soil moisture (vol. water):",
             f"  0\u20131cm: {val('soil_moisture_0_to_1cm', 100, '%')}",
             f"  1\u20133cm: {val('soil_moisture_1_to_3cm', 100, '%')}",
             f"  3\u20139cm: {val('soil_moisture_3_to_9cm', 100, '%')}",
             f"  9\u201327cm: {val('soil_moisture_9_to_27cm', 100, '%')}",
             f"  27\u201381cm: {val('soil_moisture_27_to_81cm', 100, '%')}"]
    return "\n".join(lines)

def cmd_hist(args, chat_id):
    if len(args) < 3:
        return ("Usage: <code>hist Orsova 2025-07-01 2025-07-10</code>\n"
                "(location can be a city, coordinates, or a saved slot number)")
    start, end = args[-2], args[-1]
    datep = r"^\d{4}-\d{2}-\d{2}$"
    if not re.match(datep, start) or not re.match(datep, end):
        return "Dates must be YYYY-MM-DD. Ex: <code>hist Orsova 2025-07-01 2025-07-10</code>"
    loc_text = " ".join(args[:-2]).strip()
    if not loc_text:
        return "Specify a location. Ex: <code>hist Orsova 2025-07-01 2025-07-10</code>"
    loc = resolve_location(loc_text, chat_id)
    if not loc:
        return f"Location not found: {loc_text}"
    units = get_units(chat_id)
    params = {
        "latitude": loc["lat"], "longitude": loc["lon"],
        "start_date": start, "end_date": end,
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,wind_gusts_10m_max",
        "timezone": "auto",
        "temperature_unit": OM_TEMP[units["temp"]],
        "wind_speed_unit": units["wind"],
        "precipitation_unit": OM_PRECIP[units["rain"]],
    }
    r = requests.get(ARCHIVE_URL, params=params, timeout=30)
    r.raise_for_status()
    d = r.json().get("daily", {})
    times = d.get("time", [])
    if not times:
        return "No historical data for that period (ERA5 archive lags ~5 days)."
    tmax = d.get("temperature_2m_max", [])
    tmin = d.get("temperature_2m_min", [])
    psum = d.get("precipitation_sum", [])
    gmax = d.get("wind_gusts_10m_max", [])
    tlab = UNIT_LABELS["temp"][units["temp"]]
    rlab = UNIT_LABELS["rain"][units["rain"]]
    wlab = UNIT_LABELS["wind"][units["wind"]]

    def clean(a):
        return [x for x in a if x is not None]
    def avg(a):
        xs = clean(a)
        return sum(xs) / len(xs) if xs else None
    def g(f, a):
        xs = clean(a)
        return f(xs) if xs else None
    def num(v, dec=1):
        return f"{v:.{dec}f}" if v is not None else "\u2014"

    tot_p = sum(clean(psum))
    lines = [f"\U0001f4cd <b>{loc_label(loc)}</b> \u2014 history {start} \u2192 {end}",
             "source: Open-Meteo Archive (ERA5)\n",
             f"days: {len(times)}",
             f"\U0001f321 max avg {num(avg(tmax))}{tlab} (peak {num(g(max, tmax))}{tlab})",
             f"\U0001f321 min avg {num(avg(tmin))}{tlab} (low {num(g(min, tmin))}{tlab})",
             f"\U0001f4a7 total precip {tot_p:g}{rlab}",
             f"\U0001f4a8 max gust {num(g(max, gmax), 0)}{wlab}"]
    if len(times) <= 14:
        lines.append("")
        for i in range(len(times)):
            dt = datetime.fromisoformat(times[i])
            hi = str(round(tmax[i])) if i < len(tmax) and tmax[i] is not None else "\u2014"
            lo = str(round(tmin[i])) if i < len(tmin) and tmin[i] is not None else "\u2014"
            ps = psum[i] if i < len(psum) else None
            ps_s = f"  \U0001f4a7{ps:g}{rlab}" if ps else ""
            lines.append(f"{dt:%d.%m} {lo}/{hi}{tlab}{ps_s}")
    return "\n".join(lines)

def cmd_start(args, chat_id):
    return (
        "<b>Personal weather bot</b> \U0001f324\ufe0f\n"
        "Data source: Open-Meteo (free).\n\n"
        "<b>Forecast</b>\n"
        "<code>wx Orsova</code> \u2014 24-hour hourly forecast\n"
        "<code>wx 44.816,29.879</code> \u2014 by coordinates\n"
        "<code>wx Orsova 3</code> \u2014 3-day forecast (up to 16)\n"
        "<code>wx 7</code> \u2014 7-day forecast for all saved locations\n\n"
        "<b>Soil &amp; history</b>\n"
        "<code>soil Orsova</code> \u2014 soil moisture + temperature now\n"
        "<code>hist Orsova 2025-07-01 2025-07-10</code> \u2014 past weather for a period\n\n"
        "<b>Model</b>\n"
        "<code>model</code> \u2014 show the current model and the list of models\n"
        "<code>model iconeu</code> \u2014 set the default model (name from the list)\n\n"
        "<b>Saved locations</b>\n"
        "<code>save 1 Orsova</code> \u2014 save a location in slot 1\n"
        "<code>locs</code> \u2014 list your saved locations\n"
        "<code>del 1</code> \u2014 delete the location in slot 1\n\n"
        "<b>Alerts</b>\n"
        "<code>alerts</code> \u2014 check saved locations now and report\n"
        "<code>set</code> \u2014 show current alert thresholds\n"
        "<code>set gust 70</code> \u2014 change a threshold "
        "(gust km/h, rain mm/h, snow cm/h, heat \u00b0C, frost \u00b0C)\n\n"
        "<b>Units</b>\n"
        "<code>units</code> \u2014 show current display units\n"
        "<code>units temp F</code> \u2014 set units (temp C/F, wind kmh/ms/mph/kn, "
        "rain mm/inch, pressure hpa/mmhg/inhg)\n\n"
        "Saved locations are watched automatically: you get a message when strong "
        "wind, rain, snow, heat or frost is expected in the next hours.\n\n"
        "Type <code>help</code> to see this list again."
    )

# --- Command router (easy to extend) ---
COMMANDS = {
    "wx": cmd_wx, "model": cmd_model,
    "save": cmd_save, "locs": cmd_locs, "del": cmd_del, "alerts": cmd_alerts,
    "set": cmd_set, "units": cmd_units, "soil": cmd_soil, "hist": cmd_hist,
    "start": cmd_start, "help": cmd_start,
}

def handle_text(text, chat_id):
    text = text.strip()
    if not text:
        return None
    parts = text.lstrip("/").split()
    cmd = parts[0].lower()
    args = parts[1:]
    fn = COMMANDS.get(cmd)
    if fn:
        try:
            return fn(args, chat_id)
        except requests.RequestException as e:
            return f"Data fetch error: {e}"
        except Exception as e:
            return f"Error: {e}"
    return None

# --- Telegram ---
def send(chat_id, text):
    try:
        requests.post(f"{TG_API}/sendMessage", json={
            "chat_id": chat_id, "text": text,
            "parse_mode": "HTML", "disable_web_page_preview": True,
        }, timeout=15)
    except requests.RequestException as e:
        print("Send error:", e)

def is_allowed(user_id, chat_id):
    if not ALLOWED_USERS:
        return True
    return str(user_id) in ALLOWED_USERS or str(chat_id) in ALLOWED_USERS

# --- Background alert checker ---
def alert_loop():
    while True:
        try:
            state = load_state()
            for chat_id, cdata in state.items():
                locations = cdata.get("locations", {})
                if not locations:
                    continue
                model_id, model_label = MODELS.get(
                    cdata.get("model", DEFAULT_MODEL), MODELS[DEFAULT_MODEL])
                thr = get_thresholds(chat_id)
                for slot, loc in locations.items():
                    try:
                        data = fetch_alert_forecast(loc["lat"], loc["lon"], model_id)
                    except requests.RequestException:
                        continue
                    trig = evaluate_alerts(data, thr)
                    if not trig:
                        continue
                    today = local_today(data)
                    new_msgs = []
                    for phenom, (val, tstr) in trig.items():
                        if phenom not in ALERTS_ENABLED:
                            continue
                        key = f"{slot}:{phenom}:{today}"
                        if already_sent(chat_id, key):
                            continue
                        new_msgs.append(format_alert_line(phenom, val, tstr))
                        mark_sent(chat_id, key, today)
                    if new_msgs:
                        header = f"\u26a0\ufe0f <b>ALERT \u2014 {loc_label(loc)}</b>"
                        footer = f"source: Open-Meteo \u00b7 model: {model_label}"
                        send(chat_id, header + "\n" + "\n".join(new_msgs) + "\n\n" + footer)
        except Exception as e:
            print("Alert loop error:", e)
        time.sleep(CHECK_INTERVAL_SEC)

def main():
    if not BOT_TOKEN:
        raise SystemExit("Set the TG_BOT_TOKEN environment variable (token from @BotFather).")
    threading.Thread(target=alert_loop, daemon=True).start()
    print(f"Bot started. Alert check every {CHECK_INTERVAL_SEC}s. Waiting for messages...")
    offset = None
    while True:
        try:
            r = requests.get(f"{TG_API}/getUpdates", params={
                "timeout": 30, "offset": offset
            }, timeout=40)
            for u in r.json().get("result", []):
                offset = u["update_id"] + 1
                msg = u.get("message") or u.get("edited_message")
                if not msg or "text" not in msg:
                    continue
                chat_id = msg["chat"]["id"]
                user_id = msg.get("from", {}).get("id")
                text = msg["text"]
                print(f"[msg] chat_id={chat_id} user_id={user_id}: {text!r}")
                if not is_allowed(user_id, chat_id):
                    continue
                reply = handle_text(text, chat_id)
                if reply:
                    send(chat_id, reply)
        except requests.RequestException as e:
            print("Network error:", e)
            time.sleep(5)
        except KeyboardInterrupt:
            print("\nStopped.")
            break

if __name__ == "__main__":
    main()
