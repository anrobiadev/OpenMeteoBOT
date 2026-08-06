#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Personal Telegram weather bot.
Data source: Open-Meteo (free, no API key, non-commercial use).

Commands:
  <place> [days]  -> forecast; just type a place: city name, "lat,lon", a saved
                     name (even partial), or a saved slot number. No days = 24h
                     hourly; a number 1..16 = daily. A bare number = that many
                     days for all saved locations. ("wx" prefix still works too.)
  soil <loc>      -> current soil moisture (depth bands) + soil/air data
  hist <loc> <start> <end> -> past daily weather for a period (YYYY-MM-DD)
  model           -> show the current model + list available models
  model <name>    -> set the default model (persisted, per chat)
  save <n> <loc>  -> save a city, "lat,lon", or "lat,lon Alias" in slot n
  locs            -> list saved locations
  del <n>         -> delete saved location in slot n
  alerts          -> check saved locations now and report (manual test)
  set <param> <v> -> adjust an alert threshold (gust/rain/snow/heat/frost)
  anm <feeds>     -> ANM official warnings per point (nowcasting/general/off)
  units <q> <u>   -> set display units (temp/wind/rain/pressure)
  lang <ro|en>    -> set language (auto-detected from the phone on Telegram)

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
import math
import hashlib
import html
import subprocess
import unicodedata
import threading
import requests
from io import BytesIO
try:
    from PIL import Image, ImageDraw, ImageFont
    _PIL = True
except Exception:
    _PIL = False
from datetime import datetime, timezone, timedelta

# --- Config ---
# systemd units restarted by `restart` (uses your SYSTEM password via sudo -S,
# so nothing is stored and no sudoers editing is needed).
RESTART_UNITS = os.environ.get("TG_RESTART_UNITS",
                               "meteobot.service wa-server.service wa-bridge.service")

BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
STATE_FILE = os.environ.get("TG_BOT_STATE", "bot_state.json")
# Shared across Telegram & WhatsApp (they have separate state files but the same
# working dir), so global settings like the alert interval apply to both.
CONFIG_FILE = os.environ.get("TG_CONFIG_FILE", "meteobot_config.json")
ALLOWED_USERS = {
    s.strip() for s in os.environ.get("TG_ALLOWED_USERS", "").split(",") if s.strip()
}
TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# WhatsApp health monitoring: the Node bridge writes WA_STATUS_FILE; the Telegram
# bot watches it and warns ADMIN_CHAT if WhatsApp goes down / is logged out.
WA_STATUS_FILE = os.environ.get("WA_STATUS_FILE", "wa_status.json")
WA_HEARTBEAT_STALE = int(os.environ.get("WA_HEARTBEAT_STALE", "300"))   # seconds
ADMIN_CHAT = os.environ.get("TG_ADMIN_CHAT", "") or (
    sorted(ALLOWED_USERS)[0] if ALLOWED_USERS else "")

GEO_URL = "https://geocoding-api.open-meteo.com/v1/search"
FC_URL = "https://api.open-meteo.com/v1/forecast"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"  # ERA5 history
AQI_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"  # air quality
FLOOD_URL = "https://flood-api.open-meteo.com/v1/flood"            # GloFAS river discharge
MARINE_URL = "https://marine-api.open-meteo.com/v1/marine"         # waves / sea state

RIVER_XML_URL = os.environ.get("TG_AFDJ_XML", "https://afdj.ro/ro/tabel_cotele_dunarii/xml")
PEGEL_URL = "https://pegelonline.wsv.de/webservices/rest-api/v2/stations.json"
RIVER_MAX_KM = float(os.environ.get("TG_RIVER_MAX_KM", "80"))      # nearest-gauge radius
INHGA_RIVERS_URL = os.environ.get("TG_INHGA_URL",
    "https://www.hidro.ro/bulletin_type/prognoza-hidrologica-pentru-rauri/")
RO_BBOX = (43.4, 20.2, 48.3, 30.1)   # latS, lonW, latN, lonE (Romania + Moldova, rough)
INHGA_MAP_URL = os.environ.get("TG_INHGA_MAP_URL", "https://www.hidro.ro/prognoze/")
_INHGA_MAP_CACHE = {"t": 0.0, "data": None}   # the /prognoze/ page is ~750 KB -> cache it
# AFDJ Danube gauge coordinates (from afdj.ro/ro/cotele-dunarii; XML has no coords).
DAILY_MAX = 16  # Open-Meteo daily forecast horizon
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# --- Alert configuration (edit thresholds here) ---
CHECK_INTERVAL_SEC = int(os.environ.get("TG_ALERT_INTERVAL", "900"))  # 15 min (seconds)
ALERT_WINDOW_H = 12          # how many hours ahead to scan
GUST_KMH = 60                # strong wind gusts
RAIN_MM_H = 4.0              # heavy rain per hour
SNOW_CM_H = 1.0              # snowfall per hour
HEAT_C = 35                  # heat
FROST_C = 0                  # frost (temperature <= this)
ALERTS_ENABLED = {"gust", "rain", "snow", "heat", "frost"}  # trim if you want fewer

# --- ANM official warnings (meteoromania.ro), point-in-polygon per saved location ---
ANM_ENABLED = os.environ.get("TG_ANM", "1") != "0"
ANM_FEEDS = {
    "nowcasting": "https://www.meteoromania.ro/wp-json/meteoapi/v2/avertizari-nowcasting",
    "general": "https://www.meteoromania.ro/wp-json/meteoapi/v2/avertizari-generale",
}
# feeds to check per chat (default). User can change it in-app with the "anm" command.
DEFAULT_ANM_FEEDS = [s.strip() for s in
                     os.environ.get("TG_ANM_FEEDS", "nowcasting,general").split(",") if s.strip()]
ANM_FEED_ALIASES = {"now": "nowcasting", "nowcasting": "nowcasting",
                    "gen": "general", "general": "general"}
ANM_COLORS = {
    0: ("\U0001f7e1", "galben", "yellow"),
    1: ("\U0001f7e0", "portocaliu", "orange"),
    2: ("\U0001f534", "rosu", "red"),
}

# Defaults above are the baseline; per-chat "set" overrides are layered on top.
DEFAULT_THRESHOLDS = {
    "gust": float(GUST_KMH), "rain": float(RAIN_MM_H), "snow": float(SNOW_CM_H),
    "heat": float(HEAT_C), "frost": float(FROST_C),
}
THRESH_UNIT = {"gust": "km/h", "rain": "mm/h", "snow": "cm/h", "heat": "\u00b0C", "frost": "\u00b0C"}

# --- Open-Meteo models: short name -> (API id, description) ---
# Weather models available through the Open-Meteo Forecast API (`models=` parameter).
# Grouped global -> European/regional -> other continents. Any model id not listed
# here can still be used directly, e.g. `model chmi_aladin_cz_1km`.
MODELS = {
    "auto":   ("best_match",           "Auto (Open-Meteo picks the best model per location)"),
    # --- global ---
    "icon":   ("icon_seamless",        "DWD ICON seamless (global + EU + D2)"),
    "icong":  ("icon_global",          "DWD ICON global 11 km"),
    "ecmwf":  ("ecmwf_ifs025",         "ECMWF IFS 0.25 deg"),
    "aifs":   ("ecmwf_aifs025",        "ECMWF AIFS 0.25 deg (AI model)"),
    "gfs":    ("gfs_seamless",         "NOAA GFS seamless"),
    "gfsg":   ("gfs_global",           "NOAA GFS global 0.11 deg"),
    "ukmo":   ("ukmo_seamless",        "UK Met Office seamless"),
    "ukmog":  ("ukmo_global_deterministic_10km", "UK Met Office global 10 km"),
    "gem":    ("gem_seamless",         "Canada GEM seamless"),
    "jma":    ("jma_seamless",         "Japan JMA seamless"),
    "kma":    ("kma_seamless",         "KMA Korea seamless"),
    "cma":    ("cma_grapes_global",    "CMA China GRAPES global"),
    "bom":    ("bom_access_global",    "BOM Australia ACCESS global"),
    "meteofrance": ("meteofrance_seamless", "Meteo-France seamless (ARPEGE + AROME)"),
    # --- Europe / regional (higher resolution) ---
    "iconeu": ("icon_eu",              "DWD ICON-EU 7 km - covers Romania"),
    "icond2": ("icon_d2",              "DWD ICON-D2 2 km - central Europe"),
    "arpege": ("meteofrance_arpege_europe", "Meteo-France ARPEGE Europe 11 km"),
    "arome":  ("meteofrance_arome_france_hd", "Meteo-France AROME France 1.5 km"),
    "aladin": ("chmi_aladin_central_europe_2km", "CHMI ALADIN Central Europe 2 km"),
    "aladincz": ("chmi_aladin_cz_1km", "CHMI ALADIN Czechia 1 km"),
    "aladinseam": ("chmi_aladin_seamless", "CHMI ALADIN seamless"),
    "swiss":  ("meteoswiss_icon_ch1",  "MeteoSwiss ICON-CH1 1 km (Alps)"),
    "swiss2": ("meteoswiss_icon_ch2",  "MeteoSwiss ICON-CH2 2 km (Alps)"),
    "metno":  ("metno_seamless",       "MET Norway seamless (Nordics)"),
    "knmi":   ("knmi_seamless",        "KNMI Netherlands seamless (HARMONIE)"),
    "dmi":    ("dmi_seamless",         "DMI Denmark seamless (HARMONIE)"),
    "arpae":  ("italia_meteo_arpae_icon_2i", "ItaliaMeteo ARPAE ICON-2I 2 km"),
    "austria": ("geosphere_seamless",  "GeoSphere Austria seamless (AROME)"),
    # --- North America / other high-res ---
    "hrrr":   ("gfs_hrrr",             "NOAA HRRR 3 km (USA)"),
    "gemhr":  ("gem_hrdps_continental", "Canada HRDPS 2.5 km"),
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

# --- Per-chat map settings (radar source, base dim, cloud colour/opacity, zoom) ---
# Defaults come from the env-configured module constants (defined lower in the file);
# they resolve at call time, so referencing them here is fine.
def get_map_cfg(chat_id):
    cfg = {
        "radar_src": "anm",             # 'anm' (national image) or 'rainviewer'
        "base_dim": MAP_BASE_DIM,       # 0=OSM full colour, 1=white-out
        "cloud_alpha": CLOUD_MAX_ALPHA, # 0..255 opacity at 100% overcast
        "cloud_rgb": list(CLOUD_RGB),   # [r, g, b] of the cloud shading
        "zoom": MAP_ZOOM,               # RainViewer map zoom (3..7)
        "tz": "",                       # map time zone: '' = server local, offset (+3) or IANA
        "theme": MAP_THEME_DEFAULT,     # 'light' or 'dark' basemap
    }
    cfg.update(load_state().get(str(chat_id), {}).get("map", {}))
    return cfg

def set_map_cfg(chat_id, key, value):
    def m(state):
        state.setdefault(str(chat_id), {}).setdefault("map", {})[key] = value
    update_state(m)

# --- Automatic alerts on/off, per chat (saved locations are kept either way) ---
def get_alerts_enabled(chat_id):
    return load_state().get(str(chat_id), {}).get("alerts_on", True)

def set_alerts_enabled(chat_id, on):
    def m(state):
        state.setdefault(str(chat_id), {})["alerts_on"] = bool(on)
    update_state(m)

def reset_map_cfg(chat_id):
    def m(state):
        state.setdefault(str(chat_id), {}).pop("map", None)
    update_state(m)

# --- Shared config file (Telegram + WhatsApp) for truly-global settings ----------
def load_config():
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def save_config(cfg):
    tmp = CONFIG_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f)
    os.replace(tmp, CONFIG_FILE)   # atomic; last writer wins across processes

# --- Global alert-check interval (seconds), shared by both platforms ---
def get_alert_interval():
    v = load_config().get("alert_interval")
    return int(v) if isinstance(v, int) and v >= 60 else CHECK_INTERVAL_SEC

def set_alert_interval(seconds):
    with _lock:
        cfg = load_config()
        cfg["alert_interval"] = int(seconds)
        save_config(cfg)

def fmt_pressure(hpa, unit):
    if hpa is None:
        return None
    v = hpa * PRESSURE_FACTOR[unit]
    return f"{v:.2f}" if unit == "inhg" else f"{round(v)}"

# --- Language (per-chat; "auto" = adopt the platform hint on first message) ---
DEFAULT_LANG = "en"
SUPPORTED_LANGS = ("en", "ro")

def norm_lang(code):
    if code and str(code).lower().startswith("ro"):
        return "ro"
    return "en"

def get_lang(chat_id):
    return load_state().get(str(chat_id), {}).get("lang", DEFAULT_LANG)

def set_lang(chat_id, lang):
    def m(state):
        state.setdefault(str(chat_id), {})["lang"] = lang
    update_state(m)

def ensure_lang(chat_id, hint):
    """If the user hasn't chosen a language yet, adopt the platform hint once."""
    if "lang" not in load_state().get(str(chat_id), {}) and hint:
        set_lang(chat_id, norm_lang(hint))

def get_anm_feeds(chat_id):
    return load_state().get(str(chat_id), {}).get("anm_feeds", list(DEFAULT_ANM_FEEDS))

def set_anm_feeds(chat_id, feeds):
    def m(state):
        state.setdefault(str(chat_id), {})["anm_feeds"] = feeds
    update_state(m)

# Romanian model descriptions (English ones live in MODELS)
MODEL_DESC_RO = {
    "auto":   "Auto (Open-Meteo alege cel mai bun model per locatie)",
    "icon":   "DWD ICON seamless (cade pe ICON-EU la noi)",
    "iconeu": "DWD ICON-EU 7 km - acopera Romania",
    "ecmwf":  "ECMWF IFS 0.25 grade",
    "gfs":    "NOAA GFS",
    "arpege": "Meteo-France ARPEGE/AROME",
    "ukmo":   "UK Met Office",
    "gem":    "Canada GEM",
    "jma":    "Japan JMA",
}

def model_desc(key, lang):
    if lang == "ro" and key in MODEL_DESC_RO:
        return MODEL_DESC_RO[key]
    return MODELS.get(key, (None, key))[1]

def resolve_model(key):
    """(api_model_id, label) for a shortcut from MODELS, or a raw Open-Meteo id."""
    if key in MODELS:
        return MODELS[key]
    if key and re.fullmatch(r"[a-z0-9]+(_[a-z0-9]+)+", str(key)):
        return (key, key)                       # raw id typed by the user
    return MODELS[DEFAULT_MODEL]

WEEKDAYS_LANG = {
    "en": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
    "ro": ["Lun", "Mar", "Mie", "Joi", "Vin", "Sam", "Dum"],
}

