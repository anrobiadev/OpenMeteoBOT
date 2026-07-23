# OpenMeteoBot — weather bot for Telegram & WhatsApp

A personal weather bot powered by **Open-Meteo** (free, no API key), running on a
Raspberry Pi / Linux. It provides hourly and multi-day forecasts, soil moisture,
historical data, and **automatic alerts** (wind, rain, snow, heat, frost) for saved
locations. Works on **Telegram** and, optionally, on **WhatsApp**.

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
2. Installs the required dependencies (Python: `requests`/`flask`; for WhatsApp it
   checks/installs **Node 20+** and the Baileys packages via `npm install`).
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
pip install requests flask --break-system-packages
```

(`--break-system-packages` is required on recent Raspberry Pi OS / Debian.)

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
| `wx Orsova` | 24-hour hourly forecast |
| `wx 44.816,29.879` | Forecast by coordinates |
| `wx Orsova 3` | 3-day forecast (up to 16) |
| `wx 7` | 7-day forecast for all saved locations |
| `soil Orsova` | Soil moisture + temperature, now |
| `hist Orsova 2025-07-01 2025-07-10` | Past weather for a period |
| `model` / `model iconeu` | Show / set the weather model |
| `save 1 Orsova` | Save a location in slot 1 |
| `locs` | List saved locations |
| `del 1` | Delete the location in slot 1 |
| `alerts` | Check saved locations now |
| `set gust 70` | Adjust alert thresholds |
| `units temp F` | Set units (temp/wind/rain/pressure) |
| `lang ro` / `lang en` | Switch language (auto-detected from the phone on Telegram) |

Alerts for saved locations are sent **automatically** (checked every 30 min).

---

## 12. Quick troubleshooting

| Symptom | Cause / fix |
|---|---|
| `EADDRINUSE :3100` | Port in use — `pkill -f wa_bridge.js`, or change `WA_PORT`. |
| `ECONNREFUSED 127.0.0.1:5000` | `wa_server.py` isn't running — start the Python service. |
| `Cannot find module '@whiskeysockets/baileys'` | `npm install` wasn't run in the right folder. |
| Bridge loops "Connection closed" | Old Node (<20), or wrong Pi clock (`date`), or delete `wa_auth` and re-scan. |
| Bot doesn't reply on WhatsApp | You message from the linked number (ignored) — use another number; or `WA_ALLOWED` blocks it. |
| Bot doesn't reply on Telegram | Two instances on the same token — keep only the service. |
| Wrong date/time on the Pi | `sudo timedatectl set-ntp true` (needed for the WhatsApp TLS handshake). |

---

## 13. Environment variables (reference)

| Variable | Used by | Default | Role |
|---|---|---|---|
| `TG_BOT_TOKEN` | meteo_bot | — | Telegram token (@BotFather) |
| `TG_ALLOWED_USERS` | meteo_bot | empty | Allowed Telegram IDs (empty = anyone) |
| `TG_BOT_STATE` | both Python | `bot_state.json` | State file |
| `TG_ALERT_INTERVAL` | both Python | `1800` | Alert check interval (seconds) |
| `PY_PORT` | wa_server | `5000` | Python service port |
| `WA_SEND_URL` | wa_server | `http://127.0.0.1:3000/send` | Where alerts are sent (Node bridge) |
| `WA_PORT` | wa_bridge | `3000` | Node bridge port |
| `PY_URL` | wa_bridge | `http://127.0.0.1:5000/incoming` | Where incoming messages are sent |
| `WA_ALLOWED` | wa_bridge | empty | Allowed WhatsApp numbers (empty = anyone) |

> In our setup we used `WA_PORT=3100` (port 3000 was busy), so `WA_SEND_URL` must be
> `http://127.0.0.1:3100/send`.

---

## Notes

- **Data source:** [Open-Meteo](https://open-meteo.com) — free for non-commercial use.
- **WhatsApp** uses an unofficial WhatsApp Web client (Baileys); use at your own risk,
  ideally with a secondary number.
- Do **not** commit secrets or runtime state to git — see `.gitignore`
  (`meteobot.env`, `bot_state.json`, `wa_state.json`, `wa_auth/`, `node_modules/`).
