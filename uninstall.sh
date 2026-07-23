#!/usr/bin/env bash
# =============================================================================
#  OpenMeteoBot - uninstaller
#
#  Stops and removes the systemd services and everything the installer
#  generated (token, WhatsApp session, saved state, node_modules).
#  Optionally also deletes the source files.
#
#  Run from the bot folder:
#      chmod +x uninstall.sh
#      ./uninstall.sh
#
#  Note: it does NOT remove system packages (Node.js, Python, pip modules),
#  since other programs may depend on them.
# =============================================================================
set -uo pipefail   # not -e: missing services must not abort the cleanup

cd "$(cd "$(dirname "$0")" && pwd)"
DIR="$PWD"

c_info()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
c_ok()    { printf '\033[1;32m[ok]\033[0m %s\n' "$*"; }
c_warn()  { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }

c_info "OpenMeteoBot uninstaller"
echo "Folder: $DIR"
c_warn "This will stop and remove the bot services and generated data"
c_warn "(Telegram token, WhatsApp session, saved locations/preferences)."
read -rp "Continue? [y/N]: " CONF
case "$CONF" in
  [Yy]*) : ;;
  *) echo "Aborted."; exit 0 ;;
esac

# ---------- stop, disable and remove services -------------------------------
c_info "Removing systemd services"
for s in meteobot wa-server wa-bridge; do
  sudo systemctl disable --now "$s.service" 2>/dev/null || true
  sudo rm -f "/etc/systemd/system/$s.service"
done
sudo systemctl daemon-reload
sudo systemctl reset-failed 2>/dev/null || true
c_ok "Services stopped and removed."

# ---------- kill leftover manual processes ----------------------------------
pkill -f meteo_bot.py 2>/dev/null || true
pkill -f wa_server.py 2>/dev/null || true
pkill -f wa_bridge.js 2>/dev/null || true

# ---------- remove generated files ------------------------------------------
c_info "Removing generated files"
rm -rf "$DIR/wa_auth" "$DIR/node_modules" "$DIR/__pycache__"
rm -f  "$DIR/bot_state.json" "$DIR/wa_state.json" "$DIR"/*.tmp \
       "$DIR/meteobot.env" "$DIR/package-lock.json"
c_ok "Removed token, WhatsApp session, state files and node_modules."

# ---------- optional: delete the source files too ---------------------------
echo
read -rp "Also delete the source files (the whole $DIR folder)? [y/N]: " WIPE
case "$WIPE" in
  [Yy]*)
    read -rp "Type DELETE to confirm full removal: " C2
    if [ "$C2" = "DELETE" ]; then
      cd "$HOME"
      rm -rf "$DIR"
      c_ok "Folder $DIR removed."
    else
      c_warn "Full removal cancelled; source files kept."
    fi
    ;;
  *)
    c_info "Source files kept. Reinstall anytime with: ./install.sh"
    ;;
esac

echo
c_ok "Uninstall complete."
