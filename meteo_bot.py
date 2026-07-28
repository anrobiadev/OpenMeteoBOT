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
import unicodedata
import threading
import requests
from io import BytesIO
try:
    from PIL import Image, ImageDraw
    _PIL = True
except Exception:
    _PIL = False
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
CHECK_INTERVAL_SEC = int(os.environ.get("TG_ALERT_INTERVAL", "900"))  # 30 min
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
    return MODELS.get(key, (None, ""))[1]

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
    "soil_temp0": {"en": "\U0001f321 soil temp 0cm: {v}", "ro": "\U0001f321 temp sol 0cm: {v}"},
    "soil_airhum": {"en": "\U0001f4a7 air humidity: {v}", "ro": "\U0001f4a7 umiditate aer: {v}"},
    "soil_moist_h": {"en": "soil moisture (vol. water):", "ro": "umiditate sol (apa vol.):"},
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
    "map_src_clouds": {"en": "source: Open-Meteo, \u00a9 OpenStreetMap", "ro": "sursa: Open-Meteo, \u00a9 OpenStreetMap"},
    "map_src_both": {"en": "source: Open-Meteo + RainViewer, \u00a9 OpenStreetMap", "ro": "sursa: Open-Meteo + RainViewer, \u00a9 OpenStreetMap"},
    "map_nopil": {"en": "Image maps need Pillow: <code>pip install pillow</code>",
                  "ro": "Hartile necesita Pillow: <code>pip install pillow</code>"},
    "map_nodata": {"en": "No map data available right now.", "ro": "Fara date de harta momentan."},
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
        lines.append(
            f"{t:%H:%M} {emo} {temp}{tlab} ({feels_w} {feel}\u00b0)  "
            f"\U0001f4a7{pop_s}{amt_s}  \U0001f4a8{wind} {wlab} ({gust_w} {gust}){pres_s}  {desc}"
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
        lines.append(
            f"{wd} {dt:%d.%m} {emo} {lo}/{hi}{tlab}  "
            f"\U0001f4a7{pr_s}{ps_s}  \U0001f4a8{wnd} {wlab} ({gust_w} {gst})  {desc}"
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
        sig = hashlib.md5(
            f"{area['culoare']}|{area['expira']}|{area['fenomen']}".encode()).hexdigest()[:8]
        key = f"anm:{slot}:{sig}:{today}"
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
RAINVIEWER_INDEX = "https://api.rainviewer.com/public/weather-maps.json"
# Cloud cover comes from Open-Meteo (RainViewer's free tier has no satellite):
CLOUD_URL = "https://api.open-meteo.com/v1/forecast"
CLOUD_COLS = int(os.environ.get("TG_CLOUD_COLS", "14"))
CLOUD_ROWS = int(os.environ.get("TG_CLOUD_ROWS", "12"))
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
        return f"{host}{path}/{TILE}/{z}/{x}/{y}/4/1_1.png"      # color 4, smooth+snow
    return f"{host}{path}/{TILE}/{z}/{x}/{y}/0/0_0.png"          # satellite

def _cloud_overlay(bbox, w, h):
    """White cloud-cover overlay from Open-Meteo cloud_cover sampled on a grid over
    bbox=(latN, lonW, latS, lonE). Returns (RGBA_overlay, 'HH:MM') or (None, '')."""
    latN, lonW, latS, lonE = bbox
    lats, lons = [], []
    for r in range(CLOUD_ROWS):
        lat = latN + (latS - latN) * (r + 0.5) / CLOUD_ROWS      # row 0 = north (top)
        for c in range(CLOUD_COLS):
            lon = lonW + (lonE - lonW) * (c + 0.5) / CLOUD_COLS
            lats.append(round(lat, 4)); lons.append(round(lon, 4))
    try:
        rr = requests.get(CLOUD_URL, params={
            "latitude": ",".join(map(str, lats)),
            "longitude": ",".join(map(str, lons)),
            "current": "cloud_cover", "timezone": "UTC",
        }, timeout=20, headers=_TILE_UA)
        rr.raise_for_status()
        data = rr.json()
    except (requests.RequestException, ValueError):
        return None, ""
    results = data if isinstance(data, list) else [data]
    grid = Image.new("RGBA", (CLOUD_COLS, CLOUD_ROWS), (0, 0, 0, 0))
    px = grid.load()
    tstamp = ""
    for i, res in enumerate(results):
        cur = res.get("current", {}) if isinstance(res, dict) else {}
        if not tstamp:
            tstamp = cur.get("time", "")
        cc = cur.get("cloud_cover")
        c, row = i % CLOUD_COLS, i // CLOUD_COLS
        if cc is None or row >= CLOUD_ROWS:
            continue
        a = int(CLOUD_MAX_ALPHA * max(0.0, min(100.0, float(cc))) / 100.0)
        px[c, row] = CLOUD_RGB + (a,)                            # grey clouds (darker = denser)
    overlay = grid.resize((w, h), Image.BICUBIC)                 # smooth field
    return overlay, (tstamp[-5:] if len(tstamp) >= 5 else "")    # 'HH:MM'

def build_map(lat, lon, layers, z=None, w=None, h=None, base_dim=0.0):
    """Stitch OSM base + weather overlays, centered on the point, with a marker.
    `layers` items: 'radar'/'satellite' (RainViewer tiles) or 'clouds' (Open-Meteo).
    `base_dim` (0..1) washes out the OSM base so weather stands out.
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

    paste_layer(lambda x, y: _fetch_img(OSM_TILE.format(z=z, x=x, y=y)), False)
    if base_dim > 0:                                  # fade the base map
        canvas.alpha_composite(Image.new("RGBA", (w, h), (255, 255, 255, int(255 * min(1.0, base_dim)))))
    tlabel = ""
    for layer in layers:
        if layer == "clouds":
            latN, lonW = tilexy_to_lonlat(left / TILE, top / TILE, z)
            latS, lonE = tilexy_to_lonlat((left + w) / TILE, (top + h) / TILE, z)
            overlay, clabel = _cloud_overlay((latN, lonW, latS, lonE), w, h)
            if overlay is not None:
                canvas.alpha_composite(overlay)
                if clabel and not tlabel:
                    tlabel = clabel
            continue
        url_of = lambda x, y, L=layer: _overlay_url(frames, L, z, x, y)
        paste_layer(lambda x, y: _fetch_img(url_of(x, y)) if url_of(x, y) else None, True)
        ts = frames.get("radar_time" if layer == "radar" else "sat_time")
        if ts and not tlabel:
            tlabel = datetime.fromtimestamp(ts, timezone.utc).strftime("%H:%M")

    # marker at the exact point (center of the canvas)
    d = ImageDraw.Draw(canvas)
    mx, my = w // 2, h // 2
    d.ellipse([mx - 7, my - 7, mx + 7, my + 7], outline=(255, 0, 0, 255), width=3)
    d.ellipse([mx - 2, my - 2, mx + 2, my + 2], fill=(255, 0, 0, 255))

    buf = BytesIO()
    canvas.convert("RGB").save(buf, format="PNG")
    return buf.getvalue(), tlabel

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
    lang = get_lang(chat_id)
    if not args:
        return tr("wx_usage", lang)
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
        return tr("model_unknown", lang, k=key, opts=", ".join(MODELS.keys()))
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
    cdata = load_state().get(str(chat_id), {})
    locations = cdata.get("locations", {})
    if not locations:
        return tr("no_saved_add", lang)
    model_id, model_label = MODELS.get(cdata.get("model", DEFAULT_MODEL), MODELS[DEFAULT_MODEL])
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
    return "\n\n".join(out) + "\n\n" + tr("src_model", lang, model=model_label)

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
    model_id = MODELS.get(get_model(chat_id), MODELS[DEFAULT_MODEL])[0]
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
            return "\u2014"
        v = a[idx] * scale
        return f"{v:.{dec}f}{unit}"

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
             f"  27\u201381cm: {val('soil_moisture_27_to_81cm', 100, '%')}"]
    return "\n".join(lines)

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
        "<b>Soil &amp; history</b>\n"
        "<code>soil Orsova</code> \u2014 soil moisture + temperature now\n"
        "<code>hist Orsova 2025-07-01 2025-07-10</code> \u2014 past weather for a period\n\n"
        "<b>Maps</b>\n"
        "<code>radar Orsova</code> \u2014 radar over a faded map\n"
        "<code>sat Orsova</code> \u2014 cloud cover\n"
        "<code>map Orsova</code> \u2014 clouds + radar\n\n"
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
        "<code>set</code> \u2014 show current alert thresholds\n"
        "<code>set gust 70</code> \u2014 change a threshold "
        "(gust km/h, rain mm/h, snow cm/h, heat \u00b0C, frost \u00b0C)\n"
        "<code>anm</code> \u2014 official ANM warnings: <code>anm nowcasting,general</code> / <code>anm off</code>\n\n"
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
        "<b>Sol &amp; istoric</b>\n"
        "<code>soil Orsova</code> \u2014 umiditatea solului + temperatura acum\n"
        "<code>hist Orsova 2025-07-01 2025-07-10</code> \u2014 vremea din trecut pe o perioada\n\n"
        "<b>Harti</b>\n"
        "<code>radar Orsova</code> \u2014 radar peste harta estompata\n"
        "<code>sat Orsova</code> \u2014 acoperire cu nori\n"
        "<code>map Orsova</code> \u2014 nori + radar\n\n"
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
        "<code>set</code> \u2014 arata pragurile de alerta curente\n"
        "<code>set gust 70</code> \u2014 schimba un prag "
        "(gust km/h, rain mm/h, snow cm/h, heat \u00b0C, frost \u00b0C)\n"
        "<code>anm</code> \u2014 avertizari oficiale ANM: <code>anm nowcasting,general</code> / <code>anm off</code>\n\n"
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

def _map_cmd(args, chat_id, layers, label_key, base_dim=0.0, src_key="map_src"):
    lang = get_lang(chat_id)
    if not _PIL:
        return tr("map_nopil", lang)
    if not args:
        return tr("wx_usage_short", lang)
    loc, err = find_location(" ".join(args), chat_id, lang)
    if err:
        return err
    try:
        png, tlabel = build_map(loc["lat"], loc["lon"], layers, base_dim=base_dim)
    except Exception as e:
        return tr("err_generic", lang, e=e)
    if not png:
        return tr("map_nodata", lang)
    cap = f"\U0001f4cd <b>{loc_label(loc)}</b> \u2014 {tr(label_key, lang)}"
    cap += ("\n" + (f"{tlabel} UTC \u00b7 " if tlabel else "") + tr(src_key, lang))
    return Photo(png, cap)

def cmd_radar(args, chat_id):
    # Radar over a faded OSM base so borders/coastlines show; radar stays prominent.
    return _map_cmd(args, chat_id, ["radar"], "cap_radar",
                    base_dim=MAP_BASE_DIM, src_key="map_src_radar")

def cmd_sat(args, chat_id):
    # Cloud cover from Open-Meteo (RainViewer's free tier has no satellite).
    return _map_cmd(args, chat_id, ["clouds"], "cap_sat",
                    base_dim=MAP_BASE_DIM, src_key="map_src_clouds")

def cmd_map(args, chat_id):
    return _map_cmd(args, chat_id, ["clouds", "radar"], "cap_map",
                    base_dim=MAP_BASE_DIM, src_key="map_src_both")

# --- Command router (easy to extend) ---
COMMANDS = {
    "wx": cmd_wx, "model": cmd_model,
    "save": cmd_save, "locs": cmd_locs, "del": cmd_del, "alerts": cmd_alerts,
    "set": cmd_set, "units": cmd_units, "soil": cmd_soil, "hist": cmd_hist,
    "lang": cmd_lang, "anm": cmd_anm,
    "radar": cmd_radar, "sat": cmd_sat, "satelit": cmd_sat,
    "map": cmd_map, "harta": cmd_map,
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
                lang_hint = msg.get("from", {}).get("language_code")
                text = msg["text"]
                print(f"[msg] chat_id={chat_id} user_id={user_id}: {text!r}")
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
