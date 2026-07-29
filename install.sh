#!/usr/bin/env bash
# =============================================================================
#  OpenMeteoBot - interactive installer (Telegram / WhatsApp / both)
#
#  Run it from the folder that contains meteo_bot.py, wa_server.py,
#  wa_bridge.js and package.json:
#      chmod +x install.sh
#      ./install.sh
#
#  It will: install dependencies, ask which service(s) you want, set the
#  Telegram token, link WhatsApp via QR, and create systemd services that
#  start on boot.
# =============================================================================
set -euo pipefail

cd "$(cd "$(dirname "$0")" && pwd)"
DIR="$PWD"
USR="$(whoami)"

# ---------- helpers ----------------------------------------------------------
c_info()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
c_ok()    { printf '\033[1;32m[ok]\033[0m %s\n' "$*"; }
c_warn()  { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }
c_err()   { printf '\033[1;31m[x]\033[0m %s\n' "$*"; }
die()     { c_err "$*"; exit 1; }

have()    { command -v "$1" >/dev/null 2>&1; }

port_free() { ! ss -ltn 2>/dev/null | grep -q ":$1 "; }
pick_port() {
  local cand
  for cand in 3100 3200 3300 3400 8080; do
    if port_free "$cand"; then echo "$cand"; return; fi
  done
  echo 3100
}

APT_UPDATED=0
apt_install() {
  if [ "$APT_UPDATED" = 0 ]; then sudo apt-get update -y; APT_UPDATED=1; fi
  sudo apt-get install -y "$@"
}

# ---------- 0. intro ---------------------------------------------------------
c_info "OpenMeteoBot installer"
echo "Folder: $DIR"
echo "User:   $USR"

# ---------- 1. choose services ----------------------------------------------
c_info "What do you want to install?"
echo "  1) Telegram only"
echo "  2) WhatsApp only"
echo "  3) Both"
read -rp "Choose [1/2/3]: " CH
case "$CH" in
  1) TELE=1; WA=0 ;;
  2) TELE=0; WA=1 ;;
  3) TELE=1; WA=1 ;;
  *) die "Invalid choice." ;;
esac

# ---------- 2. check required files -----------------------------------------
c_info "Checking required files"
[ -f "$DIR/meteo_bot.py" ] || die "meteo_bot.py is missing from this folder."
if [ "$WA" = 1 ]; then
  for f in wa_server.py wa_bridge.js package.json; do
    [ -f "$DIR/$f" ] || die "$f is missing from this folder."
  done
fi
c_ok "All files present."

# ---------- 3. python dependencies ------------------------------------------
c_info "Installing Python dependencies"
have python3 || die "python3 is not installed."
have pip3 || python3 -m pip --version >/dev/null 2>&1 || apt_install python3-pip
PYDEPS="requests pillow python-pam"      # pillow: map images; python-pam: restartsys
[ "$WA" = 1 ] && PYDEPS="requests pillow python-pam flask"
python3 -m pip install --break-system-packages $PYDEPS
c_ok "Python: $PYDEPS installed."

# ---------- 4. node (only for WhatsApp) -------------------------------------
if [ "$WA" = 1 ]; then
  c_info "Checking Node.js (v20+ required)"
  NEED_NODE=1
  if have node; then
    NODE_MAJOR="$(node -v | sed 's/v\([0-9]*\).*/\1/')"
    if [ "${NODE_MAJOR:-0}" -ge 20 ]; then NEED_NODE=0; fi
  fi
  if [ "$NEED_NODE" = 1 ]; then
    c_warn "Node is missing or below v20 - installing it via NodeSource."
    have curl || apt_install curl
    curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
    apt_install nodejs
  fi
  c_ok "Node $(node -v) present."

  c_info "Installing Node dependencies (npm install)"
  ( cd "$DIR" && npm install )
  c_ok "Node dependencies installed."
fi

PY="$(command -v python3)"
NODE="$(command -v node || true)"

# ---------- 5. Telegram token -----------------------------------------------
if [ "$TELE" = 1 ]; then
  c_info "Telegram setup"
  echo "Create a bot with @BotFather (/newbot) and copy the token."
  read -rp "Telegram token: " TG_TOKEN
  [ -n "$TG_TOKEN" ] || die "The token cannot be empty."
  echo "TG_BOT_TOKEN=$TG_TOKEN" > "$DIR/meteobot.env"
  read -rp "Restrict to specific Telegram IDs? (empty = anyone): " TG_USERS
  [ -n "$TG_USERS" ] && echo "TG_ALLOWED_USERS=$TG_USERS" >> "$DIR/meteobot.env"
  chmod 600 "$DIR/meteobot.env"
  c_ok "Token saved to meteobot.env."
fi

