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

The easiest way: an **interactive script** that does everything automatically.

```bash
cd ~/OpenMeteoBot
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




ROMANIAN

# OpenMeteoBot — bot meteo pentru Telegram și WhatsApp

Bot personal de vreme bazat pe **Open-Meteo** (gratuit, fără cheie API), care rulează
pe Raspberry Pi / Linux. Oferă prognoză orară și pe mai multe zile, umiditatea solului,
date istorice și **alerte automate** (vânt, ploaie, ninsoare, caniculă, îngheț) pentru
locații salvate. Funcționează pe **Telegram** și, opțional, pe **WhatsApp**.

---

## 1. Cum e construit

Trei fișiere, două „lumi":

| Fișier | Rol | Rulează pe |
|---|---|---|
| `meteo_bot.py` | Botul de **Telegram** + toată logica meteo (comenzi, alerte, Open-Meteo) | Python |
| `wa_server.py` | Serviciu care **refolosește** `meteo_bot.py` și expune logica pentru WhatsApp | Python (port 5000) |
| `wa_bridge.js` | Puntea către **WhatsApp** (Baileys / WhatsApp Web neoficial) | Node.js (port 3100) |
| `package.json` | Dependențele Node pentru punte | — |
| `install.sh` | Instalator interactiv (dependențe, token, QR, servicii) | Bash |

Fluxul pe WhatsApp:

```
WhatsApp  <->  wa_bridge.js (Node, :3100)  <->  wa_server.py (Python, :5000)  ->  meteo_bot.py
```

Fișiere de stare (se creează singure, țin locațiile/preferințele/alertele trimise):

- `bot_state.json` — pentru Telegram
- `wa_state.json` — pentru WhatsApp (separat, ca să nu se amestece)
- `wa_auth/` — sesiunea WhatsApp (după ce scanezi QR-ul o dată)

> **Notă:** Telegram și WhatsApp pot rula în paralel. Poți folosi doar Telegram,
> doar WhatsApp, sau ambele.

---

## Instalare rapidă (recomandat) — `install.sh`

Cel mai simplu mod: un **script interactiv** care face tot automat.

```bash
cd ~/OpenMeteoBot
chmod +x install.sh
./install.sh
```

Ce face, pas cu pas:

1. Te întreabă ce vrei: **Telegram**, **WhatsApp** sau **ambele**.
2. Instalează dependențele necesare (Python: `requests`/`flask`; iar pentru WhatsApp
   verifică/instalează **Node 20+** și pachetele Baileys prin `npm install`).
3. **Telegram:** îți cere tokenul (@BotFather) și îl salvează în `meteobot.env`
   (opțional, și lista de ID-uri permise).
4. **WhatsApp:** îți afișează **codul QR** de scanat și așteaptă conectarea
   (alege singur un port liber pentru punte).
5. Creează și pornește **serviciile systemd**, care pornesc automat la reboot.

La final trimiți `help` botului și gata. Dacă preferi să faci pașii **manual** (sau
pentru depanare), continuă cu secțiunile de mai jos — descriu exact același proces.

---

---

## 2. Cerințe

- Raspberry Pi / Linux (Debian/Ubuntu/Raspberry Pi OS)
- **Python 3** (vine preinstalat)
- **Node.js 20+** (obligatoriu pentru WhatsApp/Baileys) — vezi pasul 4
- Un cont Telegram (pentru token) și/sau un al doilea număr de WhatsApp

---

## 3. Instalarea fișierelor

Pune toate fișierele în **același folder** (aici folosim `~/OpenMeteoBot`):

```bash
mkdir -p ~/OpenMeteoBot
cd ~/OpenMeteoBot
# copiază aici: meteo_bot.py, wa_server.py, wa_bridge.js, package.json, install.sh
```

Dacă le-ai copiat cu `sudo` sau sunt deținute de root, dă-ți drepturi:

```bash
sudo chown -R $USER:$USER ~/OpenMeteoBot
```

---

## 4. Instalarea dependențelor

### Python

```bash
pip install requests flask --break-system-packages
```

(`--break-system-packages` e necesar pe Raspberry Pi OS / Debian recent.)

### Node.js 20+ (doar dacă folosești WhatsApp)

Verifică versiunea:

```bash
node -v
```

Dacă e sub **v20**, actualizează prin NodeSource:

```bash
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt install -y nodejs
node -v      # trebuie v20.x sau mai nou
```

Apoi instalează dependențele Node (în folderul cu `package.json`):

```bash
cd ~/OpenMeteoBot
npm install
```

---

## 5. Botul de Telegram — token

1. În Telegram deschide **@BotFather** → `/newbot` → alege un nume și un username
   care se termină în `bot`. Primești un **token** de forma
   `123456789:AAE...`.

2. Pune tokenul într-un fișier `.env` (mai sigur decât în serviciu):

   ```bash
   cd ~/OpenMeteoBot
   echo 'TG_BOT_TOKEN=123456789:AAE...token-ul-tau' > meteobot.env
   chmod 600 meteobot.env
   ```

   Fără `export`, fără ghilimele — doar `CHEIE=valoare`.

3. (Opțional) Ca botul să răspundă doar ție, adaugă pe linie nouă în `meteobot.env`:

   ```
   TG_ALLOWED_USERS=123456789
   ```

   `123456789` = ID-ul tău Telegram **numeric** (nu numărul de telefon). Îl afli
   scriind lui **@userinfobot**, sau pornind botul o dată și citind în consolă linia
   `[msg] chat_id=... user_id=...`.

### Test rapid (manual)

```bash
cd ~/OpenMeteoBot
export TG_BOT_TOKEN="123456789:AAE...token-ul-tau"
python3 meteo_bot.py
```

Trebuie să scrie `Bot started...`. Scrie-i `help` din Telegram. `Ctrl+C` ca să-l oprești.

---

## 6. WhatsApp — link prin QR

> ⚠️ **Atenție:** WhatsApp prin Baileys se leagă ca un client WhatsApp Web la
> numărul tău și **încalcă termenii WhatsApp** — numărul poate fi **banat**.
> Folosește, dacă poți, un **număr secundar**.

WhatsApp are nevoie de **două** procese pornite (Python + Node). Pentru primul link
le pornim manual, ca să vezi codul QR.

**Terminal 1 — serviciul Python:**

```bash
cd ~/OpenMeteoBot
export TG_BOT_STATE=wa_state.json
export WA_SEND_URL="http://127.0.0.1:3100/send"
python3 wa_server.py
```

Trebuie să vezi `Running on http://127.0.0.1:5000`. Lasă terminalul deschis.

