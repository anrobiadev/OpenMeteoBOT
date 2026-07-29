# OpenMeteoBot — weather bot for Telegram & WhatsApp

A personal weather bot powered by **Open-Meteo** (free, no API key), running on a
Raspberry Pi / Linux. It provides hourly and multi-day forecasts, soil moisture,
air quality, river-discharge (flood) forecasts, historical data, **radar & cloud
maps**, and **automatic alerts** (wind, rain, snow, heat, frost) for saved locations.
It also relays **official ANM warnings** (meteoromania.ro) for your exact point,
can send a **daily forecast at a set time**, and shows the **national radar image**
from ANM. Works on **Telegram** and, optionally, on **WhatsApp**.

Extra data sources it uses (all free, no key): Open-Meteo Air Quality (CAMS),
Open-Meteo Flood (GloFAS), RainViewer (radar tiles), OpenStreetMap (base map),
and meteoromania.ro (ANM warnings + national radar image).

---

## 1. Architecture

Three files, two "worlds":

| File | Role | Runs on |
|---|---|---|
| `meteo_bot.py` | The **Telegram** bot + all weather logic (commands, alerts, Open-Meteo) | Python |
| `wa_server.py` | Service that **reuses** `meteo_bot.py` and exposes the logic for WhatsApp | Python (port 5000) |
| `wa_bridge.js` | The **WhatsApp** bridge (Baileys / unofficial WhatsApp Web) | Node.js (port 3100) |
| `package.json` | Node dependencies for the bridge | — |
| `install.sh` | Interactive installer (dependencies, token, QR, services) | Bash |
| `uninstall.sh` | Removes the services and everything the installer generated | Bash |

WhatsApp flow:

```
WhatsApp  <->  wa_bridge.js (Node, :3100)  <->  wa_server.py (Python, :5000)  ->  meteo_bot.py
```

State files (auto-created; hold saved locations / preferences / sent alerts):