# ---------- 6. WhatsApp options + QR link -----------------------------------
WA_PORT=3100
if [ "$WA" = 1 ]; then
  c_info "WhatsApp setup"
  c_warn "WhatsApp via Baileys is unofficial and the number can be banned. Use a secondary number if possible."
  echo "Who can use the WhatsApp bot?"
  echo "  1) Anyone"
  echo "  2) Only specific numbers"
  read -rp "Choose [1/2]: " WA_ACCESS
  if [ "$WA_ACCESS" = "2" ]; then
    read -rp "Allowed numbers, comma-separated (digits only, with country code, no +): " WA_ALLOWED_VAL
  else
    WA_ALLOWED_VAL=""
    c_ok "WhatsApp open to anyone."
  fi
  WA_PORT="$(pick_port)"
  c_ok "Using port $WA_PORT for the bridge."

  # stop any manual/old instances so ports are free
  pkill -f wa_bridge.js 2>/dev/null || true

  c_info "Scan the QR code (WhatsApp -> Settings -> Linked devices -> Link a device)"
  LOG="$(mktemp)"
  set +e
  WA_PORT="$WA_PORT" WA_ALLOWED="${WA_ALLOWED_VAL:-}" PY_URL="http://127.0.0.1:5000/incoming" \
    "$NODE" "$DIR/wa_bridge.js" >"$LOG" 2>&1 &
  BR_PID=$!
  tail -n +1 -f "$LOG" & TAIL_PID=$!
  WAITED=0; CONNECTED=0
  while kill -0 "$BR_PID" 2>/dev/null; do
    if grep -q "WhatsApp connected." "$LOG"; then CONNECTED=1; sleep 1; break; fi
    sleep 1; WAITED=$((WAITED+1))
    if [ "$WAITED" -ge 180 ]; then c_warn "Scan timeout (3 minutes)."; break; fi
  done
  kill "$TAIL_PID" 2>/dev/null
  kill "$BR_PID" 2>/dev/null
  wait "$BR_PID" 2>/dev/null
  rm -f "$LOG"
  set -e
  if [ "$CONNECTED" = 1 ]; then
    c_ok "WhatsApp connected (session saved in wa_auth/)."
  else
    c_warn "WhatsApp did not connect now. You can re-scan later (see README, section 10)."
  fi
fi

# ---------- 7. systemd services ---------------------------------------------
c_info "Creating systemd services"
pkill -f meteo_bot.py 2>/dev/null || true
pkill -f wa_server.py 2>/dev/null || true
ENABLE=()

if [ "$TELE" = 1 ]; then
  sudo tee /etc/systemd/system/meteobot.service > /dev/null << EOF
[Unit]
Description=Telegram weather bot (meteo_bot.py)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USR
WorkingDirectory=$DIR
EnvironmentFile=$DIR/meteobot.env
Environment=TG_BOT_STATE=bot_state.json
ExecStart=$PY $DIR/meteo_bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
  ENABLE+=("meteobot.service")
  c_ok "meteobot.service created."
fi

if [ "$WA" = 1 ]; then
  sudo tee /etc/systemd/system/wa-server.service > /dev/null << EOF
[Unit]
Description=WhatsApp weather bot - Python service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USR
WorkingDirectory=$DIR
Environment=TG_BOT_STATE=wa_state.json
Environment=WA_SEND_URL=http://127.0.0.1:$WA_PORT/send
Environment=PY_PORT=5000
ExecStart=$PY $DIR/wa_server.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

  sudo tee /etc/systemd/system/wa-bridge.service > /dev/null << EOF
[Unit]
Description=WhatsApp weather bot - Baileys bridge
After=network-online.target wa-server.service
Wants=network-online.target

[Service]
Type=simple
User=$USR
WorkingDirectory=$DIR
Environment=WA_PORT=$WA_PORT
Environment=PY_URL=http://127.0.0.1:5000/incoming
Environment=WA_ALLOWED=${WA_ALLOWED_VAL:-}
ExecStart=$NODE $DIR/wa_bridge.js
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
  ENABLE+=("wa-server.service" "wa-bridge.service")
  c_ok "wa-server.service and wa-bridge.service created."
fi

# ---------- 8. enable + start ------------------------------------------------
c_info "Enabling and starting services"
sudo systemctl daemon-reload
sudo systemctl enable --now "${ENABLE[@]}"

# ---------- 9. summary -------------------------------------------------------
c_info "Done! Service status:"
sudo systemctl --no-pager --lines=0 status "${ENABLE[@]}" || true

echo
c_ok "Installation complete."
echo "Send \"help\" to the bot:"
[ "$TELE" = 1 ] && echo "  - Telegram: from the Telegram app, to your bot"
[ "$WA" = 1 ]   && echo "  - WhatsApp: from a DIFFERENT number, to the linked number"
echo
echo "Live logs:  journalctl -u <service> -f"
echo "Services:   ${ENABLE[*]}"