# --- Translation table: key -> {en, ro} (str.format templates) ---
T = {
    "wx_usage": {
        "en": ("Just type a place:\n<code>Orsova</code> \u2014 24h hourly\n"
               "<code>44.816,29.879</code> \u2014 by coordinates\n"
               "<code>Orsova 3</code> \u2014 3-day forecast\n"
               "<code>3</code> \u2014 3-day for all saved locations"),
        "ro": ("Scrie direct o localitate:\n<code>Orsova</code> \u2014 orar pe 24h\n"
               "<code>44.816,29.879</code> \u2014 dupa coordonate\n"
               "<code>Orsova 3</code> \u2014 prognoza pe 3 zile\n"
               "<code>3</code> \u2014 3 zile pentru toate locatiile salvate"),
    },
    "wx_usage_short": {
        "en": "Type a place, e.g. <code>Orsova</code> or <code>Orsova 3</code>",
        "ro": "Scrie o localitate, ex. <code>Orsova</code> sau <code>Orsova 3</code>",
    },
    "no_saved_wx": {
        "en": "No saved locations. Add one (<code>save 1 Orsova</code>) or type <code>Orsova 3</code>",
        "ro": "Nicio locatie salvata. Adauga una (<code>save 1 Orsova</code>) sau scrie <code>Orsova 3</code>",
    },
    "loc_not_found": {
        "en": "Location not found: {loc}", "ro": "Locatie negasita: {loc}",
    },
    "ambiguous": {
        "en": "Multiple saved locations match: {names}. Be more specific.",
        "ro": "Mai multe locatii salvate se potrivesc: {names}. Fii mai specific.",
    },
    "city_not_found": {
        "en": "City \u201c{city}\u201d not found. Check the spelling.",
        "ro": "Orasul \u201e{city}\u201d nu a fost gasit. Verifica denumirea.",
    },
    "loc_not_found_plain": {"en": "Location not found.", "ro": "Locatie negasita."},
    "src_model": {
        "en": "source: Open-Meteo \u00b7 model: <i>{model}</i>",
        "ro": "sursa: Open-Meteo \u00b7 model: <i>{model}</i>",
    },
    "hdr_24h": {"en": "24h forecast", "ro": "prognoza 24h"},
    "hdr_days": {"en": "{n}-day forecast", "ro": "prognoza {n} zile"},
    "feels": {"en": "feels", "ro": "resimtit"},
    "gust": {"en": "gust", "ro": "rafala"},
    "no_daily": {
        "en": "The selected model returns no daily data for this point.",
        "ro": "Modelul selectat nu are date zilnice pentru acest punct.",
    },
    "no_hourly": {
        "en": "The selected model returns no data for this point.",
        "ro": "Modelul selectat nu are date pentru acest punct.",
    },
    # model
    "model_current": {"en": "Current model: <b>{m}</b>", "ro": "Model curent: <b>{m}</b>"},
    "model_available": {"en": "Available models:", "ro": "Modele disponibile:"},
    "model_change": {"en": "Change with: <code>model icon</code>",
                     "ro": "Schimba cu: <code>model icon</code>"},
    "model_unknown": {"en": "Unknown model: <code>{k}</code>\nOptions: {opts}",
                      "ro": "Model necunoscut: <code>{k}</code>\nOptiuni: {opts}"},
    "model_set": {"en": "Default model set: <b>{k}</b> \u2014 {desc}",
                  "ro": "Model implicit setat: <b>{k}</b> \u2014 {desc}"},
    # save / locs / del
    "save_usage": {
        "en": ("Usage: <code>save 1 Orsova</code>\n"
               "by coordinates: <code>save 1 44.816,29.879</code>\n"
               "coordinates + name: <code>save 1 46.158,21.663 Cladova</code>"),
        "ro": ("Utilizare: <code>save 1 Orsova</code>\n"
               "dupa coordonate: <code>save 1 44.816,29.879</code>\n"
               "coordonate + nume: <code>save 1 46.158,21.663 Cladova</code>"),
    },
    "saved_slot": {"en": "Saved slot <b>{slot}</b>: {label}",
                   "ro": "Salvat in slotul <b>{slot}</b>: {label}"},
    "no_saved_add": {
        "en": "No saved locations. Add one with: <code>save 1 Orsova</code>",
        "ro": "Nicio locatie salvata. Adauga una cu: <code>save 1 Orsova</code>",
    },
    "saved_list": {"en": "Saved locations:", "ro": "Locatii salvate:"},
    "del_usage": {"en": "Usage: <code>del 1</code>", "ro": "Utilizare: <code>del 1</code>"},
    "del_ok": {"en": "Deleted slot {slot}.", "ro": "Slotul {slot} sters."},
    "del_missing": {"en": "Slot {slot} not found.", "ro": "Slotul {slot} nu exista."},
    # alerts (manual check)
    "alerts_nothing": {"en": "nothing in next {h}h", "ro": "nimic in urmatoarele {h}h"},
    "alerts_off_set": {
        "en": "🔕 Automatic alerts <b>paused</b>. Saved locations are kept — you can still "
              "check anytime with <code>alerts</code>. Resume with <code>alerts on</code>.",
        "ro": "🔕 Alertele automate au fost <b>oprite</b>. Locatiile salvate raman — poti verifica "
              "oricand cu <code>alerts</code>. Reporneste cu <code>alerts on</code>."},
    "alerts_on_set": {"en": "🔔 Automatic alerts <b>resumed</b>.", "ro": "🔔 Alertele automate au fost <b>repornite</b>."},
    "alerts_state_on": {"en": "🔔 Automatic alerts: <b>on</b>", "ro": "🔔 Alerte automate: <b>pornite</b>"},
    "alerts_state_off": {"en": "🔕 Automatic alerts: <b>paused</b> (<code>alerts on</code> to resume)",
                         "ro": "🔕 Alerte automate: <b>oprite</b> (<code>alerts on</code> pentru repornire)"},
    "alerts_paused_note": {"en": "🔕 <i>Automatic alerts are paused — this was a manual check.</i>",
                           "ro": "🔕 <i>Alertele automate sunt oprite — aceasta a fost o verificare manuala.</i>"},
    "fetch_error": {"en": "fetch error", "ro": "eroare la preluare"},
    # alert message (proactive)
    "alert_header": {"en": "ALERT \u2014 {loc}", "ro": "ALERTA \u2014 {loc}"},
    "al_gust": {"en": "\U0001f4a8 Strong gusts: up to {v} km/h around {t}",
                "ro": "\U0001f4a8 Rafale puternice: pana la {v} km/h in jur de {t}"},
    "al_rain": {"en": "\U0001f327\ufe0f Heavy rain: {v} mm/h around {t}",
                "ro": "\U0001f327\ufe0f Ploaie abundenta: {v} mm/h in jur de {t}"},
    "al_snow": {"en": "\u2744\ufe0f Snowfall: {v} cm/h around {t}",
                "ro": "\u2744\ufe0f Ninsoare: {v} cm/h in jur de {t}"},
    "al_heat": {"en": "\U0001f525 Heat: {v}\u00b0C around {t}",
                "ro": "\U0001f525 Canicula: {v}\u00b0C in jur de {t}"},
    "al_frost": {"en": "\U0001f9ca Frost: {v}\u00b0C around {t}",
                 "ro": "\U0001f9ca Inghet: {v}\u00b0C in jur de {t}"},
    # set thresholds
    "thr_current": {"en": "Current alert thresholds:", "ro": "Praguri de alerta curente:"},
    "thr_change": {"en": "Change with: <code>set gust 70</code>",
                   "ro": "Schimba cu: <code>set gust 70</code>"},
    "thr_unknown": {"en": "Unknown parameter: <code>{p}</code>\nOptions: {opts}",
                    "ro": "Parametru necunoscut: <code>{p}</code>\nOptiuni: {opts}"},
    "thr_usage": {"en": "Usage: <code>set {p} 70</code>", "ro": "Utilizare: <code>set {p} 70</code>"},
    "thr_nan": {"en": "Value must be a number, e.g. <code>set {p} 70</code>",
                "ro": "Valoarea trebuie sa fie un numar, ex. <code>set {p} 70</code>"},
    "thr_set": {"en": "Threshold set: <b>{p}</b> = {v} {unit}",
                "ro": "Prag setat: <b>{p}</b> = {v} {unit}"},
    # units
    "units_current": {"en": "Current display units:", "ro": "Unitati de afisare curente:"},
    "units_examples": {
        "en": ("Examples:\n<code>units temp F</code>\n<code>units wind ms</code>\n"
               "<code>units rain inch</code>\n<code>units pressure mmhg</code>"),
        "ro": ("Exemple:\n<code>units temp F</code>\n<code>units wind ms</code>\n"
               "<code>units rain inch</code>\n<code>units pressure mmhg</code>"),
    },
    "units_unknown_q": {"en": "Unknown quantity. Options: temp, wind, rain, pressure",
                        "ro": "Marime necunoscuta. Optiuni: temp, wind, rain, pressure"},
    "units_usage": {"en": "Usage: <code>units {dim} VALUE</code>\nValues: {opts}",
                    "ro": "Utilizare: <code>units {dim} VALOARE</code>\nValori: {opts}"},
    "units_unknown_v": {"en": "Unknown value for {dim}: <code>{val}</code>\nValues: {opts}",
                        "ro": "Valoare necunoscuta pentru {dim}: <code>{val}</code>\nValori: {opts}"},
    "units_set": {"en": "Unit set: <b>{dim}</b> = {label}",
                  "ro": "Unitate setata: <b>{dim}</b> = {label}"},
    # soil
    "soil_usage": {"en": "Usage: <code>soil Orsova</code> | <code>soil 44.8,29.9</code> | <code>soil 1</code>",
                   "ro": "Utilizare: <code>soil Orsova</code> | <code>soil 44.8,29.9</code> | <code>soil 1</code>"},
    "soil_nodata": {"en": "The selected model has no soil data for this point (try model auto).",
                    "ro": "Modelul selectat nu are date de sol pentru acest punct (incearca model auto)."},
    "soil_title": {"en": "soil & moisture", "ro": "sol & umiditate"},
    "soil_time_src": {"en": "time: {t} \u00b7 source: Open-Meteo",
                      "ro": "ora: {t} \u00b7 sursa: Open-Meteo"},
    "soil_temp0": {"en": "\U0001f321 Soil temp at surface (0 cm): {v}",
                   "ro": "\U0001f321 Temperatura solului la suprafata (0 cm): {v}"},
    "soil_airhum": {"en": "\U0001f4a7 Air humidity (2 m): {v}", "ro": "\U0001f4a7 Umiditatea aerului (2 m): {v}"},
    "soil_moist_h": {"en": "Soil moisture by depth (% water by volume):",
                     "ro": "Umiditatea solului pe adancimi (% apa din volum):"},
    "soil_legend": {
        "en": "Moisture guide: &lt;10% very dry, 10–25% dry, 25–40% good, &gt;40% wet/saturated. "
              "Depths go from the surface (0–1 cm) down to the root zone (27–81 cm).",
        "ro": "Ghid umiditate: &lt;10% foarte uscat, 10–25% uscat, 25–40% bun, &gt;40% ud/saturat. "
              "Adancimile merg de la suprafata (0–1 cm) pana la zona radacinilor (27–81 cm)."},
    # air quality
    "air_usage": {"en": "Usage: <code>air Orsova</code> (city, coordinates, or a saved slot)",
                  "ro": "Utilizare: <code>air Orsova</code> (oras, coordonate sau un slot salvat)"},
    "air_title": {"en": "air quality", "ro": "calitatea aerului"},
    "air_nodata": {"en": "No air-quality data for this point.", "ro": "Fara date de calitate a aerului pentru acest punct."},
    "air_time_src": {"en": "time: {t} · source: Open-Meteo (CAMS)", "ro": "ora: {t} · sursa: Open-Meteo (CAMS)"},
    "air_pm25": {"en": "PM2.5 — fine particles, reach deep in the lungs: {v}",
                 "ro": "PM2.5 — particule fine, patrund adanc in plamani: {v}"},
    "air_pm10": {"en": "PM10 — coarser dust/particles: {v}",
                 "ro": "PM10 — praf/particule mai mari: {v}"},
    "air_o3": {"en": "O₃ ozone — summer smog, irritates airways: {v}",
               "ro": "O₃ ozon — smog de vara, irita caile respiratorii: {v}"},
    "air_no2": {"en": "NO₂ — from traffic &amp; combustion: {v}",
                "ro": "NO₂ — din trafic si ardere: {v}"},
    "air_so2": {"en": "SO₂ — from burning fuels/industry: {v}",
                "ro": "SO₂ — din arderea combustibililor/industrie: {v}"},
    "air_co": {"en": "CO — carbon monoxide: {v}", "ro": "CO — monoxid de carbon: {v}"},
    "air_uv": {"en": "☀ UV index — sunburn risk (0 low … 11+ extreme): {v}",
               "ro": "☀ Index UV — risc arsuri solare (0 mic … 11+ extrem): {v}"},
    "air_legend": {
        "en": "EAQI = European Air Quality Index (0–20 good … 100+ extremely poor). "
              "Pollutants in µg/m³ — lower is better.",
        "ro": "EAQI = Indicele European al Calitatii Aerului (0–20 bun … 100+ extrem de slab). "
              "Poluantii in µg/m³ — mai mic e mai bine."},
    "aqi_unknown": {"en": "no data", "ro": "fara date"},
    "aqi_good": {"en": "good", "ro": "bun"},
    "aqi_fair": {"en": "fair", "ro": "acceptabil"},
    "aqi_moderate": {"en": "moderate", "ro": "moderat"},
    "aqi_poor": {"en": "poor", "ro": "slab"},
    "aqi_vpoor": {"en": "very poor", "ro": "foarte slab"},
    "aqi_epoor": {"en": "extremely poor", "ro": "extrem de slab"},
    # flood / river discharge
    "flood_usage": {"en": "Usage: <code>flood Orsova [days]</code> (river discharge forecast)",
                    "ro": "Utilizare: <code>flood Orsova [zile]</code> (prognoza debit rau)"},
    "flood_title": {"en": "river discharge", "ro": "debit rau"},
    "flood_src": {"en": "source: Open-Meteo Flood (GloFAS)", "ro": "sursa: Open-Meteo Flood (GloFAS)"},
    "flood_nodata": {"en": "No river-discharge data for this point (not near a modelled river).",
                     "ro": "Fara date de debit pentru acest punct (nu e langa un rau modelat)."},
    "flood_note": {"en": "⚠️ marks days near the period's peak. GloFAS ~5 km, not a local gauge.",
                   "ro": "⚠️ marcheaza zilele aproape de varf. GloFAS ~5 km, nu o statie locala."},
    # river water levels ("cote")
    "river_usage": {"en": "Usage: <code>cote Orsova</code> (or save a location and just type <code>cote</code>)",
                    "ro": "Utilizare: <code>cote Orsova</code> (sau salveaza o locatie si scrie doar <code>cote</code>)"},
    "river_title": {"en": "river level", "ro": "cota apei"},
    "river_none": {"en": "No river gauge near this point (Romanian Danube / German waterways only).",
                   "ro": "Nicio statie de cota langa acest punct (doar Dunarea romaneasca / cai navigabile germane)."},
    "river_src_afdj": {"en": "source: AFDJ Galați (afdj.ro)", "ro": "sursa: AFDJ Galați (afdj.ro)"},
    "river_src_pegel": {"en": "source: WSV PegelOnline (Germany)", "ro": "sursa: WSV PegelOnline (Germania)"},
    "river_level": {"en": "🌊 Level: {v}  (24h {d} cm {tr})", "ro": "🌊 Cota: {v}  (24h {d} cm {tr})"},
    "river_level_p": {"en": "🌊 Level: {v}  ({st})", "ro": "🌊 Cota: {v}  ({st})"},
    "river_temp": {"en": "🌡 Water temp: {v}", "ro": "🌡 Temperatura apei: {v}"},
    "river_forecast48": {"en": "📈 In 48h (INHGA): {v}", "ro": "📈 Peste 48h (INHGA): {v}"},
    "river_km": {"en": "📍 Danube km {km}", "ro": "📍 km Dunare {km}"},
    "river_near": {"en": "≈ {d} km from your point", "ro": "≈ {d} km de punctul tau"},
    "river_note": {"en": "Level relative to the local gauge zero (can be negative in drought). "
                         "Danube gauges from AFDJ; forecasts by INHGA.",
                   "ro": "Cota fata de mira locala (poate fi negativa la seceta). "
                         "Statiile de Dunare de la AFDJ; prognozele de la INHGA."},
    "river_ro_title": {"en": "Romanian rivers — INHGA forecast",
                       "ro": "Râuri România — prognoză INHGA"},
    "river_ro_src": {"en": "source: INHGA (hidro.ro)", "ro": "sursa: INHGA (hidro.ro)"},
    "river_ro_note": {"en": "National river forecast by basin (trend + status vs. the "
                            "warning levels). INHGA publishes no open per-gauge cm feed.",
                      "ro": "Prognoză națională pe bazine (tendință + starea față de "
                            "cotele de atenție). INHGA nu are feed deschis cu cote în cm pe stație."},
    "river_inhga_src": {"en": "source: INHGA hydrometric network (hidro.ro)",
                        "ro": "sursa: rețeaua hidrometrică INHGA (hidro.ro)"},
    "river_h": {"en": "Level H = {v}  (24h {d} cm {tr})",
                "ro": "Cota H = {v}  (24h {d} cm {tr})"},
    "river_updated": {"en": "\U0001f552 {t}", "ro": "\U0001f552 {t}"},
    "river_inhga_note": {"en": "H = gauge reading vs. its local zero (negative in drought); "
                               "status is relative to INHGA defence levels. ~750 river & Danube gauges.",
                         "ro": "H = citirea la miră față de zero mira (negativă la secetă); "
                               "starea e față de cotele de apărare INHGA. ~750 stații pe râuri și Dunăre."},
    "flood_rising": {"en": "rising ↗", "ro": "in crestere ↗"},
    "flood_falling": {"en": "falling ↘", "ro": "in scadere ↘"},
    "flood_steady": {"en": "steady →", "ro": "stabil →"},
    "flood_summary": {"en": "📈 Trend: <b>{trend}</b> · range <b>{lo}–{hi}</b> m³/s",
                      "ro": "📈 Tendinta: <b>{trend}</b> · interval <b>{lo}–{hi}</b> m³/s"},
    "flood_legend": {
        "en": "River discharge = water volume passing per second (m³/s). Flood risk is "
              "river-specific — watch a sharp rise vs the usual level, not the raw number.",
        "ro": "Debitul = volumul de apa ce trece pe secunda (m³/s). Riscul de inundatie e "
              "specific fiecarui rau — urmareste o crestere brusca fata de nivelul obisnuit, nu cifra in sine."},
    # wind profile
    "wind_usage": {"en": "Usage: <code>wind Orsova</code> (city, coordinates, or a saved slot)",
                   "ro": "Utilizare: <code>wind Orsova</code> (oras, coordonate sau un slot salvat)"},
    "wind_title": {"en": "wind by height", "ro": "vantul pe inaltimi"},
    "wind_nodata": {"en": "No wind data for this point.", "ro": "Fara date de vant pentru acest punct."},
    "wind_now_hdr": {"en": "<b>Now ({t})</b> — speed &amp; direction it blows FROM:",
                     "ro": "<b>Acum ({t})</b> — viteza si directia DIN care bate:"},
    "wind_gust_now": {"en": "  \U0001f4a8 gusts (10 m): {v}", "ro": "  \U0001f4a8 rafale (10 m): {v}"},
    "wind_upper_hdr": {"en": "<b>Higher up</b> (height above ground, from pressure levels):",
                       "ro": "<b>La inaltime</b> (metri deasupra solului, din nivele de presiune):"},
    "wind_band_hdr": {"en": "<b>{a}–{b} m above ground</b> ({t}) — every 100 m:",
                      "ro": "<b>{a}–{b} m deasupra solului</b> ({t}) — din 100 in 100 m:"},
    "wind_band_none": {"en": "no data covering this height band right now",
                       "ro": "fara date pentru acest interval de inaltime acum"},
    "wind_band_note": {
        "en": "Values are interpolated between the model levels (10–180 m and pressure levels), "
              "on wind vectors. Gusts are measured at 10 m and can be stronger aloft — "
              "always check local UAS/drone rules and the actual site conditions.",
        "ro": "Valorile sunt interpolate intre nivelele modelului (10–180 m si nivele de presiune), "
              "pe vectori de vant. Rafalele sunt masurate la 10 m si pot fi mai puternice in inaltime — "
              "verifica intotdeauna reglementarile UAS/drone si conditiile reale din teren."},
    "wind_next_hdr": {"en": "<b>Next hours (10 m)</b>:", "ro": "<b>Urmatoarele ore (10 m)</b>:"},
    "wind_legend": {
        "en": "Direction = where the wind comes FROM (NW = blows from north-west). "
              "10 m ≈ ground level, 80–180 m ≈ turbine/tower height. Higher levels come from "
              "pressure surfaces, so their height shifts a little with the weather.",
        "ro": "Directia = de UNDE bate vantul (NV = bate dinspre nord-vest). "
              "10 m ≈ la sol, 80–180 m ≈ inaltime turbina/turn. Nivelele de sus vin din suprafete "
              "de presiune, deci inaltimea lor variaza putin cu vremea."},
    # marine / sea state
    "marine_usage": {"en": "Usage: <code>marine Constanta</code> (a coastal/sea point)",
                     "ro": "Utilizare: <code>marine Constanta</code> (un punct pe mare/coasta)"},
    "marine_title": {"en": "sea state", "ro": "starea marii"},
    "marine_nodata": {"en": "No marine data — this point isn't at sea (try a coastal location).",
                      "ro": "Fara date marine — punctul nu e pe mare (incearca o locatie pe coasta)."},
    "marine_time_src": {"en": "time: {t} · source: Open-Meteo Marine", "ro": "ora: {t} · sursa: Open-Meteo Marine"},
    "marine_wave": {"en": "🌊 Waves — total height: {v}", "ro": "🌊 Valuri — inaltime totala: {v}"},
    "marine_period": {"en": "⏱ Wave period (time between crests): {v}",
                      "ro": "⏱ Perioada valurilor (timp intre creste): {v}"},
    "marine_swell": {"en": "🌀 Swell (long waves from afar): {v}, period {p}",
                     "ro": "🌀 Hula (valuri lungi din larg): {v}, perioada {p}"},
    "marine_windwave": {"en": "💨 Wind waves (local, from wind): {v}",
                        "ro": "💨 Valuri de vant (locale, din vant): {v}"},
    "marine_sst": {"en": "🌡 Sea surface temperature: {v}", "ro": "🌡 Temperatura apei la suprafata: {v}"},
    "marine_legend": {
        "en": "Wave height in metres; period in seconds (bigger = longer, more powerful swell).",
        "ro": "Inaltimea valurilor in metri; perioada in secunde (mai mare = hula mai lunga si puternica)."},
    # hist
    "hist_usage": {
        "en": ("Usage: <code>hist Orsova 2025-07-01 2025-07-10</code>\n"
               "(location can be a city, coordinates, or a saved slot number)"),
        "ro": ("Utilizare: <code>hist Orsova 2025-07-01 2025-07-10</code>\n"
               "(locatia poate fi oras, coordonate sau numarul unui slot salvat)"),
    },
    "hist_baddate": {"en": "Dates must be YYYY-MM-DD. Ex: <code>hist Orsova 2025-07-01 2025-07-10</code>",
                     "ro": "Datele trebuie in format AAAA-LL-ZZ. Ex: <code>hist Orsova 2025-07-01 2025-07-10</code>"},
    "hist_needloc": {"en": "Specify a location. Ex: <code>hist Orsova 2025-07-01 2025-07-10</code>",
                     "ro": "Specifica o locatie. Ex: <code>hist Orsova 2025-07-01 2025-07-10</code>"},
    "hist_nodata": {"en": "No historical data for that period (ERA5 archive lags ~5 days).",
                    "ro": "Fara date istorice pentru perioada (arhiva ERA5 are ~5 zile intarziere)."},
    "hist_title": {"en": "history {a} \u2192 {b}", "ro": "istoric {a} \u2192 {b}"},
    "hist_src": {"en": "source: Open-Meteo Archive (ERA5)", "ro": "sursa: Arhiva Open-Meteo (ERA5)"},
    "hist_days": {"en": "days: {n}", "ro": "zile: {n}"},
    "hist_maxavg": {"en": "\U0001f321 max avg {a}{u} (peak {b}{u})",
                    "ro": "\U0001f321 media max {a}{u} (varf {b}{u})"},
    "hist_minavg": {"en": "\U0001f321 min avg {a}{u} (low {b}{u})",
                    "ro": "\U0001f321 media min {a}{u} (minim {b}{u})"},
    "hist_precip": {"en": "\U0001f4a7 total precip {v}{u}", "ro": "\U0001f4a7 precip. total {v}{u}"},
    "hist_gust": {"en": "\U0001f4a8 max gust {v}{u}", "ro": "\U0001f4a8 rafala max {v}{u}"},
    # lang
    "lang_current": {"en": "Current language: <b>{l}</b>\nChange with: <code>lang ro</code> / <code>lang en</code>",
                     "ro": "Limba curenta: <b>{l}</b>\nSchimba cu: <code>lang ro</code> / <code>lang en</code>"},
    "lang_set": {"en": "Language set: <b>{l}</b>", "ro": "Limba setata: <b>{l}</b>"},
    "lang_unknown": {"en": "Supported: en, ro", "ro": "Suportate: en, ro"},
    "err_generic": {"en": "Error: {e}", "ro": "Eroare: {e}"},
    "fetch_generic": {"en": "Data fetch error: {e}", "ro": "Eroare la preluarea datelor: {e}"},
    "anm_hdr": {"en": "ANM WARNING", "ro": "AVERTIZARE ANM"},
    "anm_code": {"en": "Code: {color}", "ro": "Cod: {color}"},
    "anm_valid": {"en": "valid: {v}", "ro": "valabil: {v}"},
    "anm_src": {"en": "source: meteoromania.ro (ANM)", "ro": "sursa: meteoromania.ro (ANM)"},
    "cap_radar": {"en": "Radar", "ro": "Radar"},
    "cap_sat": {"en": "Cloud cover", "ro": "Acoperire nori"},
    "cap_map": {"en": "Clouds + Radar", "ro": "Nori + Radar"},
    "map_src": {"en": "source: RainViewer, \u00a9 OpenStreetMap", "ro": "sursa: RainViewer, \u00a9 OpenStreetMap"},
    "map_src_radar": {"en": "source: RainViewer, \u00a9 OpenStreetMap", "ro": "sursa: RainViewer, \u00a9 OpenStreetMap"},
    "map_src_anm": {"en": "source: meteoromania.ro (ANM), \u00a9 OpenStreetMap", "ro": "sursa: meteoromania.ro (ANM), \u00a9 OpenStreetMap"},
    "model_set_raw": {
        "en": "Model set to <b>{k}</b> (raw Open-Meteo id). If the API rejects it, the id is wrong.",
        "ro": "Model setat: <b>{k}</b> (id Open-Meteo direct). Daca API-ul il refuza, id-ul e gresit."},
    "cap_heat": {"en": "Temperature map", "ro": "Harta temperaturilor"},
    "cap_sat_low": {"en": "Low cloud cover", "ro": "Nori jos"},
    "cap_sat_mid": {"en": "Mid-level cloud cover", "ro": "Nori la nivel mediu"},
    "cap_sat_high": {"en": "High cloud cover", "ro": "Nori sus"},
    "cap_hum": {"en": "Relative humidity (2 m)", "ro": "Umiditate relativa (2 m)"},
    "map_forecast_in": {"en": "forecast +{h}h", "ro": "prognoza +{h}h"},
    "hum_bar_title": {"en": "Relative humidity 2 m (%) — Open-Meteo scale",
                      "ro": "Umiditate relativa 2 m (%) — scara Open-Meteo"},
    "hum_legend": {
        "en": ("<b>Colour scale</b> (bottom of the image) — Open-Meteo's official humidity "
               "scale: pink/orange = dry air · yellow-green = moderate · green/teal = very humid.\n"
               "<i>High humidity near the ground means mist, fog or low cloud is more likely.</i>"),
        "ro": ("<b>Scara de culori</b> (jos in imagine) — scara oficiala Open-Meteo pentru umiditate: "
               "roz/portocaliu = aer uscat · galben-verde = moderat · verde/turcoaz = foarte umed.\n"
               "<i>Umiditatea mare langa sol inseamna sanse crescute de ceata sau nori jos.</i>"),
    },
    "heat_bar_title": {"en": "Air temperature 2 m ({u}) — Open-Meteo scale",
                       "ro": "Temperatura aerului 2 m ({u}) — scara Open-Meteo"},
    "heat_legend": {
        "en": ("<b>Colour scale</b> (bottom of the image) — Open-Meteo's official temperature "
               "scale, the same one used on maps.open-meteo.com:\n"
               "violet/blue = frost and cold · light blue → green = cool to mild · "
               "yellow/orange = warm · red = hot · dark red = extreme heat.\n"
               "<i>Fixed steps, so the same shade always means the same temperature; "
               "the bar covers the range present on this map.</i>"),
        "ro": ("<b>Scara de culori</b> (jos in imagine) — scara oficiala de temperatura "
               "Open-Meteo, aceeasi folosita pe maps.open-meteo.com:\n"
               "violet/albastru = inghet si frig · albastru deschis → verde = racoare spre bland · "
               "galben/portocaliu = cald · rosu = foarte cald · rosu inchis = canicula.\n"
               "<i>Praguri fixe, deci aceeasi nuanta inseamna mereu aceeasi temperatura; "
               "bara acopera intervalul prezent pe aceasta harta.</i>"),
    },
    "rv_bar_title": {"en": "Radar reflectivity dBZ — RainViewer (Universal Blue)",
                     "ro": "Reflectivitate radar dBZ — RainViewer (Universal Blue)"},
    "rv_bar_left": {"en": "light rain", "ro": "ploaie slaba"},
    "rv_bar_right": {"en": "extreme / hail", "ro": "extrema / grindina"},
    "radar_legend_anm": {
        "en": ("<b>Colour scale</b> (bottom of the image) — ANM's own reflectivity scale in "
               "<b>dBZ</b>: blue/green = light–moderate rain, yellow/orange = heavy, "
               "red/magenta = torrential, possible hail. Blank = no echo."),
        "ro": ("<b>Scara de culori</b> (jos in imagine) — scara oficiala ANM de reflectivitate in "
               "<b>dBZ</b>: albastru/verde = ploaie slaba–moderata, galben/portocaliu = puternica, "
               "rosu/magenta = torentiala, posibil grindina. Gol = fara ecou."),
    },
    "radar_legend": {
        "en": ("<b>Colour scale</b> (bottom of the image) \u2014 RainViewer's official "
               "<b>Universal Blue</b> scale, in <b>dBZ</b> (radar reflectivity):\n"
               "blue ~15\u201334 light to moderate rain \u00b7 yellow/orange ~35\u201344 heavy \u00b7 "
               "red ~45\u201354 very heavy \u00b7 magenta/white 55+ extreme, hail likely. Blank = no echo."),
        "ro": ("<b>Scara de culori</b> (jos in imagine) \u2014 scara oficiala RainViewer "
               "<b>Universal Blue</b>, in <b>dBZ</b> (reflectivitate radar):\n"
               "albastru ~15\u201334 ploaie slaba spre moderata \u00b7 galben/portocaliu ~35\u201344 puternica \u00b7 "
               "rosu ~45\u201354 foarte puternica \u00b7 magenta/alb 55+ extrema, probabil grindina. Gol = fara ecou."),
    },
    "map_src_clouds": {"en": "source: Open-Meteo, \u00a9 OpenStreetMap", "ro": "sursa: Open-Meteo, \u00a9 OpenStreetMap"},
    "map_src_both": {"en": "source: Open-Meteo + RainViewer, \u00a9 OpenStreetMap", "ro": "sursa: Open-Meteo + RainViewer, \u00a9 OpenStreetMap"},
    "map_nopil": {"en": "Image maps need Pillow: <code>pip install pillow</code>",
                  "ro": "Hartile necesita Pillow: <code>pip install pillow</code>"},
    "map_nodata": {"en": "No map data available right now.", "ro": "Fara date de harta momentan."},
    "mapset_current": {
        "en": ("<b>Map settings</b>\n"
               "<code>radar</code> source: <b>{src}</b>  (anm | rainviewer)\n"
               "<code>dim</code> base fade: <b>{dim}</b>  (0..1)\n"
               "<code>alpha</code> cloud opacity: <b>{alpha}</b>  (0..255)\n"
               "<code>cloud</code> colour RGB: <b>{rgb}</b>\n"
               "<code>zoom</code> map zoom: <b>{zoom}</b>  (3..7)\n"
               "<code>tz</code> map time zone: <b>{tz}</b>\n"
               "<code>theme</code> basemap: <b>{theme}</b>  (light | dark)\n\n"
               "Change e.g.: <code>mapset radar anm</code> | <code>mapset dim 0.5</code> | "
               "<code>mapset alpha 225</code> | <code>mapset cloud 105,105,105</code> | "
               "<code>mapset zoom 6</code> | <code>thm dark</code> | "
               "<code>mapset tz Europe/Bucharest</code> | <code>mapset reset</code>"),
        "ro": ("<b>Setari harta</b>\n"
               "<code>radar</code> sursa: <b>{src}</b>  (anm | rainviewer)\n"
               "<code>dim</code> estompare fundal: <b>{dim}</b>  (0..1)\n"
               "<code>alpha</code> opacitate nori: <b>{alpha}</b>  (0..255)\n"
               "<code>cloud</code> culoare RGB: <b>{rgb}</b>\n"
               "<code>zoom</code> zoom harta: <b>{zoom}</b>  (3..7)\n"
               "<code>tz</code> fus orar harta: <b>{tz}</b>\n"
               "<code>theme</code> fundal harta: <b>{theme}</b>  (light | dark)\n\n"
               "Schimba ex.: <code>mapset radar anm</code> | <code>mapset dim 0.5</code> | "
               "<code>mapset alpha 225</code> | <code>mapset cloud 105,105,105</code> | "
               "<code>mapset zoom 6</code> | <code>thm dark</code> | "
               "<code>mapset tz Europe/Bucharest</code> | <code>mapset reset</code>"),
    },
    "mapset_set": {"en": "Set <code>{k}</code> = <b>{v}</b>", "ro": "Setat <code>{k}</code> = <b>{v}</b>"},
    "mapset_reset": {"en": "Map settings reset to defaults.", "ro": "Setarile hartii au fost resetate."},
    "mapset_unknown": {"en": "Unknown setting: <code>{k}</code>. Type <code>mapset</code> to see options.",
                       "ro": "Setare necunoscuta: <code>{k}</code>. Scrie <code>mapset</code> pentru optiuni."},
    "mapset_radar_usage": {"en": "Use: <code>mapset radar anm</code> or <code>mapset radar rainviewer</code>",
                           "ro": "Foloseste: <code>mapset radar anm</code> sau <code>mapset radar rainviewer</code>"},
    "mapset_dim_usage": {"en": "Use: <code>mapset dim 0.55</code> (0..1)", "ro": "Foloseste: <code>mapset dim 0.55</code> (0..1)"},
    "mapset_alpha_usage": {"en": "Use: <code>mapset alpha 225</code> (0..255)", "ro": "Foloseste: <code>mapset alpha 225</code> (0..255)"},
    "mapset_rgb_usage": {"en": "Use: <code>mapset cloud 105,105,105</code> (r,g,b 0..255)",
                         "ro": "Foloseste: <code>mapset cloud 105,105,105</code> (r,g,b 0..255)"},
    "mapset_zoom_usage": {"en": "Use: <code>mapset zoom 6</code> (3..7)", "ro": "Foloseste: <code>mapset zoom 6</code> (3..7)"},
    "mapset_tz_usage": {"en": "Use: <code>mapset tz Europe/Bucharest</code> | <code>mapset tz +3</code> | <code>mapset tz auto</code>",
                        "ro": "Foloseste: <code>mapset tz Europe/Bucharest</code> | <code>mapset tz +3</code> | <code>mapset tz auto</code>"},
    "mapset_theme_usage": {"en": "Use: <code>thm dark</code> or <code>thm light</code>",
                           "ro": "Foloseste: <code>thm dark</code> sau <code>thm light</code>"},
    "map_src_dark": {"en": "© OpenStreetMap, © CARTO", "ro": "© OpenStreetMap, © CARTO"},
    "alarm_none": {"en": "No alarms set. Add one: <code>alarm 1 21:05</code> (saved slot 1 at 21:05).",
                   "ro": "Nicio alarma setata. Adauga: <code>alarm 1 21:05</code> (slotul 1 salvat, la 21:05)."},
    "alarm_list_hdr": {"en": "⏰ <b>Daily forecast alarms</b> (server time):", "ro": "⏰ <b>Alarme prognoza zilnica</b> (ora serverului):"},
    "alarm_set": {"en": "⏰ Alarm set: <b>{name}</b> (slot {slot}) daily at <b>{t}</b>.",
                  "ro": "⏰ Alarma setata: <b>{name}</b> (slot {slot}) zilnic la <b>{t}</b>."},
    "alarm_off_all": {"en": "All alarms turned off.", "ro": "Toate alarmele au fost oprite."},
    "alarm_off_slot": {"en": "Alarm for slot {slot} turned off.", "ro": "Alarma pentru slotul {slot} a fost oprita."},
    "alarm_no_slot": {"en": "No saved location in slot {slot}. Save one first: <code>save {slot} Orsova</code>.",
                      "ro": "Nicio locatie salvata in slotul {slot}. Salveaza intai: <code>save {slot} Orsova</code>."},
    "alarm_usage": {"en": "Use: <code>alarm 1 21:05</code> | <code>alarm 1 off</code> | <code>alarm off</code>",
                    "ro": "Foloseste: <code>alarm 1 21:05</code> | <code>alarm 1 off</code> | <code>alarm off</code>"},
    "alarm_bad_time": {"en": "Time must be HH:MM (24h), e.g. <code>21:05</code>.", "ro": "Ora trebuie HH:MM (24h), ex. <code>21:05</code>."},
    "interval_current": {"en": "Alert check interval: <b>{m} min</b> ({s}s). Change with <code>interval 10</code> (minutes).",
                         "ro": "Interval verificare alerte: <b>{m} min</b> ({s}s). Schimba cu <code>interval 10</code> (minute)."},
    "interval_set": {"en": "Alert check interval set to <b>{m} min</b> ({s}s). Applies to Telegram &amp; WhatsApp.",
                     "ro": "Interval verificare alerte setat la <b>{m} min</b> ({s}s). Se aplica pe Telegram &amp; WhatsApp."},
    "interval_usage": {"en": "Use: <code>interval 10</code> — minutes (min 1, max 1440).",
                       "ro": "Foloseste: <code>interval 10</code> — minute (min 1, max 1440)."},
    "sys_title": {"en": "System status", "ro": "Status sistem"},
    "sys_core": {"en": "🤖 Bot core: <b>online</b>", "ro": "🤖 Nucleu bot: <b>online</b>"},
    "sys_wa_ok": {"en": "WhatsApp: <b>connected</b> (heartbeat {s}s ago)", "ro": "WhatsApp: <b>conectat</b> (heartbeat acum {s}s)"},
    "sys_wa_disc": {"en": "WhatsApp: <b>disconnected</b> (reconnecting…)", "ro": "WhatsApp: <b>deconectat</b> (se reconecteaza…)"},
    "sys_wa_logout": {"en": "WhatsApp: <b>logged out</b> — re-scan the QR", "ro": "WhatsApp: <b>delogat</b> — rescaneaza QR-ul"},
    "sys_wa_down": {"en": "WhatsApp: <b>bridge down</b> (no heartbeat for {s}s)", "ro": "WhatsApp: <b>puntea oprita</b> (fara heartbeat de {s}s)"},
    "sys_wa_none": {"en": "WhatsApp: not configured", "ro": "WhatsApp: neconfigurat"},
    "sys_interval": {"en": "⏱ Alert check: every <b>{m} min</b>", "ro": "⏱ Verificare alerte: la <b>{m} min</b>"},
    "sys_anm": {"en": "🇷🇴 ANM warnings: <b>{feeds}</b>", "ro": "🇷🇴 Avertizari ANM: <b>{feeds}</b>"},
    "sys_chat": {"en": "📍 Saved: <b>{n}</b> · ⏰ alarms: <b>{a}</b> · 🕒 map tz: <b>{tz}</b>",
                 "ro": "📍 Salvate: <b>{n}</b> · ⏰ alarme: <b>{a}</b> · 🕒 fus harti: <b>{tz}</b>"},
    "restart_usage": {"en": "Usage: <code>restart &lt;your system password&gt;</code>",
                      "ro": "Utilizare: <code>restart &lt;parola ta de sistem&gt;</code>"},
    "restart_bad_pw": {"en": "❌ Wrong password.", "ro": "❌ Parola gresita."},
    "restart_no_verify": {
        "en": "⚠️ Can't verify the password (PAM not installed). Restart aborted for safety. "
              "Run <code>pip install python-pam</code>, or set <code>TG_RESTART_PASSWORD</code> in meteobot.env.",
        "ro": "⚠️ Nu pot verifica parola (PAM neinstalat). Repornire anulata pentru siguranta. "
              "Ruleaza <code>pip install python-pam</code>, sau seteaza <code>TG_RESTART_PASSWORD</code> in meteobot.env."},
    "restart_ok": {"en": "🔄 Restarting services now… back in a few seconds.",
                   "ro": "🔄 Repornesc serviciile acum… revin in cateva secunde."},
    "restart_err": {"en": "Restart failed: {e}", "ro": "Repornire esuata: {e}"},
    "anm_off_word": {"en": "off", "ro": "oprit"},
    "anm_current": {
        "en": "ANM warnings: <b>{feeds}</b>\nSet with: <code>anm nowcasting,general</code> | <code>anm nowcasting</code> | <code>anm off</code>",
        "ro": "Avertizari ANM: <b>{feeds}</b>\nSeteaza cu: <code>anm nowcasting,general</code> | <code>anm nowcasting</code> | <code>anm off</code>",
    },
    "anm_set": {"en": "ANM warnings set: <b>{feeds}</b>", "ro": "Avertizari ANM setate: <b>{feeds}</b>"},
    "anm_set_off": {"en": "ANM warnings turned off.", "ro": "Avertizari ANM oprite."},
    "anm_unknown": {
        "en": "Unknown feed: <code>{f}</code>. Options: nowcasting, general, both, off",
        "ro": "Feed necunoscut: <code>{f}</code>. Optiuni: nowcasting, general, both, off",
    },
}

