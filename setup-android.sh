#!/data/data/com.termux/files/usr/bin/sh
# Jarvis home bridge – Termux (Android) setup. Contains no secrets: they are typed on the phone.
# Run once in Termux:
#   curl -sL https://raw.githubusercontent.com/lotanyair10-art/jarvis-hud/main/setup-android.sh | sh
set -e
SRC="https://raw.githubusercontent.com/lotanyair10-art/jarvis-hud/main"
DIR="$HOME/jarvis-bridge"
echo ">> Installing Python (a few minutes the first time)..."
pkg update -y >/dev/null 2>&1 || true
pkg install -y python >/dev/null
mkdir -p "$DIR" "$HOME/.termux/boot"
echo ">> Downloading the bridge..."
curl -sL "$SRC/bridge.py?t=$(date +%s)" -o "$DIR/bridge.py"
curl -sL "$SRC/run.sh?t=$(date +%s)" -o "$DIR/run.sh"; chmod +x "$DIR/run.sh"
echo ">> Installing libraries (pure Python, no compiler needed)..."
export AIOHTTP_NO_EXTENSIONS=1 FROZENLIST_NO_EXTENSIONS=1 MULTIDICT_NO_EXTENSIONS=1 YARL_NO_EXTENSIONS=1 PROPCACHE_NO_EXTENSIONS=1
pip install -q --no-binary aiohttp,multidict,yarl,frozenlist,propcache "pyControl4==1.5.0" python-dotenv aiohttp async-timeout
if [ ! -f "$DIR/.env" ]; then
  echo ""
  printf "Control4 email (my.control4.com): "; read C4U < /dev/tty
  printf "Control4 password: "; stty -echo < /dev/tty; read C4P < /dev/tty; stty echo < /dev/tty; echo ""
  printf "Jarvis access code (the bot sent it to you on Telegram): "; read JT < /dev/tty
  umask 077
  cat > "$DIR/.env" <<ENV
C4_USERNAME=$C4U
C4_PASSWORD=$C4P
C4_CONTROLLER_IP=192.168.10.20
JARVIS_URL=https://script.google.com/macros/s/AKfycbxXuof7eOPvrA-_p1tpX5vvnFf2mRz9Kl4HqBu_s0iJBXmtgUXsASODqERca7EbDC0B/exec
JARVIS_TOKEN=$JT
ENV
fi
# start again after a phone restart (only if the Termux:Boot app is installed and was opened once)
printf '#!/data/data/com.termux/files/usr/bin/sh\ntermux-wake-lock\nnohup %s/run.sh >/dev/null 2>&1 &\n' "$DIR" > "$HOME/.termux/boot/jarvis-bridge"
chmod +x "$HOME/.termux/boot/jarvis-bridge"
termux-wake-lock 2>/dev/null || true
pkill -f "run.sh" 2>/dev/null || true
pkill -f "bridge.py" 2>/dev/null || true   # the old bridge keeps running after its watcher stops
sleep 1
nohup "$DIR/run.sh" >/dev/null 2>&1 &
sleep 15
echo ""; tail -3 "$DIR/bridge.log" 2>/dev/null || true
echo ""; echo ">> Done. Jarvis's home bridge is running on this phone. Leave Termux open in the background."
