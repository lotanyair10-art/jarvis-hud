#!/data/data/com.termux/files/usr/bin/sh
# Keeps the bridge running: restarts it 30 seconds after any crash.
cd "$HOME/jarvis-bridge"
while true; do
  python -W ignore bridge.py >> bridge.log 2>&1
  tail -c 200000 bridge.log > bridge.log.tmp && mv bridge.log.tmp bridge.log
  sleep 30
done