**Terminal 2 — puntea Node:**

```bash
cd ~/OpenMeteoBot
export WA_PORT=3100
# export WA_ALLOWED="407xxxxxxxx"   # pune numărul tău (doar cifre) ca filtru; lasă gol = oricine
node wa_bridge.js
```

Apare un **cod QR** în terminal. În telefon: **WhatsApp → Setări → Dispozitive
conectate → Conectează un dispozitiv** → scanezi QR-ul.

După conectare (`WhatsApp connected.`), scrie-i botului **de pe alt telefon/număr**
mesajul `help`. (Botul **ignoră** mesajele trimise de pe numărul conectat — nu-ți poți
scrie ție însuți.)

Sesiunea se salvează în `wa_auth/`, deci nu mai scanezi QR data viitoare.

---

## 7. Pornire automată la reboot (systemd)

Transformăm procesele în servicii care pornesc la boot și repornesc dacă pică.

Întâi oprește instanțele pornite manual, ca să nu se bată pe porturi:

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

### 7.3 WhatsApp punte — `wa-bridge.service`

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

> Ca să pui filtrul de număr pe WhatsApp, schimbă linia în
> `Environment=WA_ALLOWED=407xxxxxxxx` și rulează
> `sudo systemctl daemon-reload && sudo systemctl restart wa-bridge`.

### 7.4 Activează tot

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now meteobot.service wa-server.service wa-bridge.service
```

Dacă vrei **doar Telegram** sau **doar WhatsApp**, activează doar serviciile respective.

---

## 8. Verificare

Starea serviciilor (trebuie `active (running)`):

```bash
systemctl status meteobot.service wa-server.service wa-bridge.service
```

Log live (înlocuiește cu serviciul dorit):

```bash
journalctl -u wa-bridge.service -f
journalctl -u meteobot.service -f
```

Procese și porturi:

```bash
ps aux | grep -E "meteo_bot|wa_server|wa_bridge" | grep -v grep
ss -ltnp | grep -E ":5000|:3100"
```

Test final: `sudo reboot`, iar după ce revine Pi-ul (fără niciun terminal deschis)
scrie `help` pe Telegram și/sau WhatsApp — trebuie să răspundă.

---

## 9. Oprire / pornire / repornire

```bash
# oprește
sudo systemctl stop meteobot.service
sudo systemctl stop wa-server.service wa-bridge.service

# pornește
sudo systemctl start meteobot.service wa-server.service wa-bridge.service

