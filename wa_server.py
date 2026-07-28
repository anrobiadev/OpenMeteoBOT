#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WhatsApp transport for the weather bot (UNOFFICIAL, via a Baileys Node bridge).

Architecture:
  WhatsApp  <->  wa_bridge.js (Baileys, Node)  <->  this HTTP service (Python)
                                                     reuses ALL of meteo_bot.py

This service:
  - POST /incoming {from, text} -> {reply}   (Node forwards user messages here)
  - runs the alert loop and PUSHES alerts to the Node bridge (POST /send)

WARNING: Baileys logs in as a WhatsApp Web client tied to YOUR phone number.
This is against WhatsApp's Terms of Service and the number CAN be banned.
Use at your own risk, ideally with a secondary number.

Env:
  WA_SEND_URL   Node bridge send endpoint (default http://127.0.0.1:3000/send)
  PY_PORT       port this service listens on (default 5000)
  TG_BOT_STATE  state file — set a DEDICATED one for WhatsApp, e.g. wa_state.json,
                so it doesn't mix with the Telegram bot's state.
"""
import os
import re
import html
import time
import base64
import threading
import requests
from flask import Flask, request, jsonify

import meteo_bot as core   # reuse all command logic + Open-Meteo helpers

WA_SEND_URL = os.environ.get("WA_SEND_URL", "http://127.0.0.1:3000/send")
PY_PORT = int(os.environ.get("PY_PORT", "5000"))

app = Flask(__name__)


def to_whatsapp(s):
    """Convert the bot's Telegram HTML markup to WhatsApp formatting."""
    if not s:
        return s
    s = re.sub(r"</?code>", "", s)                        # drop <code> tags
    s = re.sub(r"<b>(.*?)</b>", r"*\1*", s, flags=re.S)    # bold  -> *text*
    s = re.sub(r"<i>(.*?)</i>", r"_\1_", s, flags=re.S)    # italic -> _text_
    s = re.sub(r"<[^>]+>", "", s)                          # any stray tag
    return html.unescape(s)


@app.route("/incoming", methods=["POST"])
def incoming():
    data = request.get_json(force=True, silent=True) or {}
    sender = str(data.get("from", ""))     # WhatsApp JID, used as chat_id
    text = data.get("text", "")
    if not sender or not text:
        return jsonify({"reply": ""})
    try:
        reply = core.handle_text(text, sender)
    except Exception as e:
        reply = f"Error: {e}"
    if isinstance(reply, core.Photo):          # map/radar/sat -> send an image
        print(f"[incoming] {text!r} -> image ({len(reply.data)} bytes)", flush=True)
        return jsonify({"image": base64.b64encode(reply.data).decode(),
                        "caption": to_whatsapp(reply.caption)})
    print(f"[incoming] {text!r} -> text ({len(reply) if reply else 0} chars)", flush=True)
    return jsonify({"reply": to_whatsapp(reply) if reply else ""})


def wa_send(chat_id, text):
    try:
        requests.post(WA_SEND_URL, json={"to": chat_id, "text": text}, timeout=15)
    except requests.RequestException as e:
        print("WA send error:", e)


def alert_loop():
    """Same logic as meteo_bot.alert_loop, but pushes via the Node bridge."""
    while True:
        try:
            anm_areas = core.anm_get_areas()     # official ANM warnings, once per cycle
            state = core.load_state()
            for chat_id, cdata in state.items():
                locations = cdata.get("locations", {})
                if not locations:
                    continue
                model_id, model_label = core.MODELS.get(
                    cdata.get("model", core.DEFAULT_MODEL), core.MODELS[core.DEFAULT_MODEL])
                thr = core.get_thresholds(chat_id)
                lang = core.get_lang(chat_id)
                for slot, loc in locations.items():
                    for m in core.anm_alerts_for(chat_id, slot, loc, anm_areas, lang):
                        wa_send(chat_id, to_whatsapp(m))
                    try:
                        data = core.fetch_alert_forecast(loc["lat"], loc["lon"], model_id)
                    except requests.RequestException:
                        continue
                    trig = core.evaluate_alerts(data, thr)
                    if not trig:
                        continue
                    today = core.local_today(data)
                    new_msgs = []
                    for phenom, (val, tstr) in trig.items():
                        if phenom not in core.ALERTS_ENABLED:
                            continue
                        key = f"{slot}:{phenom}:{today}"
                        if core.already_sent(chat_id, key):
                            continue
                        new_msgs.append(core.format_alert_line(phenom, val, tstr, lang))
                        core.mark_sent(chat_id, key, today)
                    if new_msgs:
                        msg = core.build_alert_message(loc, new_msgs, model_label, lang)
                        wa_send(chat_id, to_whatsapp(msg))
        except Exception as e:
            print("Alert loop error:", e)
        time.sleep(core.get_alert_interval())


def alarm_loop():
    """Fire due daily forecast alarms over WhatsApp (checks ~twice a minute)."""
    while True:
        try:
            for chat_id, msg in core.collect_due_alarms():
                wa_send(chat_id, to_whatsapp(msg))
        except Exception as e:
            print("Alarm loop error:", e)
        time.sleep(30)


def main():
    threading.Thread(target=alert_loop, daemon=True).start()
    threading.Thread(target=alarm_loop, daemon=True).start()
    print(f"WhatsApp Python service on :{PY_PORT}, alerts every {core.get_alert_interval()}s")
    app.run(host="127.0.0.1", port=PY_PORT)


if __name__ == "__main__":
    main()