- `bot_state.json` — for Telegram
- `wa_state.json` — for WhatsApp (separate, so they don't mix)
- `wa_auth/` — the WhatsApp session (after you scan the QR once)

> **Note:** Telegram and WhatsApp can run in parallel. You can use Telegram only,
> WhatsApp only, or both.

---

## Quick install (recommended) — `install.sh`

The easiest way: an **interactive script** that clones the repo and sets everything up.

```bash
# install git if needed
sudo apt install -y git

# clone the repository
git clone https://github.com/anrobiadev/OpenMeteoBOT.git
cd OpenMeteoBOT

# run the interactive installer
chmod +x install.sh
./install.sh
```

What it does, step by step:

1. Asks what you want: **Telegram**, **WhatsApp**, or **both**.
2. Installs the required dependencies (Python: `requests`/`pillow`, plus `flask`
   for WhatsApp; for WhatsApp it also checks/installs **Node 20+** and the Baileys
   packages via `npm install`).
3. **Telegram:** asks for your token (@BotFather) and saves it to `meteobot.env`
   (optionally also the list of allowed IDs).
4. **WhatsApp:** shows the **QR code** to scan and waits for the connection
   (it auto-picks a free port for the bridge).
5. Creates and starts the **systemd services**, which start automatically on reboot.

At the end, send `help` to the bot. If you prefer to do it **manually** (or for
troubleshooting), follow the sections below — they describe the same process.

---

## 2. Requirements

- Raspberry Pi / Linux (Debian/Ubuntu/Raspberry Pi OS)
- **Python 3** (pre-installed)
- **Node.js 20+** (required for WhatsApp/Baileys) — see step 4
- A Telegram account (for the token) and/or a second WhatsApp number

---

## 3. Installing the files

Put all files in the **same folder** (here we use `~/OpenMeteoBot`):

```bash
mkdir -p ~/OpenMeteoBot
cd ~/OpenMeteoBot
# copy here: meteo_bot.py, wa_server.py, wa_bridge.js, package.json, install.sh
```

If they were copied with `sudo` or are owned by root, take ownership:

```bash
sudo chown -R $USER:$USER ~/OpenMeteoBot
```

---

## 4. Installing dependencies

### Python

```bash
pip install requests flask pillow --break-system-packages
```

(`--break-system-packages` is required on recent Raspberry Pi OS / Debian.)

- **`pillow`** is needed for the map commands (`radar`, `sat`, `map`). Without it
  those commands reply that Pillow is missing; everything else still works.
- For local time on maps with **named** time zones (e.g. `Europe/Bucharest`), make
  sure `tzdata` is present: `sudo apt install -y tzdata`. A numeric offset
  (`mapset tz +3`) works without it.

### Node.js 20+ (only if you use WhatsApp)

Check the version:

```bash
node -v
```

If it's below **v20**, upgrade via NodeSource:

```bash
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt install -y nodejs
node -v      # must be v20.x or newer
```

Then install the Node dependencies (in the folder with `package.json`):

```bash
cd ~/OpenMeteoBot
npm install
```

---

## 5. Telegram bot — token

1. In Telegram open **@BotFather** → `/newbot` → pick a name and a username ending
   in `bot`. You get a **token** like `123456789:AAE...`.

2. Put the token in a `.env` file (safer than in the service):

   ```bash
   cd ~/OpenMeteoBot
   echo 'TG_BOT_TOKEN=123456789:AAE...your-token' > meteobot.env
   chmod 600 meteobot.env
   ```

   No `export`, no quotes — just `KEY=value`.

3. (Optional) To make the bot reply only to you, add a new line to `meteobot.env`:

   ```
   TG_ALLOWED_USERS=123456789
   ```

   `123456789` = your **numeric** Telegram ID (not the phone number). Find it by
   messaging **@userinfobot**, or by starting the bot once and reading the console
   line `[msg] chat_id=... user_id=...`.

### Quick test (manual)

```bash
cd ~/OpenMeteoBot
export TG_BOT_TOKEN="123456789:AAE...your-token"
python3 meteo_bot.py
```

It should print `Bot started...`. Send `help` from Telegram. `Ctrl+C` to stop it.

---

## 6. WhatsApp — QR linking

> ⚠️ **Warning:** WhatsApp via Baileys logs in as a WhatsApp Web client tied to your
> number and **violates WhatsApp's Terms of Service** — the number **can be banned**.
> Use a **secondary number** if possible.

WhatsApp needs **two** processes running (Python + Node). For the first link we run
them manually so you can see the QR code.

**Terminal 1 — Python service:**

```bash
cd ~/OpenMeteoBot
export TG_BOT_STATE=wa_state.json
export WA_SEND_URL="http://127.0.0.1:3100/send"
python3 wa_server.py
```

You should see `Running on http://127.0.0.1:5000`. Leave it open.

**Terminal 2 — Node bridge:**

```bash
cd ~/OpenMeteoBot
export WA_PORT=3100
# export WA_ALLOWED="407xxxxxxxx"   # your number (digits only) as a filter; empty = anyone
node wa_bridge.js
```

A **QR code** appears in the terminal. On your phone: **WhatsApp → Settings → Linked
devices → Link a device** → scan the QR.

After it connects (`WhatsApp connected.`), message the bot **from a different
phone/number** with `help`. (The bot **ignores** messages sent from the linked number
— you cannot message yourself.)

The session is saved in `wa_auth/`, so you won't scan the QR again next time.

---

## 7. Auto-start on boot (systemd)

We turn the processes into services that start on boot and restart if they crash.

First stop any manually started instances so ports are free:

```bash
pkill -f meteo_bot.py ; pkill -f wa_server.py ; pkill -f wa_bridge.js
```

### 7.1 Telegram — `meteobot.service`

```bash
cd ~/OpenMeteoBot
sudo tee /etc/systemd/system/meteobot.service > /dev/null << EOF
[Unit]
Description=Telegram weather bot (meteo_bot.py)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$PWD
EnvironmentFile=$PWD/meteobot.env
Environment=TG_BOT_STATE=bot_state.json
ExecStart=$(which python3) $PWD/meteo_bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
```

### 7.2 WhatsApp Python — `wa-server.service`

```bash
cd ~/OpenMeteoBot
sudo tee /etc/systemd/system/wa-server.service > /dev/null << EOF
[Unit]
Description=WhatsApp weather bot - Python service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$PWD
Environment=TG_BOT_STATE=wa_state.json
Environment=WA_SEND_URL=http://127.0.0.1:3100/send
Environment=PY_PORT=5000
ExecStart=$(which python3) $PWD/wa_server.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
```

### 7.3 WhatsApp bridge — `wa-bridge.service`

```bash
cd ~/OpenMeteoBot
sudo tee /etc/systemd/system/wa-bridge.service > /dev/null << EOF
[Unit]
Description=WhatsApp weather bot - Baileys bridge
After=network-online.target wa-server.service
Wants=network-online.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$PWD
Environment=WA_PORT=3100
Environment=PY_URL=http://127.0.0.1:5000/incoming
Environment=WA_ALLOWED=
ExecStart=$(which node) $PWD/wa_bridge.js
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
```

> To set the WhatsApp number filter, change the line to
> `Environment=WA_ALLOWED=407xxxxxxxx` and run
> `sudo systemctl daemon-reload && sudo systemctl restart wa-bridge`.

### 7.4 Enable everything

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now meteobot.service wa-server.service wa-bridge.service
```

For **Telegram only** or **WhatsApp only**, enable just the relevant services.

---

## 8. Verify

Service status (must be `active (running)`):

```bash
systemctl status meteobot.service wa-server.service wa-bridge.service
```

Live logs (replace with the service you want):

```bash
journalctl -u wa-bridge.service -f
journalctl -u meteobot.service -f
```

Processes and ports:

```bash
ps aux | grep -E "meteo_bot|wa_server|wa_bridge" | grep -v grep
ss -ltnp | grep -E ":5000|:3100"
```

Final test: `sudo reboot`, and after the Pi comes back (with no terminal open) send
`help` on Telegram and/or WhatsApp — it should reply.

---

## 9. Stop / start / restart

```bash
# stop
sudo systemctl stop meteobot.service
sudo systemctl stop wa-server.service wa-bridge.service

# start
sudo systemctl start meteobot.service wa-server.service wa-bridge.service

# restart (after editing a .py/.js file)
sudo systemctl restart wa-server.service wa-bridge.service

# disable auto-start on boot (without deleting the service)
sudo systemctl disable meteobot.service
```

After any change to the `.service` files:

```bash
sudo systemctl daemon-reload
sudo systemctl restart <service-name>
```

### Uninstall

To remove the services and everything the installer generated (token, WhatsApp
session, saved state, `node_modules`):

```bash
cd ~/OpenMeteoBOT
chmod +x uninstall.sh
./uninstall.sh
```

It asks for confirmation, and can optionally delete the source folder too. It does
**not** remove system packages (Node.js, Python, pip modules).

---

## 10. Re-scan the WhatsApp QR (when needed)

If WhatsApp logs you out (you removed the device on the phone, session expired),
systemd can't display a QR (no terminal). Re-scan manually once:

```bash
sudo systemctl stop wa-bridge.service
cd ~/OpenMeteoBot
node wa_bridge.js          # the QR appears, scan it
# after "WhatsApp connected." -> Ctrl+C
sudo systemctl start wa-bridge.service
```

If the session is corrupted, delete it and scan again:

```bash
rm -rf ~/OpenMeteoBot/wa_auth
```

---

## 11. Bot commands (Telegram and WhatsApp)

| Command | What it does |
|---|---|
| `help` | Full command list |
| `Orsova` | 24-hour hourly forecast (just type the place name) |
| `44.816,29.879` | Forecast by coordinates |
| `Orsova 3` | 3-day forecast (up to 16) |
| `3` | 3-day forecast for all saved locations |
| `clad` | Partial name matches a saved location (Cladova) |
| `soil Orsova` | Soil moisture + temperature, now |
| `air Orsova` | Air quality — European AQI + PM2.5/PM10/O₃/NO₂/SO₂/CO + UV, each explained in plain language |
| `flood Orsova [days]` | River-discharge (flood) forecast, GloFAS |
| `marine Constanta` | Sea state — wave height/period/direction, swell, wind waves, sea-surface temperature |
| `hist Orsova 2025-07-01 2025-07-10` | Past weather for a period |
| `radar` | National radar image from ANM, over a faded map |
| `sat Orsova` | Cloud cover (Open-Meteo) over a faded map |
| `map Orsova` | Clouds + radar combined |
| `mapset` | Map settings (radar source, dim, cloud colour/opacity, zoom, time zone) |
| `alarm 1 21:05` / `alarm off` | Daily 24h forecast for a saved slot at a set time |
| `interval 10` | Set how often alerts are checked, in minutes (global) |
| `status` | Health report: bot core, WhatsApp link, interval, this chat's settings |
| `restart <system password>` | Restart all services (uses your system password via sudo; nothing stored) |
| `model` / `model iconeu` | Show / set the weather model |
| `save 1 Orsova` | Save a location in slot 1 |
| `locs` | List saved locations |
| `del 1` | Delete the location in slot 1 |
| `alerts` | Check saved locations now |
| `set gust 70` | Adjust alert thresholds |
| `units temp F` | Set units (temp/wind/rain/pressure) |
| `lang ro` / `lang en` | Switch language (auto-detected from the phone on Telegram) |
| `anm nowcasting,general` / `anm off` | Official ANM warnings by exact point (nowcasting and/or county-level) |

Alerts for saved locations are sent **automatically** (checked every 15 min by
default; change it live with `interval 10` — minutes — or the `TG_ALERT_INTERVAL`
env var). Both Open-Meteo threshold alerts and ANM warnings use this same interval.
Identical ANM warnings are de-duplicated by content, so the same bulletin is not
resent on every cycle.

### Maps (`radar` / `sat` / `map`) and `mapset`

- **`radar`** shows the **national radar image published by ANM** (in-country
  radars) stretched onto a faded OpenStreetMap base so borders line up. Add a
  location (`radar Orsova`) to drop a marker. Needs Pillow.
  **ANM's official colour scale** (reflectivity in dBZ) is fetched from their radar
  page and drawn under the map, so the colours match the source exactly. Disable
  with `TG_ANM_SCALE=0`.
- **Colour bars.** ANM images carry ANM's own scale. RainViewer publishes no scale
  image, so `map` (and `radar` with the RainViewer source) get a **generated bar**
  drawn under the image, labelled *indicative* — it shows the intensity order
  (light → extreme), not exact thresholds.
- **`sat Orsova`** shows **cloud cover** from Open-Meteo (grey = cloudier). The
  free RainViewer tier has no satellite, so cloud cover is used instead.
- **`map Orsova`** overlays clouds + radar on one image.
- **`mapset`** (alias `hartaset`) tunes the maps per chat and persists it:

  | Setting | Example | Meaning |
  |---|---|---|
  | radar source | `mapset radar anm` / `mapset radar rainviewer` | ANM national image, or RainViewer tiles |
  | base fade | `mapset dim 0.55` | 0 = full-colour map, 1 = white-out |
  | cloud colour | `mapset cloud 105,105,105` | RGB of the cloud shading |
  | cloud opacity | `mapset alpha 225` | 0..255 at 100% overcast |
  | zoom | `mapset zoom 6` | RainViewer map zoom (3..7) |
  | **time zone** | `mapset tz Europe/Bucharest` / `mapset tz +3` / `mapset tz auto` | Local time on map captions (default: server time) |
  | reset | `mapset reset` | Back to defaults |

  The ANM radar image geo-bounds are fixed in the code (correct national extent),
  overridable only via the `TG_ANM_BBOX` env var if ever needed.

### Health monitoring & `status`

- **`status`** (aliases `sysstatus`, `sys`) prints an on-demand health report:
  bot core, WhatsApp link state (connected / disconnected / logged out / bridge
  down, with heartbeat age), the alert interval, and this chat's saved
  locations / alarms / ANM feeds / map time zone.
- **Automatic Telegram alert when WhatsApp breaks.** The Node bridge writes a
  heartbeat (`wa_status.json`); the Telegram bot watches it and messages
  `TG_ADMIN_CHAT` if WhatsApp is **logged out**, **disconnected**, or the **bridge
  is down/hung** (no heartbeat), then a "back online" note on recovery. Set
  `TG_ADMIN_CHAT` to your Telegram ID (or rely on the first `TG_ALLOWED_USERS`);
  without it, monitoring is off. Both services must share the same working folder.

### Remote restart (`restart`)

`restart <your system password>` (alias `restartsys`) restarts `meteobot.service`,
`wa-server.service` and `wa-bridge.service` from a chat message — handy after a
`git pull`. It survives restarting its own service (runs detached with
`--no-block`).

- **How the password is checked.** It **fails closed** — it never restarts unless
  the password is actually verified. Two modes:
  - **System password (nothing stored):** verified via **PAM**, so it works even
    when `sudo` is passwordless (common on Raspberry Pi, where `sudo` would
    otherwise accept any password). Install it once:

    ```bash
    pip install python-pam --break-system-packages
    ```

    Without it, `restart` refuses to run (it will not restart on an unverified
    password) and tells you to install PAM or set a dedicated password.
  - **Dedicated password (no PAM needed):** set one in `meteobot.env` (same folder,
    no `/etc` editing):

    ```bash
    echo 'TG_RESTART_PASSWORD=your-secret' >> ~/OpenMeteoBOT/meteobot.env
    ```

    If set, this takes precedence and the system password isn't used.
- Restart the units via `TG_RESTART_UNITS` if your service names differ. The actual
  restart uses your existing `sudo` (passwordless on most Pis; otherwise the typed
  password is piped to `sudo -S`).

> ⚠️ **Security:** the password travels inside a chat message (Telegram/WhatsApp).
> The bot redacts it from its own logs, but this is your **root-capable system
> password** — anyone who can read that chat could gain full server access. Use it
> only on trusted accounts; if that worries you, create a dedicated low-privilege
> sudo user for the bot instead.

### Daily forecast alarm (`alarm`)

Send the 24h forecast for a saved location automatically at a fixed time:

- `alarm 1 21:05` — slot 1, every day at 21:05 (server local time)
- `alarm 2 08:00` — a second alarm; one per saved slot
- `alarm 1 off` — clear slot 1; `alarm off` — clear all; `alarm` — list

**ANM official warnings.** Each saved location is also checked against Romania's
official ANM warnings (meteoromania.ro) using **point-in-polygon** on the exact
coordinates — so you get the warning only if your point actually falls inside a
warned area. Two feeds are available: `nowcasting` (immediate, short-lived) and
`general` (county-level cod galben/portocaliu/roșu). Choose per chat with
`anm nowcasting,general`, `anm nowcasting`, or turn it off with `anm off`. The
warning text and color come straight from ANM.

---

## 12. Quick troubleshooting

| Symptom | Cause / fix |
|---|---|
| `EADDRINUSE :3100` | Port in use — `pkill -f wa_bridge.js`, or change `WA_PORT`. |
| `ECONNREFUSED 127.0.0.1:5000` | `wa_server.py` isn't running — start the Python service. |
| `Cannot find module '@whiskeysockets/baileys'` | `npm install` wasn't run in the right folder. |
| Bridge loops "Connection closed" | Old Node (<20), or wrong Pi clock (`date`), or delete `wa_auth` and re-scan. |
| Bot doesn't reply on WhatsApp | You message from the linked number (ignored) — use another number; or `WA_ALLOWED` blocks it. |
| WhatsApp goes silent after a while (services still "running") | The Baileys socket died silently. The bridge now has a watchdog that probes every ~1 min and auto-reconnects; if it can't (logged out), the log shows `loggedOut` — re-scan the QR. |
| Bot doesn't reply on Telegram | Two instances on the same token — keep only the service. |
| Wrong date/time on the Pi | `sudo timedatectl set-ntp true` (needed for the WhatsApp TLS handshake). |

---

## 13. Environment variables (reference)

| Variable | Used by | Default | Role |
|---|---|---|---|
| `TG_BOT_TOKEN` | meteo_bot | — | Telegram token (@BotFather) |
| `TG_ALLOWED_USERS` | meteo_bot | empty | Allowed Telegram IDs (empty = anyone) |
| `TG_ADMIN_CHAT` | meteo_bot | first allowed user | Telegram chat that gets WhatsApp-down alerts |
| `WA_STATUS_FILE` | both | `wa_status.json` | WhatsApp heartbeat file (bridge writes, bot reads) |
| `WA_HEARTBEAT_STALE` | meteo_bot | `300` | Seconds without heartbeat before "bridge down" alert |
| `TG_RESTART_PASSWORD` | meteo_bot | unset | Optional dedicated password for `restart` (else the system password via PAM) |
| `TG_RESTART_UNITS` | meteo_bot | the 3 services | systemd units `restart` restarts |
| `TG_BOT_STATE` | both Python | `bot_state.json` | State file |
| `TG_ALERT_INTERVAL` | both Python | `900` | Alert check interval (seconds; 900 = 15 min) |
| `TG_ANM` | both Python | `1` | ANM official warnings on/off globally (`0` = off) |
| `TG_ANM_FEEDS` | both Python | `nowcasting,general` | Default ANM feeds for new chats (per-chat override via `anm`) |
| `TG_MAP_ZOOM` | meteo_bot | `6` | RainViewer map zoom (3..7) |
| `TG_MAP_W` / `TG_MAP_H` | meteo_bot | `720` | RainViewer map size (px) |
| `TG_MAP_BASE_DIM` | meteo_bot | `0.55` | Default base-map fade (0..1) |
| `TG_CLOUD_RGB` | meteo_bot | `105,105,105` | Cloud shading colour |
| `TG_CLOUD_ALPHA` | meteo_bot | `225` | Cloud opacity at 100% overcast (0..255) |
| `TG_CLOUD_COLS` / `TG_CLOUD_ROWS` | meteo_bot | `14` / `12` | Cloud sampling grid |
| `TG_ANM_BBOX` | meteo_bot | `17.9727,42.0465,31.4767,49.1441` | Geo bounds (W,S,E,N) of the ANM radar image |
| `TG_ANM_MAP_W` | meteo_bot | `1000` | ANM radar output width (px) |
| `TG_ANM_SCALE` | meteo_bot | `1` | Draw ANM's official dBZ colour scale under the radar map (`0` = off) |
| `TG_ANM_SCALE_URL` | meteo_bot | ANM `sclrZ.png` | URL of ANM's colour-scale image |
| `TG_ANM_RADAR_URL` | meteo_bot | meteoromania.ro URL | ANM radar image URL template |
| `TG_ANM_RADAR_OFFSET` / `TG_ANM_RADAR_LOOKBACK` | meteo_bot | `1` / `9` | ANM radar timestamp probing (minute offset / slots back) |
| `PY_PORT` | wa_server | `5000` | Python service port |
| `WA_SEND_URL` | wa_server | `http://127.0.0.1:3000/send` | Where alerts are sent (Node bridge) |
| `WA_PORT` | wa_bridge | `3000` | Node bridge port |
| `PY_URL` | wa_bridge | `http://127.0.0.1:5000/incoming` | Where incoming messages are sent |
| `WA_ALLOWED` | wa_bridge | empty | Allowed WhatsApp numbers (empty = anyone) |
| `WA_STALE_MS` | wa_bridge | `180000` | Idle time (ms) before the watchdog probes/reconnects WhatsApp |

> In our setup we used `WA_PORT=3100` (port 3000 was busy), so `WA_SEND_URL` must be
> `http://127.0.0.1:3100/send`.

---

## Notes

- **Data source:** [Open-Meteo](https://open-meteo.com) — free for non-commercial use.
- **WhatsApp** uses an unofficial WhatsApp Web client (Baileys); use at your own risk,
  ideally with a secondary number.
- Do **not** commit secrets or runtime state to git — see `.gitignore`
  (`meteobot.env`, `bot_state.json`, `wa_state.json`, `wa_auth/`, `node_modules/`).