def tr(key, lang, **kw):
    d = T.get(key, {})
    s = d.get(lang, d.get("en", key))
    return s.format(**kw) if kw else s

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

def wmo_desc(code, lang="en"):
    if lang == "ro":
        return WMO_RO.get(code, ("\u2014", "\u2753"))
    return WMO.get(code, ("\u2014", "\u2753"))

WMO_RO = {
    0: ("Senin", "\u2600\ufe0f"), 1: ("Predominant senin", "\U0001f324\ufe0f"),
    2: ("Partial noros", "\u26c5"), 3: ("Noros", "\u2601\ufe0f"),
    45: ("Ceata", "\U0001f32b\ufe0f"), 48: ("Ceata cu depunere", "\U0001f32b\ufe0f"),
    51: ("Burnita slaba", "\U0001f326\ufe0f"), 53: ("Burnita", "\U0001f326\ufe0f"),
    55: ("Burnita densa", "\U0001f326\ufe0f"), 56: ("Burnita inghetata", "\U0001f326\ufe0f"),
    57: ("Burnita inghetata densa", "\U0001f326\ufe0f"),
    61: ("Ploaie slaba", "\U0001f327\ufe0f"), 63: ("Ploaie", "\U0001f327\ufe0f"),
    65: ("Ploaie puternica", "\U0001f327\ufe0f"), 66: ("Ploaie inghetata", "\U0001f327\ufe0f"),
    67: ("Ploaie inghetata puternica", "\U0001f327\ufe0f"),
    71: ("Ninsoare slaba", "\U0001f328\ufe0f"), 73: ("Ninsoare", "\U0001f328\ufe0f"),
    75: ("Ninsoare puternica", "\u2744\ufe0f"), 77: ("Grauri de zapada", "\U0001f328\ufe0f"),
    80: ("Averse slabe", "\U0001f326\ufe0f"), 81: ("Averse", "\U0001f327\ufe0f"),
    82: ("Averse violente", "\u26c8\ufe0f"),
    85: ("Averse de zapada", "\U0001f328\ufe0f"), 86: ("Averse de zapada puternice", "\u2744\ufe0f"),
    95: ("Furtuna", "\u26c8\ufe0f"), 96: ("Furtuna cu grindina", "\u26c8\ufe0f"),
    99: ("Furtuna cu grindina puternica", "\u26c8\ufe0f"),
}

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

def parse_leading_coords(tokens):
    """Detect coordinates at the start of a token list, allowing
    'lat,lon' | 'lat, lon' | 'lat lon'. Returns (lat, lon, rest_tokens) or None."""
    if not tokens:
        return None
    c = parse_coords(tokens[0])            # single token "lat,lon"
    if c:
        return c[0], c[1], tokens[1:]
    if len(tokens) >= 2:                    # two tokens: "lat," "lon" or "lat" "lon"
        cand = (tokens[0] + tokens[1]) if tokens[0].endswith(",") \
            else (tokens[0] + "," + tokens[1])
        c = parse_coords(cand)
        if c:
            return c[0], c[1], tokens[2:]
    return None

def parse_coords_alias(text):
    """'lat, lon [alias...]' or 'lat,lon [alias...]' -> (lat, lon, alias) or None."""
    m = re.match(r"^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*(.*)$", text)
    if not m:
        return None
    lat, lon = float(m.group(1)), float(m.group(2))
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return lat, lon, m.group(3).strip()

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

