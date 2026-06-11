#!/bin/bash
# Lockfile prevents concurrent starts (race condition when PM2 restarts rapidly)
LOCKFILE=/tmp/circus-api.lock
exec 200>"$LOCKFILE"
flock -n 200 || { echo "[circus] Another instance starting, exiting"; exit 1; }

# Wait for port 6200 to free; SIGKILL any orphan after 15s
for i in $(seq 1 15); do
  if ! lsof -i :6200 -t > /dev/null 2>&1; then
    break
  fi
  sleep 1
done
# Force-kill any orphan still holding the port
ORPHAN=$(lsof -ti :6200 2>/dev/null)
if [ -n "$ORPHAN" ]; then
  kill -9 $ORPHAN 2>/dev/null || true
  sleep 1
fi
exec /usr/bin/python3 -m uvicorn circus.app:app --host 127.0.0.1 --port 6200
