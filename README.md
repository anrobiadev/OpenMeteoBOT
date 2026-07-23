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
# copiază aici: meteo_bot.py, wa_server.py, wa_bridge.js, package.json
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