def _norm(s):
    """Lowercase and strip diacritics, for forgiving name matching."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return s.lower().strip()

def match_saved(text, chat_id):
    """Find saved locations whose name matches `text` (diacritic/case-insensitive).
    Tiered: exact, then prefix, then substring. Returns a list of (slot, loc)."""
    q = _norm(text)
    if not q:
        return []
    items = list(load_state().get(str(chat_id), {}).get("locations", {}).items())
    exact = [(s, l) for s, l in items if _norm(l.get("name", "")) == q]
    if exact:
        return exact
    prefix = [(s, l) for s, l in items if _norm(l.get("name", "")).startswith(q)]
    if prefix:
        return prefix
    return [(s, l) for s, l in items if q in _norm(l.get("name", ""))]

def find_location(text, chat_id, lang):
    """Resolve to (loc, error). Order: coordinates, saved slot number,
    partial saved-name match, then geocoding."""
    text = text.strip()
    if parse_coords(text):
        return coords_or_city(text), None
    if text.isdigit():
        saved = load_state().get(str(chat_id), {}).get("locations", {}).get(text)
        if saved:
            return saved, None
    m = match_saved(text, chat_id)
    if len(m) == 1:
        return m[0][1], None
    if len(m) > 1:
        names = ", ".join(loc_label(x[1]) for x in m)
        return None, tr("ambiguous", lang, names=names)
    g = geocode(text)
    if g:
        return g, None
    return None, tr("loc_not_found", lang, loc=text)

# --- Open-Meteo: hourly forecast for the wx command ---
def forecast(lat, lon, model_id, units):
    params = {
        "latitude": lat, "longitude": lon,
        "hourly": "temperature_2m,apparent_temperature,precipitation_probability,"
                  "precipitation,weather_code,wind_speed_10m,wind_direction_10m,"
                  "wind_gusts_10m,surface_pressure",
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

def fetch_alert_forecast(lat, lon, model_id):
    """Raw hourly data for alert evaluation, in metric units so the values
    match the thresholds (gust km/h, rain mm, snow cm, temp degC). Returns the
    Open-Meteo JSON consumed by evaluate_alerts()."""
    params = {
        "latitude": lat, "longitude": lon,
        "hourly": "temperature_2m,precipitation,snowfall,wind_gusts_10m",
        "forecast_days": 2, "timezone": "auto",
        "wind_speed_unit": "kmh",
        "precipitation_unit": "mm",
        "temperature_unit": "celsius",
    }
    if model_id and model_id != "best_match":
        params["models"] = model_id
    r = requests.get(FC_URL, params=params, timeout=15)
    r.raise_for_status()
    return r.json()

def format_24h(city, data, model_label, units, lang="en"):
    h = data.get("hourly", {})
    times = h.get("time", [])
    if not times:
        return tr("no_hourly", lang)
    n = len(times)
    temp_a = h.get("temperature_2m", [None] * n)
    feel_a = h.get("apparent_temperature", [None] * n)
    pop_a = h.get("precipitation_probability", [None] * n)
    prec_a = h.get("precipitation", [None] * n)
    code_a = h.get("weather_code", [None] * n)
    wind_a = h.get("wind_speed_10m", [None] * n)
    wdir_a = h.get("wind_direction_10m", [None] * n)
    gust_a = h.get("wind_gusts_10m", [None] * n)
    pres_a = h.get("surface_pressure", [None] * n)

    tlab = UNIT_LABELS["temp"][units["temp"]]
    wlab = UNIT_LABELS["wind"][units["wind"]]
    rlab = UNIT_LABELS["rain"][units["rain"]]
    plab = UNIT_LABELS["pressure"][units["pressure"]]
    feels_w = tr("feels", lang)
    gust_w = tr("gust", lang)

    off = timedelta(seconds=data.get("utc_offset_seconds", 0))
    now_local = (datetime.now(timezone.utc) + off).replace(
        tzinfo=None, minute=0, second=0, microsecond=0)
    start = 0
    for i, t in enumerate(times):
        if datetime.fromisoformat(t) >= now_local:
            start = i
            break

    lines = [f"\U0001f4cd <b>{city}</b> \u2014 {tr('hdr_24h', lang)}",
             tr("src_model", lang, model=model_label) + "\n"]
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
        desc, emo = wmo_desc(code_a[i], lang) if code_a[i] is not None else ("", "")
        wd_s = f" {compass(wdir_a[i])}" if wdir_a[i] is not None else ""
        lines.append(
            f"{t:%H:%M} {emo} {temp}{tlab} ({feels_w} {feel}\u00b0)  "
            f"\U0001f4a7{pop_s}{amt_s}  \U0001f4a8{wind} {wlab}{wd_s} ({gust_w} {gust}){pres_s}  {desc}"
        )
    return "\n".join(lines)

# --- Open-Meteo: multi-day daily forecast ---
def forecast_daily(lat, lon, model_id, units, days):
    params = {
        "latitude": lat, "longitude": lon,
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,"
                 "precipitation_sum,precipitation_probability_max,"
                 "wind_speed_10m_max,wind_gusts_10m_max,wind_direction_10m_dominant",
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

def format_daily(city, data, model_label, units, days, lang="en"):
    d = data.get("daily", {})
    times = d.get("time", [])
    if not times:
        return tr("no_daily", lang)
    n = len(times)
    code = d.get("weather_code", [None] * n)
    tmax = d.get("temperature_2m_max", [None] * n)
    tmin = d.get("temperature_2m_min", [None] * n)
    psum = d.get("precipitation_sum", [None] * n)
    pprob = d.get("precipitation_probability_max", [None] * n)
    wmax = d.get("wind_speed_10m_max", [None] * n)
    gmax = d.get("wind_gusts_10m_max", [None] * n)
    wdir = d.get("wind_direction_10m_dominant", [None] * n)
    tlab = UNIT_LABELS["temp"][units["temp"]]
    wlab = UNIT_LABELS["wind"][units["wind"]]
    rlab = UNIT_LABELS["rain"][units["rain"]]
    gust_w = tr("gust", lang)
    wdays = WEEKDAYS_LANG.get(lang, WEEKDAYS_LANG["en"])
    span = min(days, n)
    lines = [f"\U0001f4cd <b>{city}</b> \u2014 {tr('hdr_days', lang, n=span)}",
             tr("src_model", lang, model=model_label) + "\n"]
    for i in range(span):
        dt = datetime.fromisoformat(times[i])
        wd = wdays[dt.weekday()]
        desc, emo = wmo_desc(code[i], lang) if code[i] is not None else ("", "")
        hi = str(round(tmax[i])) if tmax[i] is not None else "\u2014"
        lo = str(round(tmin[i])) if tmin[i] is not None else "\u2014"
        pr = pprob[i]
        pr_s = f"{pr}%" if pr is not None else "\u2014"
        ps = psum[i]
        ps_s = f" {ps:g}{rlab}" if ps else ""
        wnd = str(round(wmax[i])) if wmax[i] is not None else "\u2014"
        gst = str(round(gmax[i])) if gmax[i] is not None else "\u2014"
        wd_s = f" {compass(wdir[i])}" if wdir[i] is not None else ""
        lines.append(
            f"{wd} {dt:%d.%m} {emo} {lo}/{hi}{tlab}  "
            f"\U0001f4a7{pr_s}{ps_s}  \U0001f4a8{wnd} {wlab}{wd_s} ({gust_w} {gst})  {desc}"
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

def format_alert_line(phenom, val, tstr, lang="en"):
    if phenom == "gust":
        return tr("al_gust", lang, v=round(val), t=tstr)
    if phenom == "rain":
        return tr("al_rain", lang, v=f"{val:.1f}", t=tstr)
    if phenom == "snow":
        return tr("al_snow", lang, v=f"{val:.1f}", t=tstr)
    if phenom == "heat":
        return tr("al_heat", lang, v=round(val), t=tstr)
    if phenom == "frost":
        return tr("al_frost", lang, v=round(val), t=tstr)
    return f"{phenom}: {val} at {tstr}"

def build_alert_message(loc, new_lines, model_label, lang):
    """Assemble a proactive alert message (used by Telegram and WhatsApp)."""
    header = "\u26a0\ufe0f <b>" + tr("alert_header", lang, loc=loc_label(loc)) + "</b>"
    footer = tr("src_model", lang, model=model_label)
    return header + "\n" + "\n".join(new_lines) + "\n\n" + footer

# --- ANM warnings: geometry + point-in-polygon (pure Python, no GIS libs) --------
_MERC = 20037508.342789244

def lonlat_to_mercator(lat, lon):
    x = lon * _MERC / 180.0
    y = math.log(math.tan((90.0 + lat) * math.pi / 360.0)) / (math.pi / 180.0)
    return x, y * _MERC / 180.0

def parse_wkt_rings(wkt):
    """Extract coordinate rings from a WKT POLYGON/MULTIPOLYGON (Web Mercator)."""
    rings = []
    if not wkt:
        return rings
    for m in re.finditer(r"\(([-0-9.eE ,]+)\)", wkt):   # innermost coordinate groups
        ring = []
        for pair in m.group(1).split(","):
            p = pair.split()
            if len(p) >= 2:
                try:
                    ring.append((float(p[0]), float(p[1])))
                except ValueError:
                    pass
        if len(ring) >= 3:
            rings.append(ring)
    return rings

def point_in_ring(x, y, ring):
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside

def point_in_rings(x, y, rings):
    return any(point_in_ring(x, y, r) for r in rings)

def anm_clean(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = html.unescape(s)                 # &ndash;->–, &acirc;->â, &icirc;->î, &nbsp;->space
    return re.sub(r"\s+", " ", s).strip()

def anm_walk(node, ctx, out):
    """Recursively collect every geometry (coordGis) with its inherited text."""
    if isinstance(node, dict):
        attrs = node.get("@attributes", {})
        if not isinstance(attrs, dict):
            attrs = {}
        fields = {k: v for k, v in node.items() if isinstance(v, str)}
        fields.update(attrs)
        newctx = dict(ctx)
        for src, dst in (("mesaj", "mesaj"), ("fenomeneVizate", "fenomen"),
                         ("fenomen", "fenomen"), ("dataExpirarii", "expira"),
                         ("intervalul", "interval")):
            if fields.get(src):
                newctx[dst] = fields[src]
        cg = fields.get("coordGis")
        if cg:
            try:
                cul = int(str(fields.get("culoare", "0")).strip() or 0)
            except ValueError:
                cul = 0
            out.append({
                "rings": parse_wkt_rings(cg),
                "culoare": cul,
                "cod": fields.get("cod", ""),
                "mesaj": newctx.get("mesaj", ""),
                "fenomen": newctx.get("fenomen", ""),
                "expira": newctx.get("expira", ""),
                "interval": newctx.get("interval", ""),
            })
        for k, v in node.items():
            if k != "@attributes":
                anm_walk(v, newctx, out)
    elif isinstance(node, list):
        for v in node:
            anm_walk(v, ctx, out)

def anm_get_areas():
    """Fetch active ANM warnings from all feeds -> list of areas (tagged by feed)."""
    if not ANM_ENABLED:
        return []
    areas = []
    for feed, url in ANM_FEEDS.items():
        try:
            r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            data = r.json()
        except (requests.RequestException, ValueError):
            continue
        if isinstance(data, str):      # "Nu exista date" when nothing is active
            continue
        got = []
        anm_walk(data, {}, got)
        for a in got:
            a["feed"] = feed
        areas.extend(got)
    return areas

def format_anm_alert(loc, area, lang):
    emoji, ro_name, en_name = ANM_COLORS.get(area["culoare"], ("\u26a0\ufe0f", "", ""))
    cname = ro_name if lang == "ro" else en_name
    # ANM text is user-facing free text; escape &, <, > so Telegram's HTML
    # parser accepts the message (otherwise it rejects it and nothing arrives).
    lines = [f"{emoji} <b>{tr('anm_hdr', lang)} \u2014 {html.escape(loc_label(loc))}</b>",
             tr("anm_code", lang, color=html.escape(cname))]
    fen = area.get("fenomen", "")
    if fen and "conform" not in fen.lower():
        lines.append(html.escape(html.unescape(fen.strip())))
    valid = area.get("interval") or area.get("expira")
    if valid and "conform" not in valid.lower():
        lines.append(tr("anm_valid", lang, v=html.escape(html.unescape(valid.strip()))))
    body = anm_clean(area.get("mesaj", ""))
    if body:
        # send() splits long messages, so deliver the full warning (no cut).
        lines.append(html.escape(body.strip()))
    lines.append(tr("anm_src", lang))
    return "\n".join(lines)

def anm_alerts_for(chat_id, slot, loc, areas, lang):
    """Return ANM alert messages for a location (point-in-polygon), deduped per day."""
    if not areas:
        return []
    feeds = get_anm_feeds(chat_id)
    if not feeds:                       # user turned ANM off for this chat
        return []
    areas = [a for a in areas if a.get("feed", "") in feeds]
    if not areas:
        return []
    x, y = lonlat_to_mercator(loc["lat"], loc["lon"])
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    msgs = []
    for area in areas:
        if not point_in_rings(x, y, area["rings"]):
            continue
        # Dedup on the actual warning CONTENT (colour + phenomenon + message body),
        # not on volatile fields, and with no date in the key -> the same bulletin
        # is never resent (fixes identical messages arriving repeatedly).
        body = anm_clean(area.get("mesaj", ""))
        sig = hashlib.md5(
            f"{area.get('culoare','')}|{area.get('fenomen','')}|{body}".encode()).hexdigest()[:12]
        key = f"anm:{slot}:{sig}"
        if already_sent(chat_id, key):
            continue
        mark_sent(chat_id, key, today)
        msgs.append(format_anm_alert(loc, area, lang))
    return msgs

# --- Radar / satellite maps (RainViewer tiles + OpenStreetMap base) --------------
TILE = 256
MAP_ZOOM = int(os.environ.get("TG_MAP_ZOOM", "6"))     # regional view (RainViewer max 7)
MAP_W = int(os.environ.get("TG_MAP_W", "720"))
MAP_H = int(os.environ.get("TG_MAP_H", "720"))
MAP_BASE_DIM = float(os.environ.get("TG_MAP_BASE_DIM", "0.55"))  # 0=OSM full color, 1=white-out
OSM_TILE = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
# Dark basemap (CARTO "dark_all") — weather overlays read much better on it.
DARK_TILE = os.environ.get("TG_DARK_TILE",
                           "https://basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png")
MAP_THEME_DEFAULT = os.environ.get("TG_MAP_THEME", "light")   # 'light' or 'dark'
# On a dark base the clouds must be light to stand out; on light, dark grey.
CLOUD_RGB_DARK = tuple(int(x) for x in
                       os.environ.get("TG_CLOUD_RGB_DARK", "235,235,240").split(","))[:3]
RAINVIEWER_INDEX = "https://api.rainviewer.com/public/weather-maps.json"
# Cloud cover comes from Open-Meteo (RainViewer's free tier has no satellite):
CLOUD_URL = "https://api.open-meteo.com/v1/forecast"
CLOUD_COLS = int(os.environ.get("TG_CLOUD_COLS", "20"))
CLOUD_ROWS = int(os.environ.get("TG_CLOUD_ROWS", "16"))
CLOUD_MAX_ALPHA = int(os.environ.get("TG_CLOUD_ALPHA", "225"))   # opacity at 100% overcast
CLOUD_RGB = tuple(int(x) for x in os.environ.get("TG_CLOUD_RGB", "105,105,105").split(","))[:3]
_TILE_UA = {"User-Agent": "OpenMeteoBot/1.0 (personal weather bot)"}
_rv_cache = {"t": 0, "data": None}

class Photo:
    """Marker return type: an image response instead of text."""
    def __init__(self, data, caption=""):
        self.data = data
        self.caption = caption

def lonlat_to_tilexy(lat, lon, z):
    n = 2 ** z
    x = (lon + 180.0) / 360.0 * n
    y = (1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n
    return x, y

def tilexy_to_lonlat(x, y, z):
    """Inverse of lonlat_to_tilexy -> (lat, lon)."""
    n = 2 ** z
    lon = x / n * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * y / n))))
    return lat, lon

_TZ_OFFSET_RE = re.compile(r"^([+-])(\d{1,2})(?::?(\d{2}))?$")

def _resolve_tz(tz):
    """tzinfo for a setting: ''/'auto'/'server' -> None (server local); '+3'/'-2:30'
    -> fixed offset; otherwise an IANA name (e.g. 'Europe/Bucharest')."""
    if not tz or str(tz).lower() in ("auto", "server", "local"):
        return None
    mt = _TZ_OFFSET_RE.match(str(tz).strip())
    if mt:
        sign = 1 if mt.group(1) == "+" else -1
        return timezone(sign * timedelta(hours=int(mt.group(2)), minutes=int(mt.group(3) or 0)))
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(str(tz).strip())
    except Exception:
        return None

def local_time_label(dt_utc, tz):
    """Format an aware UTC datetime as 'HH:MM TZ' in the chosen tz (server local if unset)."""
    if dt_utc is None:
        return ""
    tzi = _resolve_tz(tz)
    local = dt_utc.astimezone(tzi) if tzi is not None else dt_utc.astimezone()
    abbr = local.strftime("%Z") or "local"
    return f"{local.strftime('%H:%M')} {abbr}"

def rainviewer_frames():
    """Cached RainViewer index -> latest radar & satellite tile paths."""
    now = time.time()
    if _rv_cache["data"] and now - _rv_cache["t"] < 300:
        return _rv_cache["data"]
    out = {"host": "", "radar": None, "satellite": None, "radar_time": 0, "sat_time": 0}
    try:
        r = requests.get(RAINVIEWER_INDEX, timeout=15, headers=_TILE_UA)
        r.raise_for_status()
        j = r.json()
        out["host"] = j.get("host", "https://tilecache.rainviewer.com")
        past = (j.get("radar", {}) or {}).get("past", [])
        if past:
            out["radar"] = past[-1]["path"]
            out["radar_time"] = past[-1]["time"]
        ir = (j.get("satellite", {}) or {}).get("infrared", [])
        if ir:
            out["satellite"] = ir[-1]["path"]
            out["sat_time"] = ir[-1]["time"]
    except (requests.RequestException, ValueError, KeyError, IndexError):
        pass
    _rv_cache["t"] = now
    _rv_cache["data"] = out
    return out

def _fetch_img(url):
    try:
        r = requests.get(url, timeout=12, headers=_TILE_UA)
        if r.status_code != 200 or not r.content:
            return None
        return Image.open(BytesIO(r.content)).convert("RGBA")
    except Exception:
        return None

def _overlay_url(frames, layer, z, x, y):
    host = frames["host"]
    path = frames.get(layer)
    if not path:
        return None
    if layer == "radar":
        # Colour scheme 2 = "Universal Blue" — the only one the public API offers,
        # and the one our legend bar is built from. 1_1 = smoothed + snow.
        return f"{host}{path}/{TILE}/{z}/{x}/{y}/{RV_COLOR_SCHEME}/1_1.png"
    return f"{host}{path}/{TILE}/{z}/{x}/{y}/0/0_0.png"          # satellite

def _sample_field(bbox, variable, cols, rows, when=None, model_id=None):
    """Sample an Open-Meteo variable on a cols×rows grid over bbox=(latN,lonW,latS,lonE).
    `when` = UTC datetime for a forecast hour (None = current).
    Returns (values row-major, iso_time, cols_used, rows_used) or (None, '', 0, 0)."""
    latN, lonW, latS, lonE = bbox
    lats, lons = [], []
    for r in range(rows):
        lat = latN + (latS - latN) * (r + 0.5) / rows            # row 0 = north (top)
        for c in range(cols):
            lon = lonW + (lonE - lonW) * (c + 0.5) / cols
            # 3 decimals (~110 m) keeps the query string short: too many long
            # coordinates make the URL exceed the server limit and the call fails.
            lats.append(f"{lat:.3f}"); lons.append(f"{lon:.3f}")
    params = {"latitude": ",".join(lats),
              "longitude": ",".join(lons), "timezone": "UTC"}
    if model_id and model_id != "best_match":     # same model as maps.open-meteo.com
        params["models"] = model_id
    if when is None:
        params["current"] = variable
    else:                                    # one single hour keeps the response small
        hh = when.strftime("%Y-%m-%dT%H:00")
        params.update({"hourly": variable, "start_hour": hh, "end_hour": hh})
    try:
        rr = requests.get(CLOUD_URL, params=params, timeout=25, headers=_TILE_UA)
        rr.raise_for_status()
        data = rr.json()
    except (requests.RequestException, ValueError) as e:
        # Causes: too many grid points -> URL too long; or the chosen regional model
        # rejects a multi-point `current=`/`hourly=` query. Retry on a coarser grid,
        # then (if a model was pinned) retry without it so global data still renders.
        print(f"[map] field '{variable}' failed on {cols}x{rows} grid "
              f"(model={model_id}): {e}", flush=True)
        if cols > 8 and rows > 6:
            return _sample_field(bbox, variable, max(8, cols // 2), max(6, rows // 2),
                                 when, model_id)
        if model_id:                       # drop the model -> Open-Meteo best_match
            print(f"[map] retrying field '{variable}' without model", flush=True)
            return _sample_field(bbox, variable, cols, rows, when, None)
        return None, "", 0, 0
    results = data if isinstance(data, list) else [data]
    vals, tstamp = [], ""
    for res in results:
        if not isinstance(res, dict):
            vals.append(None); continue
        if when is None:
            cur = res.get("current", {})
            if not tstamp:
                tstamp = cur.get("time", "")
            vals.append(cur.get(variable))
        else:
            hr = res.get("hourly", {}) or {}
            times = hr.get("time") or []
            arr = hr.get(variable) or []
            if not tstamp and times:
                tstamp = times[0]
            vals.append(arr[0] if arr else None)
    if model_id and all(v is None for v in vals):
        # The pinned regional model has no coverage at these points -> fall back to
        # the global best_match so the overlay isn't silently blank.
        print(f"[map] field '{variable}' all-null for model {model_id}; "
              f"falling back to best_match", flush=True)
        return _sample_field(bbox, variable, cols, rows, when, None)
    return vals, tstamp, cols, rows

def _cloud_overlay(bbox, w, h, rgb=None, max_alpha=None, variable="cloud_cover", when=None, model_id=None):
    """Cloud-cover overlay (total / low / mid / high) as a translucent grey field.
    Returns (RGBA_overlay, iso_time) or (None, '')."""
    rgb = tuple(rgb) if rgb else CLOUD_RGB
    max_alpha = CLOUD_MAX_ALPHA if max_alpha is None else max_alpha
    vals, tstamp, gc, gr = _sample_field(bbox, variable, CLOUD_COLS, CLOUD_ROWS, when, model_id)
    if not vals or all(v is None for v in vals):
        return None, ""
    grid = Image.new("RGBA", (gc, gr), (0, 0, 0, 0))
    px = grid.load()
    for i, cc in enumerate(vals):
        c, row = i % gc, i // gc
        if cc is None or row >= gr:
            continue
        a = int(max_alpha * max(0.0, min(100.0, float(cc))) / 100.0)
        px[c, row] = rgb + (a,)                                  # denser cloud = more opaque
    overlay = grid.resize((w, h), Image.BICUBIC)                 # smooth field
    return overlay, tstamp                                       # ISO time (UTC) or ''

def _rh_overlay(bbox, w, h, when=None, alpha=None, model_id=None):
    """Relative-humidity field using Open-Meteo's official 'relative' palette."""
    alpha = TEMP_ALPHA if alpha is None else alpha
    vals, tstamp, gc, gr = _sample_field(bbox, "relative_humidity_2m", TEMP_COLS, TEMP_ROWS, when, model_id)
    if not vals or all(v is None for v in vals):
        return None, "", None, None
    grid = Image.new("RGBA", (gc, gr), (0, 0, 0, 0))
    px = grid.load()
    got = []
    for i, v in enumerate(vals):
        c, row = i % gc, i // gc
        if v is None or row >= gr:
            continue
        got.append(float(v))
        px[c, row] = rh_color(float(v)) + (alpha,)
    if not got:
        return None, "", None, None
    return grid.resize((w, h), Image.BICUBIC), tstamp, min(got), max(got)

# --- Temperature map (Open-Meteo temperature_2m sampled on a grid) ---------------
# Open-Meteo serves no map tiles, so we sample the field and colour it ourselves.
# Open-Meteo's OFFICIAL temperature colour scale (the one maps.open-meteo.com uses),
# taken from @openmeteo/weather-map-layer. It is a *breakpoint* (step) scale: a value
# takes the colour of the last breakpoint <= value.
OM_TEMP_BREAKS = (
    -80, -60, -50, -40, -37.5, -35, -32.5, -30, -27.5, -25, -22.5, -20, -17.5, -15,
    -12.5, -10, -8, -6, -4, -2, 0, 2, 4, 6, 8, 10, 12, 14, 16, 18, 20, 22, 24, 26,
    28, 30, 32, 34, 36, 38, 40, 42, 44, 46, 48, 50,
)
OM_TEMP_COLORS = (
    (26, 242, 221), (22, 147, 178), (23, 101, 143), (47, 17, 189), (86, 15, 201),
    (131, 13, 213), (181, 10, 226), (239, 7, 239), (206, 6, 241), (171, 5, 243),
    (136, 4, 245), (100, 4, 247), (64, 3, 249), (26, 2, 251), (1, 14, 253),
    (0, 52, 255), (35, 110, 251), (69, 156, 247), (102, 192, 245), (134, 219, 245),
    (124, 245, 124), (90, 244, 90), (56, 244, 56), (21, 245, 21), (7, 224, 7),
    (4, 193, 4), (2, 161, 2), (0, 128, 0), (57, 170, 0), (142, 213, 0),
    (255, 255, 0), (255, 233, 0), (255, 210, 0), (255, 188, 0), (255, 165, 0),
    (255, 141, 0), (255, 118, 0), (255, 94, 0), (255, 71, 0), (255, 47, 0),
    (255, 24, 0), (255, 0, 0), (228, 0, 10), (201, 0, 18), (174, 0, 23), (147, 0, 26),
)
# Open-Meteo's official "relative" (humidity) scale, same source as the temp one.
OM_RH_BREAKS = tuple(range(0, 100, 5))
OM_RH_COLORS = (
    (207, 89, 126), (215, 95, 116), (223, 102, 106), (230, 124, 110), (233, 142, 115),
    (235, 156, 117), (237, 171, 119), (237, 186, 125), (236, 202, 135), (234, 215, 146),
    (233, 226, 156), (214, 225, 148), (188, 216, 141), (164, 207, 136), (128, 199, 117),
    (92, 194, 108), (65, 190, 120), (49, 176, 134), (29, 170, 139), (13, 160, 143),
)

def rh_color(v):
    """Relative humidity % -> RGB (Open-Meteo breakpoint scale)."""
    idx = 0
    for i, b in enumerate(OM_RH_BREAKS):
        if v >= b:
            idx = i
        else:
            break
    return OM_RH_COLORS[idx]

TEMP_COLS = int(os.environ.get("TG_TEMP_COLS", "20"))
TEMP_ROWS = int(os.environ.get("TG_TEMP_ROWS", "16"))
TEMP_ALPHA = int(os.environ.get("TG_TEMP_ALPHA", "170"))   # let the map show through

def temp_color(c):
    """Temperature in °C -> RGB, using Open-Meteo's official breakpoint scale
    (colour of the last breakpoint <= value), same as maps.open-meteo.com."""
    idx = 0
    for i, b in enumerate(OM_TEMP_BREAKS):
        if c >= b:
            idx = i
        else:
            break
    return OM_TEMP_COLORS[idx]

def _temp_overlay(bbox, w, h, alpha=None, when=None, model_id=None):
    """Temperature field over bbox=(latN, lonW, latS, lonE).
    Returns (RGBA_overlay, iso_time, min_C, max_C) or (None, '', None, None)."""
    alpha = TEMP_ALPHA if alpha is None else alpha
    vals, tstamp, gc, gr = _sample_field(bbox, "temperature_2m", TEMP_COLS, TEMP_ROWS, when, model_id)
    if not vals:
        return None, "", None, None
    grid = Image.new("RGBA", (gc, gr), (0, 0, 0, 0))
    px = grid.load()
    got = []
    for i, t in enumerate(vals):
        c, row = i % gc, i // gc
        if t is None or row >= gr:
            continue
        got.append(float(t))
        px[c, row] = temp_color(float(t)) + (alpha,)
    if not got:
        return None, "", None, None
    return grid.resize((w, h), Image.BICUBIC), tstamp, min(got), max(got)

# --- Legend bar built from RainViewer's OFFICIAL colour table --------------------
# Scheme 2 "Universal Blue" (the only one the public API serves), rain colours for
# dBZ 15..65, taken from rainviewer.com/api/color-schemes.html (colour table CSV).
RV_COLOR_SCHEME = int(os.environ.get("TG_RV_COLOR", "2"))
RV_DBZ_MIN, RV_DBZ_MAX = 15, 65
RV_UNIVERSAL_BLUE = (
    "88ddee", "6cd1eb", "51c5e8", "36bae5", "1baee2",   # 15-19  light rain
    "00a3e0", "009ad5", "0091ca", "0088bf", "007fb4",   # 20-24
    "0077aa", "0070a3", "00699c", "006295", "005b8e",   # 25-29
    "005588", "005180", "004e78", "004a70", "004768",   # 30-34  moderate
    "ffee00", "ffe000", "ffd200", "ffc500", "ffb700",   # 35-39  heavy
    "ffaa00", "ff9f00", "ff9500", "ff8b00", "ff8100",   # 40-44
    "ff4400", "f23600", "e62800", "d91b00", "cd0d00",   # 45-49  very heavy
    "c10000", "a80000", "8f0000", "760000", "5d0000",   # 50-54
    "ffaaff", "ff9fff", "ff95ff", "ff8bff", "ff81ff",   # 55-59  extreme / hail
    "ff77ff", "ff6cff", "ff62ff", "ff58ff", "ff4eff",   # 60-64
    "ffffff",                                            # 65+
)

def _hex_rgb(h):
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))

def _load_font(size):
    for path in ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                 "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    try:
        return ImageFont.load_default()
    except Exception:
        return None

def _legend_bar(width, left_label, right_label, title, height=24):
    """RainViewer's official Universal Blue scale, drawn as a dBZ colour bar."""
    pad = 10
    font = _load_font(13)
    small = _load_font(11)
    title_h = 18 if title else 0
    img = Image.new("RGBA", (width, title_h + height + 34), (255, 255, 255, 255))
    d = ImageDraw.Draw(img)
    if title:
        d.text((pad, 2), title, fill=(30, 30, 30, 255), font=font)
    bar_w = width - 2 * pad
    n = len(RV_UNIVERSAL_BLUE)
    seg = bar_w / float(n)
    y0 = title_h + 2
    for i, hx in enumerate(RV_UNIVERSAL_BLUE):          # one segment per dBZ step
        x0 = pad + int(round(i * seg))
        x1 = pad + int(round((i + 1) * seg)) + 1
        d.rectangle([x0, y0, x1, y0 + height], fill=_hex_rgb(hx) + (255,))
    d.rectangle([pad, y0, pad + bar_w, y0 + height], outline=(90, 90, 90, 255), width=1)

    ty = y0 + height + 2
    for dbz in range(RV_DBZ_MIN, RV_DBZ_MAX + 1, 10):   # dBZ ticks: 15,25,...,65
        frac = (dbz - RV_DBZ_MIN) / float(RV_DBZ_MAX - RV_DBZ_MIN)
        x = pad + frac * bar_w
        d.line([x, y0 + height, x, y0 + height + 3], fill=(90, 90, 90, 255), width=1)
        txt = f"{dbz}"
        try:
            tw = d.textlength(txt, font=small)
        except Exception:
            tw = 6 * len(txt)
        d.text((min(max(pad, x - tw / 2), pad + bar_w - tw), ty + 2),
               txt, fill=(40, 40, 40, 255), font=small)
    ty2 = ty + 15
    d.text((pad, ty2), left_label, fill=(40, 40, 40, 255), font=small)
    try:
        tw = d.textlength(right_label, font=small)
    except Exception:
        tw = 6 * len(right_label)
    d.text((pad + bar_w - tw, ty2), right_label, fill=(40, 40, 40, 255), font=small)
    return img