# repornește (după ce ai modificat un fișier .py/.js)
sudo systemctl restart wa-server.service wa-bridge.service

# dezactivează pornirea la boot (fără să șteargă serviciul)
sudo systemctl disable meteobot.service
```

După orice modificare la fișierele `.service`:

```bash
sudo systemctl daemon-reload
sudo systemctl restart <nume-serviciu>
```

---

## 10. Rescanare QR WhatsApp (când e nevoie)

Dacă WhatsApp te deconectează (ștergi dispozitivul din telefon, sesiune expirată),
systemd nu poate afișa QR (n-are terminal). Rescanezi manual o dată:

```bash
sudo systemctl stop wa-bridge.service
cd ~/OpenMeteoBot
node wa_bridge.js          # apare QR-ul, îl scanezi
# după "WhatsApp connected." -> Ctrl+C
sudo systemctl start wa-bridge.service
```

Dacă sesiunea e coruptă, șterge-o și scanează din nou:

```bash
rm -rf ~/OpenMeteoBot/wa_auth
```

---

## 11. Comenzi bot (Telegram și WhatsApp)

| Comandă | Ce face |
|---|---|
| `help` | Lista completă de comenzi |
| `wx Orsova` | Prognoză orară pe 24h |
| `wx 44.816,29.879` | Prognoză după coordonate |
| `wx Orsova 3` | Prognoză pe 3 zile (până la 16) |
| `wx 7` | Prognoză pe 7 zile pentru toate locațiile salvate |
| `soil Orsova` | Umiditatea solului + temperatură, acum |
| `hist Orsova 2025-07-01 2025-07-10` | Vremea din trecut pe o perioadă |
| `model` / `model iconeu` | Vezi / setează modelul meteo |
| `save 1 Orsova` | Salvează o locație în slotul 1 |
| `locs` | Listează locațiile salvate |
| `del 1` | Șterge locația din slotul 1 |
| `alerts` | Verifică acum locațiile salvate |
| `set gust 70` | Reglează pragurile de alertă |
| `units temp F` | Setează unitățile (temp/wind/rain/pressure) |

Alertele pentru locațiile salvate vin **automat** (verificare la 30 min).

---

## 12. Depanare rapidă

| Simptom | Cauză / soluție |
|---|---|
| `EADDRINUSE :3100` | Portul e ocupat — `pkill -f wa_bridge.js`, sau schimbă `WA_PORT`. |
| `ECONNREFUSED 127.0.0.1:5000` | `wa_server.py` nu rulează — pornește serviciul Python. |
| `Cannot find module '@whiskeysockets/baileys'` | Nu s-a rulat `npm install` în folderul corect. |
| Punte în buclă „Connection closed" | Node vechi (<20), sau ceasul Pi greșit (`date`), sau șterge `wa_auth` și rescanează. |
| Botul nu răspunde pe WhatsApp | Scrii de pe numărul conectat (ignorat) — folosește alt număr; sau `WA_ALLOWED` blochează numărul. |
| Botul nu răspunde pe Telegram | Rulează două instanțe pe același token — lasă doar serviciul. |
| Data/ora greșite pe Pi | `sudo timedatectl set-ntp true` (necesar pentru handshake-ul WhatsApp). |

---

## 13. Variabile de mediu (referință)

| Variabilă | Folosită de | Implicit | Rol |
|---|---|---|---|
| `TG_BOT_TOKEN` | meteo_bot | — | Token Telegram (@BotFather) |
| `TG_ALLOWED_USERS` | meteo_bot | gol | ID-uri Telegram permise (gol = oricine) |
| `TG_BOT_STATE` | ambele Python | `bot_state.json` | Fișierul de stare |
| `TG_ALERT_INTERVAL` | ambele Python | `1800` | Interval verificare alerte (secunde) |
| `PY_PORT` | wa_server | `5000` | Portul serviciului Python |
| `WA_SEND_URL` | wa_server | `http://127.0.0.1:3000/send` | Unde trimite alertele (puntea Node) |
| `WA_PORT` | wa_bridge | `3000` | Portul punții Node |
| `PY_URL` | wa_bridge | `http://127.0.0.1:5000/incoming` | Unde trimite mesajele primite |
| `WA_ALLOWED` | wa_bridge | gol | Numere WhatsApp permise (gol = oricine) |

> În configurația noastră am folosit `WA_PORT=3100` (portul 3000 era ocupat), deci
> `WA_SEND_URL` trebuie să fie `http://127.0.0.1:3100/send`.

---

*Sursă date: [Open-Meteo](https://open-meteo.com) — gratuit pentru uz non-comercial.*
