#!/bin/bash
# Daemonises immediately so container init is not blocked.
# Waits for Wine Python to be ready, then starts RPyC on port 18812
# using the Wine Python that has MetaTrader5 — NOT Linux python3.
(
  export DISPLAY=:1
  LOG=/tmp/rpyc_bridge.log
  WINE_PY='C:\Program Files (x86)\Python39-32\python.exe'

  echo "[rpyc] Waiting for Wine Python..." > "$LOG"

  for i in $(seq 1 120); do
    wine "$WINE_PY" --version >> "$LOG" 2>&1 && break
    sleep 3
  done

  echo "[rpyc] Wine Python ready. Starting RPyC on 0.0.0.0:18812..." >> "$LOG"

  # Retry loop — restart server if it ever crashes
  while true; do
    wine "$WINE_PY" -m rpyc_classic --host 0.0.0.0 -p 18812 >> "$LOG" 2>&1
    echo "[rpyc] Server exited, restarting in 5s..." >> "$LOG"
    sleep 5
  done
) &

exit 0