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
        "en": ("Usage:\n<code>wx Orsova</code> \u2014 24h hourly\n"
               "<code>wx 44.816,29.879</code> \u2014 by coordinates\n"
               "<code>wx Orsova 3</code> \u2014 3-day forecast\n"
               "<code>wx 3</code> \u2014 3-day for all saved locations"),
        "ro": ("Utilizare:\n<code>wx Orsova</code> \u2014 orar pe 24h\n"
               "<code>wx 44.816,29.879</code> \u2014 dupa coordonate\n"
               "<code>wx Orsova 3</code> \u2014 prognoza pe 3 zile\n"
               "<code>wx 3</code> \u2014 3 zile pentru toate locatiile salvate"),
    },
    "wx_usage_short": {
        "en": "Usage: <code>wx Orsova</code> or <code>wx Orsova 3</code>",
        "ro": "Utilizare: <code>wx Orsova</code> sau <code>wx Orsova 3</code>",
    },
    "no_saved_wx": {
        "en": "No saved locations. Add one (<code>save 1 Orsova</code>) or use <code>wx Orsova 3</code>",
        "ro": "Nicio locatie salvata. Adauga una (<code>save 1 Orsova</code>) sau foloseste <code>wx Orsova 3</code>",
    },
    "loc_not_found": {
        "en": "Location not found: {loc}", "ro": "Locatie negasita: {loc}",
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
               "or by coordinates: <code>save 1 44.816,29.879</code>"),
        "ro": ("Utilizare: <code>save 1 Orsova</code>\n"
               "sau dupa coordonate: <code>save 1 44.816,29.879</code>"),
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

    loc = resolve_location(loc_text, chat_id)
    if not loc:
        return tr("loc_not_found", lang, loc=loc_text)
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
    loc_text = " ".join(args[1:]).strip()
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
    loc = resolve_location(" ".join(args), chat_id)
    if not loc:
        return tr("loc_not_found_plain", lang)
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
    loc = resolve_location(loc_text, chat_id)
    if not loc:
        return tr("loc_not_found", lang, loc=loc_text)
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
        "<b>Units &amp; language</b>\n"
        "<code>units</code> \u2014 show current display units\n"
        "<code>units temp F</code> \u2014 set units (temp C/F, wind kmh/ms/mph/kn, "
        "rain mm/inch, pressure hpa/mmhg/inhg)\n"
        "<code>lang ro</code> / <code>lang en</code> \u2014 change language\n\n"
        "Saved locations are watched automatically: you get a message when strong "
        "wind, rain, snow, heat or frost is expected in the next hours.\n\n"
        "Type <code>help</code> to see this list again."
    ),
    "ro": (
        "<b>Bot meteo personal</b> \U0001f324\ufe0f\n"
        "Sursa datelor: Open-Meteo (gratuit).\n\n"
        "<b>Prognoza</b>\n"
        "<code>wx Orsova</code> \u2014 prognoza orara pe 24h\n"
        "<code>wx 44.816,29.879</code> \u2014 dupa coordonate\n"
        "<code>wx Orsova 3</code> \u2014 prognoza pe 3 zile (pana la 16)\n"
        "<code>wx 7</code> \u2014 7 zile pentru toate locatiile salvate\n\n"
        "<b>Sol &amp; istoric</b>\n"
        "<code>soil Orsova</code> \u2014 umiditatea solului + temperatura acum\n"
        "<code>hist Orsova 2025-07-01 2025-07-10</code> \u2014 vremea din trecut pe o perioada\n\n"
        "<b>Model</b>\n"
        "<code>model</code> \u2014 arata modelul curent si lista de modele\n"
        "<code>model iconeu</code> \u2014 seteaza modelul implicit (nume din lista)\n\n"
        "<b>Locatii salvate</b>\n"
        "<code>save 1 Orsova</code> \u2014 salveaza o locatie in slotul 1\n"
        "<code>locs</code> \u2014 listeaza locatiile salvate\n"
        "<code>del 1</code> \u2014 sterge locatia din slotul 1\n\n"
        "<b>Alerte</b>\n"
        "<code>alerts</code> \u2014 verifica acum locatiile salvate\n"
        "<code>set</code> \u2014 arata pragurile de alerta curente\n"
        "<code>set gust 70</code> \u2014 schimba un prag "
        "(gust km/h, rain mm/h, snow cm/h, heat \u00b0C, frost \u00b0C)\n\n"
        "<b>Unitati &amp; limba</b>\n"
        "<code>units</code> \u2014 arata unitatile de afisare curente\n"
        "<code>units temp F</code> \u2014 seteaza unitatile (temp C/F, wind kmh/ms/mph/kn, "
        "rain mm/inch, pressure hpa/mmhg/inhg)\n"
        "<code>lang ro</code> / <code>lang en</code> \u2014 schimba limba\n\n"
        "Locatiile salvate sunt monitorizate automat: primesti mesaj cand se asteapta "
        "vant puternic, ploaie, ninsoare, canicula sau inghet in urmatoarele ore.\n\n"
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

# --- Command router (easy to extend) ---
COMMANDS = {
    "wx": cmd_wx, "model": cmd_model,
    "save": cmd_save, "locs": cmd_locs, "del": cmd_del, "alerts": cmd_alerts,
    "set": cmd_set, "units": cmd_units, "soil": cmd_soil, "hist": cmd_hist,
    "lang": cmd_lang,
    "start": cmd_start, "help": cmd_start,
}

def handle_text(text, chat_id, lang_hint=None):
    text = text.strip()
    if not text:
        return None
    ensure_lang(chat_id, lang_hint)   # adopt phone language on first message
    parts = text.lstrip("/").split()
    cmd = parts[0].lower()
    args = parts[1:]
    fn = COMMANDS.get(cmd)
    if fn:
        lang = get_lang(chat_id)
        try:
            return fn(args, chat_id)
        except requests.RequestException as e:
            return tr("fetch_generic", lang, e=e)
        except Exception as e:
            return tr("err_generic", lang, e=e)
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
                lang = get_lang(chat_id)
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