def _temp_bar(width, vmin_c, vmax_c, title, unit="C", height=24,
              color_fn=None, step=None):
    """Colour-scale bar (temperature by default) with tick labels."""
    color_fn = color_fn or temp_color
    pad = 10
    font = _load_font(13)
    small = _load_font(11)
    # widen a little so the range reads nicely, then snap to 5-unit steps
    lo = math.floor((vmin_c - 1) / 5.0) * 5
    hi = math.ceil((vmax_c + 1) / 5.0) * 5
    if hi - lo < 10:
        hi = lo + 10
    title_h = 18
    img = Image.new("RGBA", (width, title_h + height + 34), (255, 255, 255, 255))
    d = ImageDraw.Draw(img)
    d.text((pad, 2), title, fill=(30, 30, 30, 255), font=font)
    bar_w = width - 2 * pad
    y0 = title_h + 2
    for x in range(bar_w):                       # 1px columns follow the palette steps
        c = lo + (hi - lo) * (x / float(max(1, bar_w - 1)))
        d.line([pad + x, y0, pad + x, y0 + height], fill=color_fn(c) + (255,))
    d.rectangle([pad, y0, pad + bar_w, y0 + height], outline=(90, 90, 90, 255), width=1)

    if step is None:
        step = 5 if (hi - lo) <= 40 else 10
    ty = y0 + height + 4
    t = lo
    while t <= hi:
        frac = (t - lo) / float(hi - lo)
        x = pad + frac * bar_w
        d.line([x, y0 + height, x, y0 + height + 3], fill=(90, 90, 90, 255), width=1)
        shown = t * 9.0 / 5.0 + 32.0 if unit == "F" else t
        txt = f"{shown:.0f}"
        try:
            tw = d.textlength(txt, font=small)
        except Exception:
            tw = 6 * len(txt)
        d.text((min(max(pad, x - tw / 2), pad + bar_w - tw), ty), txt,
               fill=(40, 40, 40, 255), font=small)
        t += step
    return img

def _stack_below(base, strip):
    """Return base with `strip` appended underneath (same width)."""
    bw, bh = base.size
    if strip.size[0] != bw:
        strip = strip.resize((bw, max(1, int(strip.size[1] * bw / float(strip.size[0])))),
                             Image.LANCZOS)
    out = Image.new("RGBA", (bw, bh + strip.size[1]), (255, 255, 255, 255))
    out.paste(base, (0, 0))
    out.alpha_composite(strip, (0, bh))
    return out

def build_map(lat, lon, layers, z=None, w=None, h=None, base_dim=0.0,
              cloud_rgb=None, cloud_alpha=None, tz=None, legend=None,
              cloud_var="cloud_cover", when=None, theme="light", model_id=None):
    """Stitch OSM base + weather overlays, centered on the point, with a marker.
    `layers` items: 'radar'/'satellite' (RainViewer tiles) or 'clouds' (Open-Meteo).
    `base_dim` (0..1) washes out the OSM base so weather stands out.
    `cloud_rgb`/`cloud_alpha` override the cloud shading; `tz` localises the time.
    Returns (png_bytes, time_label) or (None, '')."""
    if not _PIL:
        return None, ""
    z = z or MAP_ZOOM
    w = w or MAP_W
    h = h or MAP_H
    n = 2 ** z
    need_rv = any(l in ("radar", "satellite") for l in layers)
    frames = rainviewer_frames() if need_rv else {
        "host": "", "radar": None, "satellite": None, "radar_time": 0, "sat_time": 0}
    fx, fy = lonlat_to_tilexy(lat, lon, z)
    cpx, cpy = fx * TILE, fy * TILE
    left, top = cpx - w / 2.0, cpy - h / 2.0
    x0, x1 = math.floor(left / TILE), math.floor((left + w) / TILE)
    y0, y1 = math.floor(top / TILE), math.floor((top + h) / TILE)
    canvas = Image.new("RGBA", (w, h), (30, 30, 30, 255))

    def paste_layer(getter, composite):
        # alpha_composite() rejects negative destinations, and edge tiles start
        # off-canvas (negative px/py). So build the overlay on its own layer with
        # paste() (which allows negatives), then composite the whole layer at (0,0).
        layer_img = Image.new("RGBA", (w, h), (0, 0, 0, 0)) if composite else None
        for tx in range(x0, x1 + 1):
            for ty in range(y0, y1 + 1):
                if ty < 0 or ty >= n:
                    continue
                img = getter(tx % n, ty)
                if not img:
                    continue
                px = int(round(tx * TILE - left))
                py = int(round(ty * TILE - top))
                if composite:
                    layer_img.paste(img, (px, py), img)   # negatives OK; use alpha as mask
                else:
                    canvas.paste(img, (px, py))
        if composite:
            canvas.alpha_composite(layer_img)             # dest (0,0) -> valid

    dark = (theme == "dark")
    tile_url = DARK_TILE if dark else OSM_TILE
    paste_layer(lambda x, y: _fetch_img(tile_url.format(z=z, x=x, y=y)), False)
    if base_dim > 0:                                  # fade the base map
        wash = (0, 0, 0) if dark else (255, 255, 255)   # darken on dark, whiten on light
        canvas.alpha_composite(Image.new("RGBA", (w, h), wash + (int(255 * min(1.0, base_dim)),)))
    src_dt = None                                     # UTC datetime of the freshest layer
    trange = None                                     # (min, max) for the gradient legend

    def _bbox():
        latN, lonW = tilexy_to_lonlat(left / TILE, top / TILE, z)
        latS, lonE = tilexy_to_lonlat((left + w) / TILE, (top + h) / TILE, z)
        return (latN, lonW, latS, lonE)

    def _stamp(iso):
        nonlocal src_dt
        if iso and src_dt is None:
            try:
                src_dt = datetime.fromisoformat(iso).replace(tzinfo=timezone.utc)
            except ValueError:
                pass

    for layer in layers:
        if layer == "heat":
            overlay, iso, tmin, tmax = _temp_overlay(_bbox(), w, h, when=when, model_id=model_id)
            if overlay is not None:
                canvas.alpha_composite(overlay)
                trange = (tmin, tmax)
                _stamp(iso)
            continue
        if layer == "humidity":
            overlay, iso, hmin, hmax = _rh_overlay(_bbox(), w, h, when=when, model_id=model_id)
            if overlay is not None:
                canvas.alpha_composite(overlay)
                trange = (hmin, hmax)
                _stamp(iso)
            continue
        if layer == "clouds":
            crgb = cloud_rgb
            if dark and (crgb is None or tuple(crgb) == CLOUD_RGB):
                crgb = CLOUD_RGB_DARK           # light clouds read better on a dark base
            overlay, iso = _cloud_overlay(_bbox(), w, h, crgb, cloud_alpha,
                                          variable=cloud_var, when=when, model_id=model_id)
            if overlay is not None:
                canvas.alpha_composite(overlay)
                _stamp(iso)
            continue
        url_of = lambda x, y, L=layer: _overlay_url(frames, L, z, x, y)
        paste_layer(lambda x, y: _fetch_img(url_of(x, y)) if url_of(x, y) else None, True)
        ts = frames.get("radar_time" if layer == "radar" else "sat_time")
        if ts and src_dt is None:
            src_dt = datetime.fromtimestamp(ts, timezone.utc)
    tlabel = local_time_label(src_dt, tz)

    # marker at the exact point (center of the canvas)
    d = ImageDraw.Draw(canvas)
    mx, my = w // 2, h // 2
    d.ellipse([mx - 7, my - 7, mx + 7, my + 7], outline=(255, 0, 0, 255), width=3)
    d.ellipse([mx - 2, my - 2, mx + 2, my + 2], fill=(255, 0, 0, 255))

    if legend and "humidity" in layers and trange:    # humidity scale (0..100 %)
        try:
            canvas = _stack_below(canvas, _temp_bar(
                w, 0, 100, legend.get("title", ""), unit="%", color_fn=rh_color, step=10))
        except Exception:
            pass
    elif legend and "heat" in layers and trange:      # temperature gradient scale
        try:
            canvas = _stack_below(canvas, _temp_bar(
                w, trange[0], trange[1], legend.get("title", ""), legend.get("unit", "C")))
        except Exception:
            pass
    elif legend and "radar" in layers:    # generated bar (RainViewer has no scale image)
        try:
            canvas = _stack_below(canvas, _legend_bar(
                w, legend.get("left", ""), legend.get("right", ""), legend.get("title", "")))
        except Exception:
            pass                          # never fail the map because of the legend

    buf = BytesIO()
    canvas.convert("RGB").save(buf, format="PNG")
    return buf.getvalue(), tlabel

# --- ANM national radar image, georeferenced over an OSM base --------------------
# The ANM viewer draws the radar PNG as a Leaflet imageOverlay stretched to fixed
# lat/lon bounds. We replicate that: build an OSM base for the SAME Web-Mercator
# rectangle and stretch the radar onto it, so borders line up with ANM's own view.
# BBOX order: West,South,East,North (lon/lat). Tune with TG_ANM_BBOX if borders drift.
# Exact bounds from ANM's Leaflet imageOverlay (Web Mercator EPSG:3857, converted
# to lon/lat): X 2000709.43..3503967.83 m, Y 5162129.78..6302543.86 m.
ANM_BBOX = tuple(float(x) for x in
                 os.environ.get("TG_ANM_BBOX", "17.9727,42.0465,31.4767,49.1441").split(","))[:4]
ANM_MAP_W = int(os.environ.get("TG_ANM_MAP_W", "1000"))        # output width in px
ANM_RADAR_URL = os.environ.get(
    "TG_ANM_RADAR_URL",
    "https://www.meteoromania.ro/radar/mos.live.{date}.{hm}.0_mercator.png")
ANM_RADAR_OFFSET_MIN = int(os.environ.get("TG_ANM_RADAR_OFFSET", "1"))
ANM_RADAR_LOOKBACK = int(os.environ.get("TG_ANM_RADAR_LOOKBACK", "9"))
_anm_radar_cache = {"t": 0, "png": None, "dt": None}
# ANM's own reflectivity (dBZ) colour scale, as shown on their radar page.
ANM_SCALE_URL = os.environ.get("TG_ANM_SCALE_URL",
                               "https://www.meteoromania.ro/radarm/img/sclrZ.png")
ANM_SHOW_SCALE = os.environ.get("TG_ANM_SCALE", "1") != "0"
_anm_scale_cache = {"t": 0, "img": None}

def anm_scale_image():
    """ANM's official colour scale image (cached ~1h). None if unavailable."""
    if not _PIL:
        return None
    now = time.time()
    if _anm_scale_cache["img"] is not None and now - _anm_scale_cache["t"] < 3600:
        return _anm_scale_cache["img"]
    img = _fetch_img(ANM_SCALE_URL)
    if img is not None:
        _anm_scale_cache.update(t=now, img=img)
    return img

