#!/bin/bash
# Circus-api watchdog: restarts PM2 process if health check fails
HEALTH=$(curl -sf --max-time 5 http://localhost:6200/health 2>/dev/null)
if [ -z "$HEALTH" ]; then
  logger -t circus-watchdog "health check failed, restarting circus-api"
  # Kill any port orphan first
  lsof -ti :6200 | xargs -r kill -9 2>/dev/null
  sleep 1
  pm2 restart circus-api
fi