def _attach_scale(base, scale, pad=8, max_frac=0.18):
    """Return a new image = `base` with ANM's scale strip added underneath."""
    bw, bh = base.size
    sw, sh = scale.size
    target_w = bw - 2 * pad
    new_h = max(1, int(round(sh * target_w / float(sw))))
    if new_h > int(bh * max_frac):                 # too tall (vertical bar) -> fit by height
        new_h = int(bh * max_frac)
        target_w = max(1, int(round(sw * new_h / float(sh))))
    strip = scale.resize((target_w, new_h), Image.LANCZOS)
    out = Image.new("RGBA", (bw, bh + new_h + 2 * pad), (255, 255, 255, 255))
    out.paste(base, (0, 0))
    out.alpha_composite(strip, ((bw - target_w) // 2, bh + pad))
    return out

def _anm_radar_candidates(now=None):
    """UTC timestamps to try, newest first: minute floored to 10 + OFFSET, then back."""
    now = (now or datetime.now(timezone.utc)).replace(second=0, microsecond=0)
    base = now.replace(minute=(now.minute // 10) * 10)
    if base + timedelta(minutes=ANM_RADAR_OFFSET_MIN) > now:
        base -= timedelta(minutes=10)
    return [base - timedelta(minutes=10 * i) + timedelta(minutes=ANM_RADAR_OFFSET_MIN)
            for i in range(ANM_RADAR_LOOKBACK + 1)]

def anm_radar_image():
    """Latest ANM national radar PNG. Returns (png_bytes, dt_utc) or (None, None)."""
    now = time.time()
    if _anm_radar_cache["png"] and now - _anm_radar_cache["t"] < 300:
        return _anm_radar_cache["png"], _anm_radar_cache["dt"]
    for ts in _anm_radar_candidates():
        url = ANM_RADAR_URL.format(date=ts.strftime("%Y%m%d"), hm=ts.strftime("%H%M"))
        try:
            r = requests.get(url, timeout=12, headers={"User-Agent": "Mozilla/5.0"})
        except requests.RequestException:
            continue
        if r.status_code == 200 and r.content and "image" in r.headers.get("Content-Type", "").lower():
            _anm_radar_cache.update(t=now, png=r.content, dt=ts)
            return r.content, ts
    return None, None

def _merc_y(lat):
    return (1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0   # 0..1, north small

def _osm_base_for_bbox(W, S, E, N, out_w):
    """Stitch an OSM base covering the Web-Mercator rectangle of the bbox.
    Returns (canvas, (w, h), (nx0, ny0, sx, sy)) where the tuple maps lon/lat to px."""
    nx0, nx1 = (W + 180.0) / 360.0, (E + 180.0) / 360.0
    ny0, ny1 = _merc_y(N), _merc_y(S)                          # top=north, bottom=south
    z = int(round(math.log2(max(1.0, out_w / (TILE * (nx1 - nx0))))))
    z = max(3, min(9, z))
    n = 2 ** z
    wx0, wx1 = nx0 * n * TILE, nx1 * n * TILE
    wy0, wy1 = ny0 * n * TILE, ny1 * n * TILE
    w, h = int(round(wx1 - wx0)), int(round(wy1 - wy0))
    canvas = Image.new("RGBA", (w, h), (235, 235, 235, 255))
    tx0, tx1 = math.floor(wx0 / TILE), math.floor(wx1 / TILE)
    ty0, ty1 = math.floor(wy0 / TILE), math.floor(wy1 / TILE)
    for tx in range(tx0, tx1 + 1):
        for ty in range(ty0, ty1 + 1):
            if ty < 0 or ty >= n:
                continue
            img = _fetch_img(OSM_TILE.format(z=z, x=tx % n, y=ty))
            if not img:
                continue
            canvas.paste(img, (int(round(tx * TILE - wx0)), int(round(ty * TILE - wy0))))
    return canvas, (w, h), (nx0, ny0, (nx1 - nx0), (ny1 - ny0))

def build_anm_radar_map(mlat=None, mlon=None, base_dim=None, bbox=None, tz=None):
    """ANM national radar stretched onto a faded OSM base (georeferenced by bbox).
    Optional marker at (mlat, mlon). Returns (png_bytes, time_label) or (None, '')."""
    if not _PIL:
        return None, ""
    base_dim = MAP_BASE_DIM if base_dim is None else base_dim
    png_bytes, dt_utc = anm_radar_image()
    if not png_bytes:
        return None, ""
    label = local_time_label(dt_utc, tz)
    W, S, E, N = bbox if bbox else ANM_BBOX
    base, (w, h), (nx0, ny0, dnx, dny) = _osm_base_for_bbox(W, S, E, N, ANM_MAP_W)
    if base_dim > 0:                                           # fade the base
        base.alpha_composite(Image.new("RGBA", (w, h), (255, 255, 255, int(255 * min(1.0, base_dim)))))
    try:
        radar = Image.open(BytesIO(png_bytes)).convert("RGBA").resize((w, h), Image.BILINEAR)
    except Exception:
        return None, ""
    base.alpha_composite(radar)                               # respects the radar's own alpha
    if mlat is not None and mlon is not None:
        px = ((mlon + 180.0) / 360.0 - nx0) / dnx * w
        py = (_merc_y(mlat) - ny0) / dny * h
        if 0 <= px <= w and 0 <= py <= h:
            d = ImageDraw.Draw(base)
            mx, my = int(px), int(py)
            d.ellipse([mx - 7, my - 7, mx + 7, my + 7], outline=(255, 0, 0, 255), width=3)
            d.ellipse([mx - 2, my - 2, mx + 2, my + 2], fill=(255, 0, 0, 255))
    if ANM_SHOW_SCALE:                    # ANM's own dBZ colour scale, under the map
        scale = anm_scale_image()
        if scale is not None:
            try:
                base = _attach_scale(base, scale)
            except Exception:
                pass                      # never fail the map because of the scale
    out = BytesIO()
    base.convert("RGB").save(out, format="PNG")
    return out.getvalue(), label

def already_sent(chat_id, key):
    return key in load_state().get(str(chat_id), {}).get("alerts_sent", {})

def mark_sent(chat_id, key, today):
    """Record that `key` was sent. Stores the date so marks can survive past
    midnight (ANM dedup) and get pruned by age instead of by an exact-day suffix."""
    def m(state):
        c = state.setdefault(str(chat_id), {})
        sent = c.setdefault("alerts_sent", {})
        try:
            cutoff = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=2)).strftime("%Y-%m-%d")
        except ValueError:
            cutoff = ""
        for k in list(sent.keys()):          # prune marks older than ~2 days
            v = sent[k]
            d = v if isinstance(v, str) else ""   # legacy True -> prune
            if not d or d < cutoff:
                del sent[k]
        sent[key] = today
    update_state(m)

# --- Commands ---
def cmd_wx(args, chat_id):
    lang = get_lang(chat_id)
    if not args:
        return tr("wx_usage", lang)
    units = get_units(chat_id)
    model_id, model_label = resolve_model(get_model(chat_id))
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
            return tr("wx_usage_short", lang)
        locs = load_state().get(str(chat_id), {}).get("locations", {})
        if not locs:
            return tr("no_saved_wx", lang)
        out = []
        for slot in sorted(locs, key=int):
            loc = locs[slot]
            data = forecast_daily(loc["lat"], loc["lon"], model_id, units, days)
            out.append(format_daily(loc_label(loc), data, model_label, units, days, lang))
        return "\n\n".join(out)

    loc, err = find_location(loc_text, chat_id, lang)
    if err:
        return err
    if days is None:
        data = forecast(loc["lat"], loc["lon"], model_id, units)
        return format_24h(loc_label(loc), data, model_label, units, lang)
    data = forecast_daily(loc["lat"], loc["lon"], model_id, units, days)
    return format_daily(loc_label(loc), data, model_label, units, days, lang)

def cmd_model(args, chat_id):
    lang = get_lang(chat_id)
    current = get_model(chat_id)
    if not args:
        lines = [tr("model_current", lang, m=current) + "\n", tr("model_available", lang)]
        for k in MODELS:
            mark = " \u2705" if k == current else ""
            lines.append(f"<code>{k}</code> \u2014 {model_desc(k, lang)}{mark}")
        lines.append("\n" + tr("model_change", lang))
        return "\n".join(lines)
    key = args[0].lower()
    if key not in MODELS:
        # Accept any raw Open-Meteo model id too (e.g. chmi_aladin_cz_1km), so new
        # models work without a bot update. Looks like an id -> pass it through.
        if re.fullmatch(r"[a-z0-9]+(_[a-z0-9]+)+", key):
            set_model(chat_id, key)
            return tr("model_set_raw", lang, k=key)
        return tr("model_unknown", lang, k=key, opts=", ".join(list(MODELS.keys())[:12]) + ", …")
    set_model(chat_id, key)
    return tr("model_set", lang, k=key, desc=model_desc(key, lang))

def cmd_save(args, chat_id):
    lang = get_lang(chat_id)
    if len(args) < 2 or not args[0].isdigit():
        return tr("save_usage", lang)
    slot = args[0]
    rest = args[1:]
    pc = parse_leading_coords(rest)
    if pc:
        lat, lon, alias_tokens = pc
        alias = " ".join(alias_tokens).strip()
        entry = {"name": alias if alias else f"{lat:.4f},{lon:.4f}",
                 "country": "", "lat": lat, "lon": lon}
    else:
        loc_text = " ".join(rest).strip()
        loc = coords_or_city(loc_text)
        if not loc:
            return tr("city_not_found", lang, city=loc_text)
        entry = {"name": loc["name"], "country": loc.get("country", ""),
                 "lat": loc["lat"], "lon": loc["lon"]}
    def m(state):
        state.setdefault(str(chat_id), {}).setdefault("locations", {})[slot] = entry
    update_state(m)
    return tr("saved_slot", lang, slot=slot, label=loc_label(entry))

def cmd_locs(args, chat_id):
    lang = get_lang(chat_id)
    locations = load_state().get(str(chat_id), {}).get("locations", {})
    if not locations:
        return tr("no_saved_add", lang)
    lines = [tr("saved_list", lang)]
    for slot in sorted(locations, key=int):
        lines.append(f"<b>{slot}</b> \u2014 {loc_label(locations[slot])}")
    return "\n".join(lines)

def cmd_del(args, chat_id):
    lang = get_lang(chat_id)
    if not args or not args[0].isdigit():
        return tr("del_usage", lang)
    slot = args[0]
    removed = [False]
    def m(state):
        locs = state.get(str(chat_id), {}).get("locations", {})
        if slot in locs:
            del locs[slot]
            removed[0] = True
    update_state(m)
    return tr("del_ok", lang, slot=slot) if removed[0] else tr("del_missing", lang, slot=slot)

def cmd_alerts(args, chat_id):
    lang = get_lang(chat_id)
    # `alerts off` / `alerts on` pause or resume the automatic checks for this chat
    # (saved locations are kept). `alerts` with no argument still checks right now.
    if args:
        a0 = args[0].lower()
        if a0 in ("off", "stop", "oprit", "pause"):
            set_alerts_enabled(chat_id, False)
            return tr("alerts_off_set", lang)
        if a0 in ("on", "start", "pornit", "resume"):
            set_alerts_enabled(chat_id, True)
            return tr("alerts_on_set", lang)
        if a0 in ("status", "stare"):
            return tr("alerts_state_on" if get_alerts_enabled(chat_id) else "alerts_state_off", lang)
    cdata = load_state().get(str(chat_id), {})
    locations = cdata.get("locations", {})
    if not locations:
        return tr("no_saved_add", lang)
    model_id, model_label = resolve_model(cdata.get("model", DEFAULT_MODEL))
    thr = get_thresholds(chat_id)
    out = []
    for slot in sorted(locations, key=int):
        loc = locations[slot]
        try:
            data = fetch_alert_forecast(loc["lat"], loc["lon"], model_id)
        except requests.RequestException:
            out.append(f"{loc_label(loc)}: " + tr("fetch_error", lang))
            continue
        trig = evaluate_alerts(data, thr)
        active = {p: v for p, v in trig.items() if p in ALERTS_ENABLED}
        if not active:
            out.append(f"\u2705 {loc_label(loc)}: " + tr("alerts_nothing", lang, h=ALERT_WINDOW_H))
        else:
            block = [f"\u26a0\ufe0f <b>{loc_label(loc)}</b>:"]
            for p, (val, tstr) in active.items():
                block.append("  " + format_alert_line(p, val, tstr, lang))
            out.append("\n".join(block))
    tail = "\n\n" + tr("src_model", lang, model=model_label)
    if not get_alerts_enabled(chat_id):        # manual check still works when paused
        tail += "\n" + tr("alerts_paused_note", lang)
    return "\n\n".join(out) + tail

def cmd_set(args, chat_id):
    lang = get_lang(chat_id)
    thr = get_thresholds(chat_id)
    if not args:
        lines = [tr("thr_current", lang)]
        for p in ("gust", "rain", "snow", "heat", "frost"):
            lines.append(f"<code>{p}</code> {thr[p]:g} {THRESH_UNIT[p]}")
        lines.append("\n" + tr("thr_change", lang))
        return "\n".join(lines)
    param = args[0].lower()
    if param not in DEFAULT_THRESHOLDS:
        return tr("thr_unknown", lang, p=param, opts=", ".join(DEFAULT_THRESHOLDS))
    if len(args) < 2:
        return tr("thr_usage", lang, p=param)
    try:
        value = float(args[1].replace(",", "."))
    except ValueError:
        return tr("thr_nan", lang, p=param)
    set_threshold(chat_id, param, value)
    return tr("thr_set", lang, p=param, v=f"{value:g}", unit=THRESH_UNIT[param])

def cmd_units(args, chat_id):
    lang = get_lang(chat_id)
    u = get_units(chat_id)
    if not args:
        lines = [tr("units_current", lang)]
        for dim in ("temp", "wind", "rain", "pressure"):
            lines.append(f"<code>{dim}</code> {UNIT_LABELS[dim][u[dim]]}")
        lines.append("\n" + tr("units_examples", lang))
        return "\n".join(lines)
    dim = args[0].lower()
    if dim not in DEFAULT_UNITS:
        return tr("units_unknown_q", lang)
    if len(args) < 2:
        return tr("units_usage", lang, dim=dim, opts=", ".join(UNIT_LABELS[dim].keys()))
    val = UNIT_ALIASES[dim].get(args[1].lower())
    if val is None:
        return tr("units_unknown_v", lang, dim=dim, val=args[1], opts=", ".join(UNIT_LABELS[dim].keys()))
    set_unit(chat_id, dim, val)
    return tr("units_set", lang, dim=dim, label=UNIT_LABELS[dim][val])

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
    lang = get_lang(chat_id)
    if not args:
        return tr("soil_usage", lang)
    loc, err = find_location(" ".join(args), chat_id, lang)
    if err:
        return err
    model_id = resolve_model(get_model(chat_id))[0]
    data = fetch_soil(loc["lat"], loc["lon"], model_id)
    h = data.get("hourly", {})
    times = h.get("time", [])
    if not times:
        return tr("soil_nodata", lang)
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
            return "<b>\u2014</b>"
        v = a[idx] * scale
        return f"<b>{v:.{dec}f}{unit}</b>"

    deg = "\u00b0C"   # avoid a backslash inside the f-string braces (Python <3.12)
    lines = [f"\U0001f4cd <b>{loc_label(loc)}</b> \u2014 {tr('soil_title', lang)}",
             tr("soil_time_src", lang, t=times[idx][-5:]) + "\n",
             tr("soil_temp0", lang, v=val('soil_temperature_0cm', 1, deg, 1)),
             tr("soil_airhum", lang, v=val('relative_humidity_2m', 1, '%')),
             tr("soil_moist_h", lang),
             f"  0\u20131cm: {val('soil_moisture_0_to_1cm', 100, '%')}",
             f"  1\u20133cm: {val('soil_moisture_1_to_3cm', 100, '%')}",
             f"  3\u20139cm: {val('soil_moisture_3_to_9cm', 100, '%')}",
             f"  9\u201327cm: {val('soil_moisture_9_to_27cm', 100, '%')}",
             f"  27\u201381cm: {val('soil_moisture_27_to_81cm', 100, '%')}",
             "\n<i>" + tr("soil_legend", lang) + "</i>"]
    return "\n".join(lines)

# --- Air quality (Open-Meteo Air Quality API) ---
# European AQI bands (EEA): 0-20 good, 20-40 fair, 40-60 moderate, 60-80 poor,
# 80-100 very poor, 100+ extremely poor.
def eaqi_band(aqi, lang):
    if aqi is None:
        return ("\u2754", tr("aqi_unknown", lang))
    a = float(aqi)
    if a <= 20:   return ("\U0001f7e2", tr("aqi_good", lang))
    if a <= 40:   return ("\U0001f7e2", tr("aqi_fair", lang))
    if a <= 60:   return ("\U0001f7e1", tr("aqi_moderate", lang))
    if a <= 80:   return ("\U0001f7e0", tr("aqi_poor", lang))
    if a <= 100:  return ("\U0001f534", tr("aqi_vpoor", lang))
    return ("\U0001f7e3", tr("aqi_epoor", lang))

def cmd_air(args, chat_id):
    lang = get_lang(chat_id)
    if not args:
        return tr("air_usage", lang)
    loc, err = find_location(" ".join(args), chat_id, lang)
    if err:
        return err
    params = {
        "latitude": loc["lat"], "longitude": loc["lon"], "timezone": "auto",
        "current": "european_aqi,pm2_5,pm10,nitrogen_dioxide,ozone,"
                   "sulphur_dioxide,carbon_monoxide,uv_index",
    }
    r = requests.get(AQI_URL, params=params, timeout=15)
    r.raise_for_status()
    cur = r.json().get("current", {})
    if not cur:
        return tr("air_nodata", lang)
    emoji, label = eaqi_band(cur.get("european_aqi"), lang)
    aqi = cur.get("european_aqi")
    aqi_s = f"{round(aqi)}" if aqi is not None else "\u2014"

    def v(key, unit, dec=0):
        x = cur.get(key)
        return f"<b>{x:.{dec}f}{unit}</b>" if isinstance(x, (int, float)) else "<b>\u2014</b>"

    t = cur.get("time", "")
    lines = [f"\U0001f4cd <b>{loc_label(loc)}</b> \u2014 {tr('air_title', lang)}",
             tr("air_time_src", lang, t=t[-5:] if len(t) >= 5 else "") + "\n",
             f"{emoji} <b>EAQI {aqi_s}</b> \u2014 {label}",
             tr("air_pm25", lang, v=v("pm2_5", " \u00b5g/m\u00b3", 1)),
             tr("air_pm10", lang, v=v("pm10", " \u00b5g/m\u00b3", 1)),
             tr("air_o3", lang, v=v("ozone", " \u00b5g/m\u00b3")),
             tr("air_no2", lang, v=v("nitrogen_dioxide", " \u00b5g/m\u00b3")),
             tr("air_so2", lang, v=v("sulphur_dioxide", " \u00b5g/m\u00b3")),
             tr("air_co", lang, v=v("carbon_monoxide", " \u00b5g/m\u00b3")),
             tr("air_uv", lang, v=v("uv_index", "", 1)),
             "\n<i>" + tr("air_legend", lang) + "</i>"]
    return "\n".join(lines)

# --- Flood / river discharge (Open-Meteo Flood API, GloFAS) ---
def cmd_flood(args, chat_id):
    lang = get_lang(chat_id)
    if not args:
        return tr("flood_usage", lang)
    toks = list(args)
    days = 7
    if toks and toks[-1].isdigit() and 1 <= int(toks[-1]) <= 30:
        days = int(toks[-1])
        toks = toks[:-1]
    if not toks:
        return tr("flood_usage", lang)
    loc, err = find_location(" ".join(toks), chat_id, lang)
    if err:
        return err
    params = {"latitude": loc["lat"], "longitude": loc["lon"],
              "daily": "river_discharge", "forecast_days": days}
    r = requests.get(FLOOD_URL, params=params, timeout=20)
    r.raise_for_status()
    daily = r.json().get("daily", {})
    times = daily.get("time", [])
    disch = daily.get("river_discharge", [])
    if not times or all(d is None for d in disch):
        return tr("flood_nodata", lang)
    vals = [d for d in disch if isinstance(d, (int, float))]
    peak = max(vals) if vals else 0
    low = min(vals) if vals else 0
    lines = [f"\U0001f30a <b>{loc_label(loc)}</b> — {tr('flood_title', lang)}",
             tr("flood_src", lang) + "\n"]
    for t, d in zip(times, disch):
        ds = f"{d:.1f}" if isinstance(d, (int, float)) else "—"
        mark = " ⚠️" if isinstance(d, (int, float)) and peak > 0 and d >= 0.9 * peak else ""
        lines.append(f"{t[5:]}: <b>{ds}</b> m³/s{mark}")
    # trend over the period + range, for context (flood levels are river-specific)
    if len(vals) >= 2:
        change = vals[-1] - vals[0]
        base = abs(vals[0]) or 1.0
        if change > 0.1 * base:
            trend = tr("flood_rising", lang)
        elif change < -0.1 * base:
            trend = tr("flood_falling", lang)
        else:
            trend = tr("flood_steady", lang)
        lines.append("\n" + tr("flood_summary", lang,
                                trend=trend, lo=f"{low:.1f}", hi=f"{peak:.1f}"))
    lines.append("\n<i>" + tr("flood_legend", lang) + "</i>")
    lines.append(tr("flood_note", lang))
    return "\n".join(lines)

# --- Wind & sea helpers ---
_COMPASS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]

def compass(deg):
    if deg is None:
        return ""
    return _COMPASS[int((float(deg) % 360) / 45.0 + 0.5) % 8]

# --- Wind profile by height ---
# Open-Meteo gives fixed heights above ground (10/80/120/180 m) and, for higher up,
# pressure levels. Their real height varies with weather/terrain, so we read the
# geopotential height and subtract the terrain elevation to get metres above ground.
WIND_LEVELS = (10, 80, 120, 180)
WIND_PLEVELS = (975, 950, 900, 850, 700)     # ≈ 300 m, 600 m, 1 km, 1.5 km, 3 km
UAS_RANGE = (250, 800)                     # default band for `wind <loc> uas`
UAS_STEP = 100

def _uv(speed, deg):
    """Wind speed+direction (FROM, degrees) -> u,v vector components."""
    r = math.radians(float(deg))
    return -speed * math.sin(r), -speed * math.cos(r)

def _from_uv(u, v):
    """u,v -> (speed, direction FROM in degrees)."""
    return math.hypot(u, v), (math.degrees(math.atan2(-u, -v)) % 360.0)

def interp_wind_profile(levels, targets):
    """Interpolate wind at `targets` (m above ground) from known (agl, speed, dir)
    `levels`. Interpolation is done on u/v components — averaging degrees directly
    would be wrong (e.g. 350° and 10°). Returns [(height, speed, dir)]."""
    pts = sorted((h, s, d) for h, s, d in levels if h is not None and s is not None and d is not None)
    if len(pts) < 2:
        return []
    out = []
    for th in targets:
        if th < pts[0][0] or th > pts[-1][0]:
            continue                                  # don't extrapolate beyond data
        lo = max((p for p in pts if p[0] <= th), key=lambda p: p[0])
        hi = min((p for p in pts if p[0] >= th), key=lambda p: p[0])
        if lo[0] == hi[0]:
            sp, dr = lo[1], lo[2]
        else:
            f = (th - lo[0]) / float(hi[0] - lo[0])
            u0, v0 = _uv(lo[1], lo[2])
            u1, v1 = _uv(hi[1], hi[2])
            sp, dr = _from_uv(u0 + (u1 - u0) * f, v0 + (v1 - v0) * f)
        out.append((th, sp, dr))
    return out

def cmd_wind(args, chat_id):
    """Wind speed/gusts/direction now and next hours, at several heights.
    Optional band: `wind Orsova uas` or `wind Orsova 250-800` -> interpolated
    profile every 100 m (UAS / drone flight levels)."""
    lang = get_lang(chat_id)
    if not args:
        return tr("wind_usage", lang)
    toks = list(args)
    band = None
    if toks and toks[-1].lower() in ("uas", "drone", "drona", "dron"):   # 'drone' kept as alias
        band = UAS_RANGE
        toks = toks[:-1]
    else:                                   # trailing "250-800" or "250 800"
        mt = re.match(r"^(\d{2,5})\s*-\s*(\d{2,5})$", toks[-1]) if toks else None
        if mt:
            band = (int(mt.group(1)), int(mt.group(2)))
            toks = toks[:-1]
        elif len(toks) >= 3 and toks[-1].isdigit() and toks[-2].isdigit():
            band = (int(toks[-2]), int(toks[-1]))
            toks = toks[:-2]
    if band:
        lo, hi = min(band), max(band)
        band = (max(0, lo), min(hi, 12000))
    if not toks:
        return tr("wind_usage", lang)
    loc, err = find_location(" ".join(toks), chat_id, lang)
    if err:
        return err
    units = get_units(chat_id)
    model_id, model_label = resolve_model(get_model(chat_id))
    hourly = ",".join([f"wind_speed_{m}m" for m in WIND_LEVELS] +
                      [f"wind_direction_{m}m" for m in WIND_LEVELS] + ["wind_gusts_10m"] +
                      [f"wind_speed_{p}hPa" for p in WIND_PLEVELS] +
                      [f"wind_direction_{p}hPa" for p in WIND_PLEVELS] +
                      [f"geopotential_height_{p}hPa" for p in WIND_PLEVELS])
    params = {"latitude": loc["lat"], "longitude": loc["lon"], "hourly": hourly,
              "forecast_days": 2, "timezone": "auto", "wind_speed_unit": units["wind"]}
    if model_id and model_id != "best_match":
        params["models"] = model_id
    r = requests.get(FC_URL, params=params, timeout=15)
    r.raise_for_status()
    data = r.json()
    h = data.get("hourly", {})
    times = h.get("time", [])
    if not times:
        return tr("wind_nodata", lang)
    off = timedelta(seconds=data.get("utc_offset_seconds", 0))
    now = (datetime.now(timezone.utc) + off).replace(tzinfo=None, minute=0, second=0, microsecond=0)
    idx = 0
    for i, t in enumerate(times):
        if datetime.fromisoformat(t) >= now:
            idx = i
            break
    wlab = UNIT_LABELS["wind"][units["wind"]]

    def at(key, i):
        a = h.get(key)
        return a[i] if a and i < len(a) and a[i] is not None else None

    # Known levels (height above ground -> speed, direction) for interpolation.
    terrain = data.get("elevation")
    known = []
    for mlvl in WIND_LEVELS:
        sp = at(f"wind_speed_{mlvl}m", idx)
        dr = at(f"wind_direction_{mlvl}m", idx)
        if sp is not None and dr is not None:
            known.append((float(mlvl), sp, dr))
    plevels = []                                   # (agl, speed, dir, hPa)
    for p in WIND_PLEVELS:
        sp = at(f"wind_speed_{p}hPa", idx)
        dr = at(f"wind_direction_{p}hPa", idx)
        gh = at(f"geopotential_height_{p}hPa", idx)
        agl = (gh - terrain) if (gh is not None and isinstance(terrain, (int, float))) else None
        if sp is None or (agl is not None and agl < 50):
            continue
        plevels.append((agl, sp, dr, p))
        if agl is not None and dr is not None:
            known.append((float(agl), sp, dr))

    lines = [f"\U0001f4cd <b>{loc_label(loc)}</b> — {tr('wind_title', lang)}",
             tr("src_model", lang, model=model_label) + "\n"]

    if band:                                       # UAS band: every 100 m, interpolated
        lo_b, hi_b = int(band[0]), int(band[1])
        first = ((lo_b + UAS_STEP - 1) // UAS_STEP) * UAS_STEP   # next round 100
        targets = [lo_b] + [x for x in range(first, hi_b + 1, UAS_STEP) if x > lo_b]
        if hi_b not in targets:                    # always include the top of the band
            targets.append(hi_b)
        prof = interp_wind_profile(known, targets)
        lines.append(tr("wind_band_hdr", lang, a=band[0], b=band[1], t=times[idx][-5:]))
        if not prof:
            lines.append("  " + tr("wind_band_none", lang))
        for hgt, sp, dr in prof:
            lines.append(f"  {hgt:>4} m: <b>{sp:.0f} {wlab}</b> {compass(dr)} ({round(dr)}°)")
        g = at("wind_gusts_10m", idx)
        if g is not None:
            lines.append(tr("wind_gust_now", lang, v=f"<b>{g:.0f} {wlab}</b>"))
        lines.append("\n<i>" + tr("wind_band_note", lang) + "</i>")
        return "\n".join(lines)

    lines.append(tr("wind_now_hdr", lang, t=times[idx][-5:]))
    for mlvl in WIND_LEVELS:                       # profile: speed + direction per height
        sp = at(f"wind_speed_{mlvl}m", idx)
        dr = at(f"wind_direction_{mlvl}m", idx)
        sp_s = f"<b>{sp:.0f} {wlab}</b>" if sp is not None else "<b>—</b>"
        dr_s = f" {compass(dr)} ({round(dr)}°)" if dr is not None else ""
        lines.append(f"  {mlvl:>3} m: {sp_s}{dr_s}")
    g = at("wind_gusts_10m", idx)
    if g is not None:
        lines.append(tr("wind_gust_now", lang, v=f"<b>{g:.0f} {wlab}</b>"))

    plines = []
    for agl, sp, dr, p in plevels:
        hlabel = (f"~{agl:,.0f} m".replace(",", " ") if agl is not None else f"{p} hPa")
        dr_s = f" {compass(dr)}" if dr is not None else ""
        plines.append(f"  {hlabel} ({p} hPa): <b>{sp:.0f} {wlab}</b>{dr_s}")
    if plines:
        lines.append("\n" + tr("wind_upper_hdr", lang))
        lines.extend(plines)

    lines.append("\n" + tr("wind_next_hdr", lang))
    for i in range(idx, min(idx + 12, len(times)), 3):   # every 3h, next ~12h
        sp = at("wind_speed_10m", i)
        dr = at("wind_direction_10m", i)
        gg = at("wind_gusts_10m", i)
        sp_s = f"<b>{sp:.0f}</b>" if sp is not None else "<b>—</b>"
        gg_s = f" ({tr('gust', lang)} <b>{gg:.0f}</b>)" if gg is not None else ""
        dr_s = f" {compass(dr)}" if dr is not None else ""
        lines.append(f"  {times[i][-5:]}: {sp_s} {wlab}{dr_s}{gg_s}")
    lines.append("\n<i>" + tr("wind_legend", lang) + "</i>")
    return "\n".join(lines)

def cmd_marine(args, chat_id):
    lang = get_lang(chat_id)
    if not args:
        return tr("marine_usage", lang)
    loc, err = find_location(" ".join(args), chat_id, lang)
    if err:
        return err
    params = {
        "latitude": loc["lat"], "longitude": loc["lon"], "timezone": "auto",
        "current": "wave_height,wave_direction,wave_period,wind_wave_height,"
                   "swell_wave_height,swell_wave_period,sea_surface_temperature",
    }
    try:
        r = requests.get(MARINE_URL, params=params, timeout=15)
        r.raise_for_status()
        cur = r.json().get("current", {})
    except (requests.RequestException, ValueError):
        return tr("marine_nodata", lang)
    # Inland points return all-null -> tell the user it's not a sea location.
    keys = ("wave_height", "wind_wave_height", "swell_wave_height", "sea_surface_temperature")
    if not cur or all(cur.get(k) is None for k in keys):
        return tr("marine_nodata", lang)

    def num(key, unit, dec=1):
        x = cur.get(key)
        return f"<b>{x:.{dec}f}{unit}</b>" if isinstance(x, (int, float)) else "<b>—</b>"

    wdir = cur.get("wave_direction")
    wdir_s = f" <b>{compass(wdir)}</b> ({round(wdir)}°)" if wdir is not None else ""
    t = cur.get("time", "")
    lines = [f"\U0001f30a <b>{loc_label(loc)}</b> — {tr('marine_title', lang)}",
             tr("marine_time_src", lang, t=t[-5:] if len(t) >= 5 else "") + "\n",
             tr("marine_wave", lang, v=num("wave_height", " m")) + wdir_s,
             tr("marine_period", lang, v=num("wave_period", " s", 0)),
             tr("marine_swell", lang, v=num("swell_wave_height", " m"),
                p=num("swell_wave_period", " s", 0)),
             tr("marine_windwave", lang, v=num("wind_wave_height", " m")),
             tr("marine_sst", lang, v=num("sea_surface_temperature", "°C")),
             "\n<i>" + tr("marine_legend", lang) + "</i>"]
    return "\n".join(lines)

# --- River water levels ("cote") — AFDJ (Romanian Danube) + PegelOnline (Germany) ---
def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))

def _xml_field(block, tag):
    m = re.search(rf"<{tag}>\s*<value>([^<]*)</value>", block)
    return m.group(1).strip() if m else None

def afdj_levels():
    """Fetch the AFDJ Lower-Danube gauges (afdj.ro). Each gauge is an
    <item key="N"> block that already carries its coordinates and Danube km,
    so no hard-coded station list is needed. Returns a list of dicts."""
    try:
        r = requests.get(RIVER_XML_URL, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        xml = r.text
    except (requests.RequestException, ValueError):
        return []
    out = []
    for block in re.split(r"<item\b[^>]*>", xml)[1:]:
        name = _xml_field(block, "field_localitatea")
        if not name:
            m = re.search(r"<title><value>([^<]*)</value>", block)
            name = m.group(1).strip() if m else None
        if not name:
            continue
        def num(tag):
            v = _xml_field(block, tag)
            try:
                return float(v)
            except (TypeError, ValueError):
                return None
        geo = re.search(r"<field_geolocation_demo_single>.*?<lat>([-\d.]+)</lat>"
                        r"<lng>([-\d.]+)</lng>", block, re.S)
        lat = float(geo.group(1)) if geo else None
        lon = float(geo.group(2)) if geo else None
        out.append({"name": name, "lat": lat, "lon": lon,
                    "km": num("field_km"), "cota": num("field_cota"),
                    "var": num("field_variatia"),
                    "temp": num("field_temperatura_masurata"),
                    "t48": num("field_tendinta_48h")})
    return out

def _river_from_afdj(lat, lon, lang):
    """Nearest Lower-Danube gauge to the point, using the feed's own coordinates."""
    best, bestd = None, 1e9
    for st in afdj_levels():
        if st["lat"] is None or st["cota"] is None:
            continue
        d = haversine_km(lat, lon, st["lat"], st["lon"])
        if d < bestd:
            best, bestd = st, d
    if not best or bestd > RIVER_MAX_KM:
        return None
    var = best["var"]
    trend = ("\u2192" if not var else ("\u2197" if var > 0 else "\u2198"))
    lines = [f"\U0001f30a <b>{best['name']}</b> \u2014 {tr('river_title', lang)}",
             tr("river_src_afdj", lang) + "\n",
             tr("river_level", lang, v=f"<b>{best['cota']:.0f} cm</b>",
                d=f"{var:+.0f}" if var is not None else "\u2014", tr=trend)]
    if best.get("temp") is not None:
        lines.append(tr("river_temp", lang, v=f"<b>{best['temp']:.1f}\u00b0C</b>"))
    if best.get("t48"):        # 0 is the feed's "no forecast" sentinel
        lines.append(tr("river_forecast48", lang, v=f"<b>{best['t48']:.0f} cm</b>"))
    if best.get("km") is not None:
        lines.append(tr("river_km", lang, km=f"{best['km']:g}"))
    lines.append(tr("river_near", lang, d=f"{bestd:.0f}"))
    lines.append("\n<i>" + tr("river_note", lang) + "</i>")
    return "\n".join(lines)

def _river_from_pegel(lat, lon, lang):
    try:
        r = requests.get(PEGEL_URL, params={"latitude": lat, "longitude": lon,
                         "radius": RIVER_MAX_KM, "includeCurrentMeasurement": "true"},
                         timeout=15, headers=_TILE_UA)
        r.raise_for_status()
        stations = r.json()
    except (requests.RequestException, ValueError):
        return None
    best, bestd, bestw = None, 1e9, None
    for s in stations if isinstance(stations, list) else []:
        wl = next((ts for ts in s.get("timeseries", [])
                   if ts.get("shortname") == "W" and ts.get("currentMeasurement")), None)
        if not wl or not isinstance(s.get("latitude"), (int, float)):
            continue
        d = haversine_km(lat, lon, s["latitude"], s["longitude"])
        if d < bestd:
            best, bestd, bestw = s, d, wl
    if not best or bestd > RIVER_MAX_KM:
        return None
    cm = bestw["currentMeasurement"].get("value")
    st = bestw["currentMeasurement"].get("stateMnwMhw", "")
    water = (best.get("water") or {}).get("longname", "")
    lines = [f"\U0001f30a <b>{best.get('longname', best.get('shortname', ''))}</b> ({water}) — {tr('river_title', lang)}",
             tr("river_src_pegel", lang) + "\n",
             tr("river_level_p", lang, v=f"<b>{cm:.0f} cm</b>", st=st or "—"),
             tr("river_near", lang, d=f"{bestd:.0f}"),
             "\n<i>" + tr("river_note", lang) + "</i>"]
    return "\n".join(lines)

def _js_json_var(html_txt, varname):
    """Pull `var <name> = JSON.parse('...')` embedded in the page and decode it."""
    key = "var " + varname + " = JSON.parse('"
    i = html_txt.find(key)
    if i < 0:
        return None
    i += len(key)
    j = html_txt.find("')", i)
    if j < 0:
        return None
    try:
        return json.loads(html_txt[i:j].replace("\\'", "'"))
    except (ValueError, TypeError):
        return None

def inhga_map_stations():
    """~750 INHGA river & Danube gauges with live level, read from the /prognoze/
    page (data is inlined as JS vars, not a separate API). Cached ~30 min."""
    now = time.time()
    if _INHGA_MAP_CACHE["data"] is not None and now - _INHGA_MAP_CACHE["t"] < 1800:
        return _INHGA_MAP_CACHE["data"]
    try:
        r = requests.get(INHGA_MAP_URL, timeout=25, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        r.encoding = "utf-8"
        html_txt = r.text
    except (requests.RequestException, ValueError):
        return _INHGA_MAP_CACHE["data"] or []
    stations = _js_json_var(html_txt, "stations")      # [{title,lat,lng,idomm}]
    values = _js_json_var(html_txt, "station_data")    # {code: {TEXT1,TEXT2,TEXT3,...}}
    if not stations or not values:
        return _INHGA_MAP_CACHE["data"] or []
    out = []
    for s in stations:
        d = values.get(str(s.get("idomm")))
        if not d:
            continue
        hm = re.search(r"H=\s*(-?\d+)", d.get("TEXT1") or "")
        if not hm:
            continue
        vm = re.search(r"nivel\s*=\s*(-?\d+)", d.get("TEXT2") or "")
        rv = re.search(r"\bR\.\s*(.+)$", d.get("DENUMIRE_STATIE") or "")
        out.append({"name": s.get("title", ""), "lat": s.get("lat"), "lon": s.get("lng"),
                    "river": rv.group(1).strip().title() if rv else "",
                    "h": int(hm.group(1)), "var": int(vm.group(1)) if vm else None,
                    "status": (d.get("TEXT3") or "").strip(),
                    "dep": d.get("COD_DEP_COTE", 0) or 0, "data": (d.get("DATA") or "")[:16]})
    _INHGA_MAP_CACHE["t"] = now
    _INHGA_MAP_CACHE["data"] = out
    return out

_DEP_EMOJI = {0: "\U0001f7e2", 1: "\U0001f7e1", 2: "\U0001f7e0", 3: "\U0001f534"}

def _river_from_inhga_map(lat, lon, lang):
    """Nearest INHGA river/Danube gauge (cm) to the point, from the /prognoze/ map."""
    best, bestd = None, 1e9
    for st in inhga_map_stations():
        if st["lat"] is None:
            continue
        d = haversine_km(lat, lon, st["lat"], st["lon"])
        if d < bestd:
            best, bestd = st, d
    if not best or bestd > RIVER_MAX_KM:
        return None
    var = best["var"]
    arrow = ("\u2192" if not var else ("\u2197" if var > 0 else "\u2198"))
    title = best["name"] + (f" ({best['river']})" if best["river"] else "")
    emoji = _DEP_EMOJI.get(best["dep"], "\U0001f7e2")
    lines = [f"\U0001f30a <b>{title}</b> \u2014 {tr('river_title', lang)}",
             tr("river_inhga_src", lang) + "\n",
             f"{emoji} " + tr("river_h", lang, v=f"<b>{best['h']} cm</b>",
                d=f"{var:+d}" if var is not None else "\u2014", tr=arrow)]
    if best["status"]:
        lines.append(best["status"])
    if best["data"]:
        lines.append(tr("river_updated", lang, t=best["data"]))
    lines.append(tr("river_near", lang, d=f"{bestd:.0f}"))
    lines.append("\n<i>" + tr("river_inhga_note", lang) + "</i>")
    return "\n".join(lines)

def _in_romania(lat, lon):
    s, w, n, e = RO_BBOX
    return s <= lat <= n and w <= lon <= e

def _strip_tags(html_txt):
    t = re.sub(r"(?is)<script.*?</script>", " ", html_txt)
    t = re.sub(r"(?is)<style.*?</style>", " ", t)
    t = re.sub(r"(?s)<[^>]+>", " ", t)
    return html.unescape(re.sub(r"[ \t]+", " ", t))

def inhga_river_bulletin():
    """Latest INHGA national river forecast (hidro.ro). Returns (title, text) or None.
    The bulletin is a prose forecast; INHGA has no open per-station cm feed."""
    try:
        r = requests.get(INHGA_RIVERS_URL, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        r.encoding = "utf-8"           # hidro.ro sends no charset -> force UTF-8
        idx = r.text
    except (requests.RequestException, ValueError):
        return None
    m = re.search(r'href="(https://www\.hidro\.ro/bulletin/'
                  r'prognoza-hidrologica-pentru-rauri[^"]*)"', idx)
    if not m:
        return None
    try:
        rr = requests.get(m.group(1), timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        rr.raise_for_status()
        rr.encoding = "utf-8"
        page = rr.text
    except (requests.RequestException, ValueError):
        return None
    tm = re.search(r"<title>([^<]*)</title>", page)
    title = tm.group(1).split(" \u2013 ")[0].strip() if tm else ""
    text = _strip_tags(page)
    fm = re.search(r"(Debitele.*?COTEL[EA] DE [\u0102\u0202A-Z\u021a\u0218\u00ce ]+\.)", text, re.S)
    body = re.sub(r"\s+", " ", fm.group(1)).strip() if fm else None
    if not body:
        dm = re.search(r'property="og:description" content="([^"]*)"', page)
        body = html.unescape(dm.group(1)).strip() if dm else None
    return (title, body) if body else None

def _river_from_inhga(lat, lon, lang):
    """National river forecast for points in Romania (no per-gauge cm available)."""
    b = inhga_river_bulletin()
    if not b:
        return None
    title, body = b
    lines = [f"\U0001f30a <b>{tr('river_ro_title', lang)}</b>",
             tr("river_ro_src", lang) + "\n"]
    if title:
        lines.append(f"<i>{title}</i>\n")
    lines.append(body)
    lines.append("\n<i>" + tr("river_ro_note", lang) + "</i>")
    return "\n".join(lines)

def cmd_cote(args, chat_id):
    lang = get_lang(chat_id)
    if not args:
        # default to saved slot 1 if present
        locs = load_state().get(str(chat_id), {}).get("locations", {})
        if "1" in locs:
            loc = locs["1"]
        else:
            return tr("river_usage", lang)
    else:
        loc, err = find_location(" ".join(args), chat_id, lang)
        if err:
            return err
    res = _river_from_afdj(loc["lat"], loc["lon"], lang)   # Romanian Danube first
    if res is None:
        res = _river_from_pegel(loc["lat"], loc["lon"], lang)   # German waterways
    if res is None:
        country = (loc.get("country") or "").lower()
        ro = (("romania" in country or "moldova" in country) if country
              else _in_romania(loc["lat"], loc["lon"]))
        if ro:                                                 # Romanian rivers & Danube (INHGA)
            res = _river_from_inhga_map(loc["lat"], loc["lon"], lang)   # per-gauge cm
            if res is None:
                res = _river_from_inhga(loc["lat"], loc["lon"], lang)   # national bulletin
    return res if res is not None else tr("river_none", lang)

def cmd_hist(args, chat_id):
    lang = get_lang(chat_id)
    if len(args) < 3:
        return tr("hist_usage", lang)
    start, end = args[-2], args[-1]
    datep = r"^\d{4}-\d{2}-\d{2}$"
    if not re.match(datep, start) or not re.match(datep, end):
        return tr("hist_baddate", lang)
    loc_text = " ".join(args[:-2]).strip()
    if not loc_text:
        return tr("hist_needloc", lang)
    loc, err = find_location(loc_text, chat_id, lang)
    if err:
        return err
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
        return tr("hist_nodata", lang)
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
    lines = [f"\U0001f4cd <b>{loc_label(loc)}</b> \u2014 {tr('hist_title', lang, a=start, b=end)}",
             tr("hist_src", lang) + "\n",
             tr("hist_days", lang, n=len(times)),
             tr("hist_maxavg", lang, a=num(avg(tmax)), b=num(g(max, tmax)), u=tlab),
             tr("hist_minavg", lang, a=num(avg(tmin)), b=num(g(min, tmin)), u=tlab),
             tr("hist_precip", lang, v=f"{tot_p:g}", u=rlab),
             tr("hist_gust", lang, v=num(g(max, gmax), 0), u=wlab)]
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

HELP = {
    "en": (
        "<b>Personal weather bot</b> \U0001f324\ufe0f\n"
        "Data source: Open-Meteo (free).\n\n"
        "<b>Forecast</b>\n"
        "Just type a place name:\n"
        "<code>Orsova</code> \u2014 24-hour hourly forecast\n"
        "<code>44.816,29.879</code> \u2014 by coordinates\n"
        "<code>Orsova 3</code> \u2014 3-day forecast (up to 16)\n"
        "<code>3</code> \u2014 3-day forecast for all saved locations\n\n"
        "<b>Soil, air &amp; history</b>\n"
        "<code>soil Orsova</code> \u2014 soil moisture + temperature now\n"
        "<code>air Orsova</code> \u2014 air quality (European AQI + pollutants)\n"
        "<code>flood Orsova</code> \u2014 river discharge forecast (GloFAS)\n"
        "<code>cote Orsova</code> \u2014 Danube/river water level (AFDJ / PegelOnline)\n"
        "<code>wind Orsova</code> \u2014 wind speed &amp; direction by height (10 m \u2026 ~3 km)\n"
        "<code>wind Orsova uas</code> \u2014 250\u2013800 m every 100 m (or <code>wind Orsova 300-600</code>)\n"
        "<code>marine Constanta</code> \u2014 sea state: waves, swell, water temp\n"
        "<code>hist Orsova 2025-07-01 2025-07-10</code> \u2014 past weather for a period\n\n"
        "<b>Maps</b>\n"
        "<code>radar</code> \u2014 national radar (ANM) over a faded map\n"
        "<code>sat Orsova</code> \u2014 cloud cover\n"
        "<code>sat Orsova low</code> \u2014 low clouds only (key for UAS flights)\n"
        "<code>heat Orsova</code> \u2014 temperature map (colour scale on the image)\n"
        "<code>hum Orsova</code> \u2014 relative humidity map\n"
        "<code>sat Orsova low +6h</code> \u2014 any map as forecast, up to +72h\n"
        "<code>thm dark</code> / <code>thm light</code> \u2014 dark or light basemap\n"
        "<code>map Orsova</code> \u2014 clouds + radar\n"
        "<code>mapset</code> \u2014 map settings (radar source, dim, cloud colour/opacity, zoom)\n"
        "<code>mapset tz Europe/Bucharest</code> \u2014 local time on maps (or <code>+3</code> / <code>auto</code>)\n\n"
        "<b>Model</b>\n"
        "<code>model</code> \u2014 show the current model and the list of models\n"
        "<code>model iconeu</code> \u2014 set the default model (name from the list)\n\n"
        "<b>Saved locations</b>\n"
        "<code>save 1 Orsova</code> \u2014 save a location in slot 1\n"
        "<code>locs</code> \u2014 list your saved locations\n"
        "<code>del 1</code> \u2014 delete the location in slot 1\n"
        "You can type part of a saved name: <code>clad</code> \u2192 Cladova\n\n"
        "<b>Alerts</b>\n"
        "<code>alerts</code> \u2014 check saved locations now and report\n"
        "<code>alerts off</code> / <code>alerts on</code> \u2014 pause or resume automatic alerts\n"
        "<code>set</code> \u2014 show current alert thresholds\n"
        "<code>set gust 70</code> \u2014 change a threshold "
        "(gust km/h, rain mm/h, snow cm/h, heat \u00b0C, frost \u00b0C)\n"
        "<code>anm</code> \u2014 official ANM warnings: <code>anm nowcasting,general</code> / <code>anm off</code>\n"
        "<code>alarm 1 21:05</code> \u2014 daily 24h forecast for slot 1 at 21:05 (<code>alarm off</code>)\n"
        "<code>interval 10</code> \u2014 how often alerts are checked, in minutes\n"
        "<code>status</code> \u2014 service health (bot core, WhatsApp link, settings)\n"
        "<code>restart &lt;system password&gt;</code> \u2014 restart the services\n\n"
        "<b>Units &amp; language</b>\n"
        "<code>units</code> \u2014 show current display units\n"
        "<code>units temp F</code> \u2014 set units (temp C/F, wind kmh/ms/mph/kn, "
        "rain mm/inch, pressure hpa/mmhg/inhg)\n"
        "<code>lang ro</code> / <code>lang en</code> \u2014 change language\n\n"
        "Saved locations are watched automatically: you get a message when strong "
        "wind, rain, snow, heat or frost is expected in the next hours.\n"
        "Official ANM warnings for your exact point are included too.\n\n"
        "Type <code>help</code> to see this list again."
    ),
    "ro": (
        "<b>Bot meteo personal</b> \U0001f324\ufe0f\n"
        "Sursa datelor: Open-Meteo (gratuit).\n\n"
        "<b>Prognoza</b>\n"
        "Scrie direct o localitate:\n"
        "<code>Orsova</code> \u2014 prognoza orara pe 24h\n"
        "<code>44.816,29.879</code> \u2014 dupa coordonate\n"
        "<code>Orsova 3</code> \u2014 prognoza pe 3 zile (pana la 16)\n"
        "<code>3</code> \u2014 prognoza pe 3 zile pentru toate locatiile salvate\n\n"
        "<b>Sol, aer &amp; istoric</b>\n"
        "<code>soil Orsova</code> \u2014 umiditatea solului + temperatura acum\n"
        "<code>air Orsova</code> \u2014 calitatea aerului (AQI european + poluanti)\n"
        "<code>flood Orsova</code> \u2014 prognoza debit rau (GloFAS)\n"
        "<code>cote Orsova</code> \u2014 cota apei Dunarii/raurilor (AFDJ / PegelOnline)\n"
        "<code>wind Orsova</code> \u2014 viteza si directia vantului pe inaltimi (10 m \u2026 ~3 km)\n"
        "<code>wind Orsova uas</code> \u2014 250\u2013800 m din 100 in 100 (sau <code>wind Orsova 300-600</code>)\n"
        "<code>marine Constanta</code> \u2014 starea marii: valuri, hula, temp apa\n"
        "<code>hist Orsova 2025-07-01 2025-07-10</code> \u2014 vremea din trecut pe o perioada\n\n"
        "<b>Harti</b>\n"
        "<code>radar</code> \u2014 radar national (ANM) peste harta estompata\n"
        "<code>sat Orsova</code> \u2014 acoperire cu nori\n"
        "<code>sat Orsova low</code> \u2014 doar norii jos (esential pentru zboruri UAS)\n"
        "<code>heat Orsova</code> \u2014 harta temperaturilor (scara de culori pe imagine)\n"
        "<code>hum Orsova</code> \u2014 harta umiditatii relative\n"
        "<code>sat Orsova low +6h</code> \u2014 orice harta ca prognoza, pana la +72h\n"
        "<code>thm dark</code> / <code>thm light</code> \u2014 fundal inchis sau luminos\n"
        "<code>map Orsova</code> \u2014 nori + radar\n"
        "<code>mapset</code> \u2014 setari harta (sursa radar, estompare, culoare/opacitate nori, zoom)\n"
        "<code>mapset tz Europe/Bucharest</code> \u2014 ora locala pe harti (sau <code>+3</code> / <code>auto</code>)\n\n"
        "<b>Model</b>\n"
        "<code>model</code> \u2014 arata modelul curent si lista de modele\n"
        "<code>model iconeu</code> \u2014 seteaza modelul implicit (nume din lista)\n\n"
        "<b>Locatii salvate</b>\n"
        "<code>save 1 Orsova</code> \u2014 salveaza o locatie in slotul 1\n"
        "<code>locs</code> \u2014 listeaza locatiile salvate\n"
        "<code>del 1</code> \u2014 sterge locatia din slotul 1\n"
        "Poti scrie o parte din nume: <code>clad</code> \u2192 Cladova\n\n"
        "<b>Alerte</b>\n"
        "<code>alerts</code> \u2014 verifica acum locatiile salvate\n"
        "<code>alerts off</code> / <code>alerts on</code> \u2014 opreste sau reporneste alertele automate\n"
        "<code>set</code> \u2014 arata pragurile de alerta curente\n"
        "<code>set gust 70</code> \u2014 schimba un prag "
        "(gust km/h, rain mm/h, snow cm/h, heat \u00b0C, frost \u00b0C)\n"
        "<code>anm</code> \u2014 avertizari oficiale ANM: <code>anm nowcasting,general</code> / <code>anm off</code>\n"
        "<code>alarm 1 21:05</code> \u2014 prognoza zilnica 24h pentru slotul 1 la 21:05 (<code>alarm off</code>)\n"
        "<code>interval 10</code> \u2014 cat de des se verifica alertele, in minute\n"
        "<code>status</code> \u2014 starea serviciilor (nucleu bot, WhatsApp, setari)\n"
        "<code>restart &lt;parola de sistem&gt;</code> \u2014 reporneste serviciile\n\n"
        "<b>Unitati &amp; limba</b>\n"
        "<code>units</code> \u2014 arata unitatile de afisare curente\n"
        "<code>units temp F</code> \u2014 seteaza unitatile (temp C/F, wind kmh/ms/mph/kn, "
        "rain mm/inch, pressure hpa/mmhg/inhg)\n"
        "<code>lang ro</code> / <code>lang en</code> \u2014 schimba limba\n\n"
        "Locatiile salvate sunt monitorizate automat: primesti mesaj cand se asteapta "
        "vant puternic, ploaie, ninsoare, canicula sau inghet in urmatoarele ore.\n"
        "Se includ si avertizarile oficiale ANM pentru punctul tau exact.\n\n"
        "Scrie <code>help</code> ca sa revezi lista."
    ),
}

def cmd_start(args, chat_id):
    return HELP.get(get_lang(chat_id), HELP["en"])

def cmd_lang(args, chat_id):
    lang = get_lang(chat_id)
    if not args:
        return tr("lang_current", lang, l=lang)
    new = args[0].lower()
    if new not in SUPPORTED_LANGS:
        return tr("lang_unknown", lang)
    set_lang(chat_id, new)
    return tr("lang_set", new, l=new)

def cmd_anm(args, chat_id):
    lang = get_lang(chat_id)
    if not args:
        cur = get_anm_feeds(chat_id)
        val = ", ".join(cur) if cur else tr("anm_off_word", lang)
        return tr("anm_current", lang, feeds=val)
    tokens = [t for t in re.split(r"[,\s]+", ",".join(args).lower()) if t]
    if len(tokens) == 1 and tokens[0] in ("off", "none", "stop", "oprit"):
        set_anm_feeds(chat_id, [])
        return tr("anm_set_off", lang)
    if len(tokens) == 1 and tokens[0] in ("both", "all", "on", "toate"):
        feeds = list(ANM_FEEDS.keys())
    else:
        feeds = []
        for tk in tokens:
            f = ANM_FEED_ALIASES.get(tk)
            if not f:
                return tr("anm_unknown", lang, f=tk)
            if f not in feeds:
                feeds.append(f)
    set_anm_feeds(chat_id, feeds)
    return tr("anm_set", lang, feeds=", ".join(feeds))

MAP_MAX_AHEAD_H = 72          # forecast maps: up to 3 days ahead
_AHEAD_RE = re.compile(r"^\+?(\d{1,3})\s*(h|hr|hours?|ore?|d|z|zile?|days?)$", re.I)

def _parse_ahead(tok):
    """'+6h' / '12h' / '+2d' / '3zile' -> hours ahead (capped), or None."""
    mt = _AHEAD_RE.match(tok.strip())
    if not mt:
        return None
    n = int(mt.group(1))
    unit = mt.group(2).lower()
    hours = n * 24 if unit.startswith(("d", "z")) else n
    return max(0, min(hours, MAP_MAX_AHEAD_H))

def _map_cmd(args, chat_id, layers, label_key, src_key="map_src", legend_key=None,
             cloud_var="cloud_cover"):
    lang = get_lang(chat_id)
    if not _PIL:
        return tr("map_nopil", lang)
    if not args:
        return tr("wx_usage_short", lang)
    toks = list(args)
    ahead = None
    if len(toks) > 1:                       # optional trailing "+6h" / "+2d"
        got = _parse_ahead(toks[-1])
        if got is not None:
            ahead = got
            toks = toks[:-1]
    if not toks:
        return tr("wx_usage_short", lang)
    when = None
    if ahead:
        when = (datetime.now(timezone.utc) + timedelta(hours=ahead)).replace(
            minute=0, second=0, microsecond=0)
    loc, err = find_location(" ".join(toks), chat_id, lang)
    if err:
        return err
    cfg = get_map_cfg(chat_id)
    if "humidity" in layers:
        bar = {"title": tr("hum_bar_title", lang)}
    elif "heat" in layers:
        tunit = get_units(chat_id)["temp"]            # 'C' or 'F'
        bar = {"title": tr("heat_bar_title", lang, u=UNIT_LABELS["temp"][tunit]),
               "unit": tunit}
    elif "radar" in layers:
        bar = {"title": tr("rv_bar_title", lang), "left": tr("rv_bar_left", lang),
               "right": tr("rv_bar_right", lang)}
    else:
        bar = None
    try:
        png, tlabel = build_map(loc["lat"], loc["lon"], layers, z=cfg["zoom"],
                                base_dim=cfg["base_dim"], cloud_rgb=tuple(cfg["cloud_rgb"]),
                                cloud_alpha=cfg["cloud_alpha"], tz=cfg.get("tz", ""),
                                legend=bar, cloud_var=cloud_var, when=when,
                                theme=cfg.get("theme", MAP_THEME_DEFAULT),
                                model_id=resolve_model(get_model(chat_id))[0])
    except Exception as e:
        return tr("err_generic", lang, e=e)
    if not png:
        return tr("map_nodata", lang)
    cap = f"\U0001f4cd <b>{loc_label(loc)}</b> \u2014 {tr(label_key, lang)}"
    if ahead:
        cap += " \u00b7 " + tr("map_forecast_in", lang, h=ahead)
    cap += ("\n" + (f"{tlabel} \u00b7 " if tlabel else "") + tr(src_key, lang))
    if legend_key:
        cap += "\n\n" + tr(legend_key, lang)
    return Photo(png, cap)

def cmd_radar(args, chat_id):
    lang = get_lang(chat_id)
    if not _PIL:
        return tr("map_nopil", lang)
    cfg = get_map_cfg(chat_id)
    if cfg["radar_src"] == "rainviewer":
        # RainViewer radar over a faded OSM base, centered on the given location.
        return _map_cmd(args, chat_id, ["radar"], "cap_radar",
                        src_key="map_src_radar", legend_key="radar_legend")
    # ANM national radar (in-country radars); optional location just drops a marker.
    mlat = mlon = None
    if args:
        loc, err = find_location(" ".join(args), chat_id, lang)
        if err:
            return err
        mlat, mlon = loc["lat"], loc["lon"]
    try:
        png, tlabel = build_anm_radar_map(mlat, mlon, base_dim=cfg["base_dim"],
                                          tz=cfg.get("tz", ""))
    except Exception as e:
        return tr("err_generic", lang, e=e)
    if not png:
        return tr("map_nodata", lang)
    cap = f"\U0001f4e1 <b>{tr('cap_radar', lang)}</b>"
    cap += "\n" + (f"{tlabel} · " if tlabel else "") + tr("map_src_anm", lang)
    # ANM's official scale is drawn on the image itself; explain what it means.
    cap += "\n\n" + tr("radar_legend_anm" if ANM_SHOW_SCALE else "radar_legend", lang)
    return Photo(png, cap)

CLOUD_VARS = {"low": ("cloud_cover_low", "cap_sat_low"),
              "mid": ("cloud_cover_mid", "cap_sat_mid"),
              "high": ("cloud_cover_high", "cap_sat_high"),
              "jos": ("cloud_cover_low", "cap_sat_low"),
              "mijloc": ("cloud_cover_mid", "cap_sat_mid"),
              "sus": ("cloud_cover_high", "cap_sat_high")}

def cmd_sat(args, chat_id):
    # Cloud cover from Open-Meteo (RainViewer's free tier has no satellite).
    # `sat <loc> low` -> low clouds only (the layer that matters for UAS flights).
    toks = list(args)
    var, label = "cloud_cover", "cap_sat"
    if len(toks) > 1 and toks[-1].lower() in CLOUD_VARS:
        var, label = CLOUD_VARS[toks[-1].lower()]
        toks = toks[:-1]
    elif len(toks) > 2 and toks[-2].lower() in CLOUD_VARS:   # "<loc> low +6h"
        var, label = CLOUD_VARS[toks[-2].lower()]
        toks = toks[:-2] + toks[-1:]
    return _map_cmd(toks, chat_id, ["clouds"], label,
                    src_key="map_src_clouds", cloud_var=var)

def cmd_hum(args, chat_id):
    # Relative humidity at 2 m, Open-Meteo's official 'relative' palette.
    return _map_cmd(args, chat_id, ["humidity"], "cap_hum",
                    src_key="map_src_clouds", legend_key="hum_legend")

def cmd_heat(args, chat_id):
    # Temperature field sampled from Open-Meteo and coloured on the map.
    return _map_cmd(args, chat_id, ["heat"], "cap_heat",
                    src_key="map_src_clouds", legend_key="heat_legend")

def cmd_map(args, chat_id):
    return _map_cmd(args, chat_id, ["clouds", "radar"], "cap_map",
                    src_key="map_src_both", legend_key="radar_legend")

def cmd_theme(args, chat_id):
    """Shorthand for `mapset theme dark|light`."""
    return cmd_mapset(["theme"] + list(args), chat_id)

def cmd_mapset(args, chat_id):
    """User-editable map settings: radar source, base fade, cloud colour/opacity, zoom."""
    lang = get_lang(chat_id)
    cfg = get_map_cfg(chat_id)
    if not args:
        return tr("mapset_current", lang,
                  src=cfg["radar_src"], dim=f"{cfg['base_dim']:g}",
                  alpha=cfg["cloud_alpha"], rgb=",".join(map(str, cfg["cloud_rgb"])),
                  zoom=cfg["zoom"], tz=(cfg.get("tz") or "auto (server)"),
                  theme=cfg.get("theme", MAP_THEME_DEFAULT))
    key = args[0].lower()
    rest = args[1:]
    val = rest[0] if rest else ""
    val_all = " ".join(rest)
    if key in ("radar", "source", "src"):
        v = val.lower()
        if v == "anm":
            src = "anm"
        elif v in ("rainviewer", "rain", "rv"):
            src = "rainviewer"
        else:
            return tr("mapset_radar_usage", lang)
        set_map_cfg(chat_id, "radar_src", src)
        return tr("mapset_set", lang, k="radar", v=src)
    if key in ("dim", "base"):
        try:
            f = float(val.replace(",", ".")); assert 0.0 <= f <= 1.0
        except (ValueError, AssertionError):
            return tr("mapset_dim_usage", lang)
        set_map_cfg(chat_id, "base_dim", f)
        return tr("mapset_set", lang, k="dim", v=f"{f:g}")
    if key in ("alpha", "cloudalpha", "opacity"):
        try:
            a = int(val); assert 0 <= a <= 255
        except (ValueError, AssertionError):
            return tr("mapset_alpha_usage", lang)
        set_map_cfg(chat_id, "cloud_alpha", a)
        return tr("mapset_set", lang, k="alpha", v=a)
    if key in ("cloud", "rgb", "cloudrgb", "color", "colour"):
        parts = [p for p in re.split(r"[,\s]+", val_all) if p]
        try:
            rgb = [int(p) for p in parts]
            assert len(rgb) == 3 and all(0 <= x <= 255 for x in rgb)
        except (ValueError, AssertionError):
            return tr("mapset_rgb_usage", lang)
        set_map_cfg(chat_id, "cloud_rgb", rgb)
        return tr("mapset_set", lang, k="cloud", v=",".join(map(str, rgb)))
    if key in ("zoom", "z"):
        try:
            zz = int(val); assert 3 <= zz <= 7
        except (ValueError, AssertionError):
            return tr("mapset_zoom_usage", lang)
        set_map_cfg(chat_id, "zoom", zz)
        return tr("mapset_set", lang, k="zoom", v=zz)
    if key in ("theme", "thm", "tema"):
        v = val.lower()
        if v in ("dark", "night", "intunecat", "negru"):
            set_map_cfg(chat_id, "theme", "dark")
            return tr("mapset_set", lang, k="theme", v="dark")
        if v in ("light", "day", "luminos", "alb"):
            set_map_cfg(chat_id, "theme", "light")
            return tr("mapset_set", lang, k="theme", v="light")
        return tr("mapset_theme_usage", lang)
    if key in ("tz", "timezone", "fus"):
        v = val.strip()
        if v.lower() in ("auto", "server", "local", "reset", ""):
            set_map_cfg(chat_id, "tz", "")
            return tr("mapset_set", lang, k="tz", v="auto (server)")
        if _resolve_tz(v) is None:               # not a valid offset or IANA name
            return tr("mapset_tz_usage", lang)
        set_map_cfg(chat_id, "tz", v)
        return tr("mapset_set", lang, k="tz", v=v)
    if key in ("reset", "default", "defaults"):
        reset_map_cfg(chat_id)
        return tr("mapset_reset", lang)
    return tr("mapset_unknown", lang, k=key)

# --- Daily forecast alarm (per saved location + time HH:MM, server local time) ----
_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")

def _parse_hhmm(s):
    mt = _TIME_RE.match(s.strip())
    if not mt:
        return None
    h, mi = int(mt.group(1)), int(mt.group(2))
    if 0 <= h <= 23 and 0 <= mi <= 59:
        return f"{h:02d}:{mi:02d}"
    return None

def cmd_alarm(args, chat_id):
    """Set/list/clear daily 24h-forecast alarms. Fires at server local time.
    Examples: 'alarm 1 21:05' (slot 1 at 21:05), 'alarm 1 off', 'alarm off'."""
    lang = get_lang(chat_id)
    cdata = load_state().get(str(chat_id), {})
    alarms = dict(cdata.get("alarms", {}))
    locs = cdata.get("locations", {})
    if not args:
        if not alarms:
            return tr("alarm_none", lang)
        lines = [tr("alarm_list_hdr", lang)]
        for slot in sorted(alarms, key=lambda s: (int(s) if s.isdigit() else 999)):
            loc = locs.get(slot)
            name = loc_label(loc) if loc else slot
            lines.append(f"<code>{slot}</code> {name} — <b>{alarms[slot]}</b>")
        return "\n".join(lines)
    a0 = args[0].lower()
    if a0 in ("off", "stop", "oprit", "none") and len(args) == 1:
        def m(state):
            state.setdefault(str(chat_id), {}).pop("alarms", None)
            state.setdefault(str(chat_id), {}).pop("alarms_fired", None)
        update_state(m)
        return tr("alarm_off_all", lang)
    slot = args[0]
    if slot not in locs:
        return tr("alarm_no_slot", lang, slot=slot)
    if len(args) >= 2 and args[1].lower() in ("off", "stop", "oprit"):
        def m(state):
            a = state.setdefault(str(chat_id), {}).setdefault("alarms", {})
            a.pop(slot, None)
        update_state(m)
        return tr("alarm_off_slot", lang, slot=slot)
    if len(args) < 2:
        return tr("alarm_usage", lang)
    hhmm = _parse_hhmm(args[1])
    if not hhmm:
        return tr("alarm_bad_time", lang)
    def m(state):
        state.setdefault(str(chat_id), {}).setdefault("alarms", {})[slot] = hhmm
    update_state(m)
    return tr("alarm_set", lang, slot=slot, name=loc_label(locs[slot]), t=hhmm)

def collect_due_alarms(now=None):
    """Return [(chat_id, message)] for alarms due at `now` (server local time),
    marking them fired for today so they don't repeat. Shared by both transports."""
    now = now or datetime.now()
    hhmm = now.strftime("%H:%M")
    today = now.strftime("%Y-%m-%d")
    out, to_mark = [], []
    for chat_id, cdata in load_state().items():
        alarms = cdata.get("alarms", {})
        if not alarms:
            continue
        locs = cdata.get("locations", {})
        fired = cdata.get("alarms_fired", {})
        units = get_units(chat_id)
        lang = get_lang(chat_id)
        model_id, model_label = resolve_model(cdata.get("model", DEFAULT_MODEL))
        for slot, t in alarms.items():
            if t != hhmm or fired.get(slot) == today:
                continue
            loc = locs.get(slot)
            if not loc:
                continue
            try:
                data = forecast(loc["lat"], loc["lon"], model_id, units)
                msg = format_24h(loc_label(loc), data, model_label, units, lang)
            except requests.RequestException:
                continue
            out.append((chat_id, msg))
            to_mark.append((str(chat_id), slot, today))
    if to_mark:
        def m(state):
            for cid, slot, day in to_mark:
                fired = state.setdefault(cid, {}).setdefault("alarms_fired", {})
                for k in list(fired.keys()):     # keep only today's marks
                    if fired[k] != day:
                        del fired[k]
                fired[slot] = day
        update_state(m)
    return out

def alarm_loop():
    """Fire due daily alarms; checks about twice a minute."""
    while True:
        try:
            for chat_id, msg in collect_due_alarms():
                send(chat_id, msg)
        except Exception as e:
            print("Alarm loop error:", e)
        time.sleep(30)

# --- WhatsApp health (read the bridge's heartbeat file) ---
def wa_health():
    """Return (state, age_seconds). state: ok / disconnected / loggedout / down / none."""
    try:
        with open(WA_STATUS_FILE, "r", encoding="utf-8") as f:
            s = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return ("none", None)
    age = int(time.time() - s.get("ts", 0))
    if age > WA_HEARTBEAT_STALE:
        return ("down", age)
    if s.get("loggedOut"):
        return ("loggedout", age)
    if not s.get("connected"):
        return ("disconnected", age)
    return ("ok", age)

def wa_monitor_loop():
    """Warn ADMIN_CHAT on Telegram when WhatsApp goes down / is logged out."""
    if not ADMIN_CHAT:
        print("WA monitor off: set TG_ADMIN_CHAT (or TG_ALLOWED_USERS) to get WhatsApp-down alerts.")
        return
    bad, alerted = 0, False
    msgs = {
        "down": "\U0001f534 WhatsApp bridge not responding (no heartbeat) — process stopped or hung.",
        "loggedout": "\U0001f534 WhatsApp logged out — re-scan the QR to reconnect.",
        "disconnected": "⚠️ WhatsApp disconnected — trying to reconnect…",
    }
    while True:
        try:
            state, _age = wa_health()
            if state in ("ok", "none"):
                if alerted and state == "ok":
                    send(ADMIN_CHAT, "✅ WhatsApp is back online.")
                alerted = False
                bad = 0
            else:
                bad += 1
                if bad >= 3 and not alerted:      # ~3 min debounce (60s loop)
                    send(ADMIN_CHAT, msgs.get(state, "⚠️ WhatsApp problem."))
                    alerted = True
        except Exception as e:
            print("WA monitor error:", e)
        time.sleep(60)

def cmd_interval(args, chat_id):
    """Show/set the background alert-check interval (in minutes; applies to everyone)."""
    lang = get_lang(chat_id)
    cur = get_alert_interval()
    if not args:
        return tr("interval_current", lang, m=cur // 60, s=cur)
    try:
        mins = int(args[0])
        assert mins >= 1
    except (ValueError, AssertionError):
        return tr("interval_usage", lang)
    mins = min(mins, 1440)                     # cap at 24h
    set_alert_interval(mins * 60)
    return tr("interval_set", lang, m=mins, s=mins * 60)

def cmd_status(args, chat_id):
    """Clear on-demand health report: bot core, WhatsApp link, interval, this chat."""
    lang = get_lang(chat_id)
    state, age = wa_health()
    wa = {
        "ok": tr("sys_wa_ok", lang, s=(age if age is not None else 0)),
        "disconnected": tr("sys_wa_disc", lang),
        "loggedout": tr("sys_wa_logout", lang),
        "down": tr("sys_wa_down", lang, s=(age if age is not None else 0)),
        "none": tr("sys_wa_none", lang),
    }[state]
    cdata = load_state().get(str(chat_id), {})
    nloc = len(cdata.get("locations", {}))
    alarms = cdata.get("alarms", {})
    feeds = cdata.get("anm_feeds", list(DEFAULT_ANM_FEEDS))
    feeds_s = ", ".join(feeds) if feeds else tr("anm_off_word", lang)
    tzc = get_map_cfg(chat_id).get("tz") or "auto"
    lines = [
        f"\U0001fa7a <b>{tr('sys_title', lang)}</b>",
        tr("sys_core", lang),
        f"\U0001f4f1 {wa}",
        tr("sys_interval", lang, m=get_alert_interval() // 60),
        tr("alerts_state_on" if get_alerts_enabled(chat_id) else "alerts_state_off", lang),
        tr("sys_anm", lang, feeds=feeds_s),
        tr("sys_chat", lang, n=nloc, a=len(alarms), tz=tzc),
    ]
    return "\n".join(lines)

def _verify_system_password(pw):
    """True/False if `pw` is the bot user's system password (checked via PAM, so it
    works even when sudo is passwordless). None if PAM isn't available to verify."""
    try:
        import pam
        import pwd
    except ImportError:
        return None
    try:
        user = pwd.getpwuid(os.getuid()).pw_name
        return bool(pam.pam().authenticate(user, pw))
    except Exception:
        return None

def cmd_restart(args, chat_id):
    """Restart the systemd services. The password is checked (dedicated
    TG_RESTART_PASSWORD if set, otherwise the system password via PAM). Fails CLOSED:
    it never restarts unless the password was actually verified. Detached + --no-block
    so it survives restarting its own service."""
    lang = get_lang(chat_id)
    pw = " ".join(args)
    if not pw:
        return tr("restart_usage", lang)
    dedicated = os.environ.get("TG_RESTART_PASSWORD", "")
    if dedicated:
        if pw != dedicated:
            return tr("restart_bad_pw", lang)
    else:
        ok = _verify_system_password(pw)
        if ok is None:                       # cannot verify -> refuse (no open gate)
            return tr("restart_no_verify", lang)
        if not ok:
            return tr("restart_bad_pw", lang)
    # Launch detached; feed pw to sudo -S (used only if sudo actually needs it),
    # short delay so this reply is delivered before the service restarts.
    try:
        p = subprocess.Popen(
            ["sh", "-c", f"sleep 3; exec sudo -S systemctl restart --no-block {RESTART_UNITS}"],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True, text=True)
        p.stdin.write(pw + "\n"); p.stdin.flush(); p.stdin.close()
    except Exception as e:
        return tr("restart_err", lang, e=e)
    print(f"[restart] triggered by chat {chat_id}: {RESTART_UNITS}")
    return tr("restart_ok", lang)

def redact_log(text):
    """Hide the password when a restart command is echoed to the logs."""
    parts = (text or "").lstrip("/").split()
    if parts and parts[0].lower() in ("restart", "restartsys") and len(parts) > 1:
        return parts[0] + " ***"
    return text

# --- Command router (easy to extend) ---
COMMANDS = {
    "wx": cmd_wx, "model": cmd_model,
    "save": cmd_save, "locs": cmd_locs, "del": cmd_del, "alerts": cmd_alerts,
    "set": cmd_set, "units": cmd_units, "soil": cmd_soil, "hist": cmd_hist,
    "air": cmd_air, "aer": cmd_air, "flood": cmd_flood, "inundatii": cmd_flood,
    "cote": cmd_cote, "river": cmd_cote, "dunare": cmd_cote, "nivel": cmd_cote,
    "marine": cmd_marine, "mare": cmd_marine, "wind": cmd_wind, "vant": cmd_wind,
    "lang": cmd_lang, "anm": cmd_anm, "alarm": cmd_alarm, "alarma": cmd_alarm,
    "interval": cmd_interval,
    "status": cmd_status, "sysstatus": cmd_status, "sys": cmd_status,   # sysstatus kept as alias
    "restart": cmd_restart, "restartsys": cmd_restart,                  # restartsys kept as alias
    "radar": cmd_radar, "sat": cmd_sat, "satelit": cmd_sat,
    "map": cmd_map, "harta": cmd_map, "mapset": cmd_mapset, "hartaset": cmd_mapset,
    "heat": cmd_heat, "temp": cmd_heat, "caldura": cmd_heat,
    "hum": cmd_hum, "humidity": cmd_hum, "umiditate": cmd_hum,
    "thm": cmd_theme, "theme": cmd_theme, "tema": cmd_theme,
    "start": cmd_start, "help": cmd_start,
}

def handle_text(text, chat_id, lang_hint=None):
    text = text.strip()
    if not text:
        return None
    ensure_lang(chat_id, lang_hint)   # adopt phone language on first message
    parts = text.lstrip("/").split()
    cmd = parts[0].lower()
    fn = COMMANDS.get(cmd)
    if fn is not None:
        args = parts[1:]
    else:
        fn = cmd_wx          # not a command -> treat the message as a place name
        args = parts
    lang = get_lang(chat_id)
    try:
        return fn(args, chat_id)
    except requests.RequestException as e:
        return tr("fetch_generic", lang, e=e)
    except Exception as e:
        return tr("err_generic", lang, e=e)

# --- Telegram ---
TG_LIMIT = 4096   # Telegram hard limit per message (chars)

def _split_message(text, limit=TG_LIMIT):
    """Split long text into <=limit chunks, preferring line boundaries so we
    don't cut inside an HTML tag."""
    if len(text) <= limit:
        return [text]
    chunks, cur = [], ""
    for line in text.split("\n"):
        while len(line) > limit:        # a single line longer than the limit
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(line[:limit])
            line = line[limit:]
        add = line if not cur else "\n" + line
        if len(cur) + len(add) > limit:
            chunks.append(cur)
            cur = line
        else:
            cur += add
    if cur:
        chunks.append(cur)
    return chunks

def _post_message(chat_id, text):
    try:
        r = requests.post(f"{TG_API}/sendMessage", json={
            "chat_id": chat_id, "text": text,
            "parse_mode": "HTML", "disable_web_page_preview": True,
        }, timeout=15)
        if not r.ok:
            print(f"Telegram rejected message (chat {chat_id}): {r.status_code} {r.text}")
    except requests.RequestException as e:
        print("Send error:", e)

def send(chat_id, text):
    for chunk in _split_message(text):
        _post_message(chat_id, chunk)

def send_photo(chat_id, data, caption=""):
    try:
        requests.post(f"{TG_API}/sendPhoto",
                      data={"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"},
                      files={"photo": ("map.png", data, "image/png")}, timeout=30)
    except requests.RequestException as e:
        print("sendPhoto error:", e)

def is_allowed(user_id, chat_id):
    if not ALLOWED_USERS:
        return True
    return str(user_id) in ALLOWED_USERS or str(chat_id) in ALLOWED_USERS

# --- Background alert checker ---
def alert_loop():
    while True:
        try:
            anm_areas = anm_get_areas()          # official ANM warnings, once per cycle
            state = load_state()
            for chat_id, cdata in state.items():
                locations = cdata.get("locations", {})
                if not locations:
                    continue
                if not cdata.get("alerts_on", True):   # user paused automatic alerts
                    continue
                model_id, model_label = MODELS.get(
                    cdata.get("model", DEFAULT_MODEL), MODELS[DEFAULT_MODEL])
                thr = get_thresholds(chat_id)
                lang = get_lang(chat_id)
                for slot, loc in locations.items():
                    for m in anm_alerts_for(chat_id, slot, loc, anm_areas, lang):
                        send(chat_id, m)
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
                        new_msgs.append(format_alert_line(phenom, val, tstr, lang))
                        mark_sent(chat_id, key, today)
                    if new_msgs:
                        send(chat_id, build_alert_message(loc, new_msgs, model_label, lang))
        except Exception as e:
            print("Alert loop error:", e)
        time.sleep(get_alert_interval())

def main():
    if not BOT_TOKEN:
        raise SystemExit("Set the TG_BOT_TOKEN environment variable (token from @BotFather).")
    threading.Thread(target=alert_loop, daemon=True).start()
    threading.Thread(target=alarm_loop, daemon=True).start()
    threading.Thread(target=wa_monitor_loop, daemon=True).start()
    print(f"Bot started. Alert check every {get_alert_interval()}s. Waiting for messages...")
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
                lang_hint = msg.get("from", {}).get("language_code")
                text = msg["text"]
                print(f"[msg] chat_id={chat_id} user_id={user_id}: {redact_log(text)!r}")
                if not is_allowed(user_id, chat_id):
                    continue
                reply = handle_text(text, chat_id, lang_hint)
                if isinstance(reply, Photo):
                    send_photo(chat_id, reply.data, reply.caption)
                elif reply:
                    send(chat_id, reply)
        except requests.RequestException as e:
            print("Network error:", e)
            time.sleep(5)
        except KeyboardInterrupt:
            print("\nStopped.")
            break

if __name__ == "__main__":
    main()
