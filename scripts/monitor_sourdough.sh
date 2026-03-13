#!/usr/bin/env bash
# Launch the Sourdough Live Monitor as a background process
# Logs go to logs/sourdough_live.log

set -e

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LOG_FILE="$REPO_DIR/logs/sourdough_live.log"
PID_FILE="$REPO_DIR/logs/sourdough_monitor.pid"

mkdir -p "$REPO_DIR/logs"

# Kill any previous instance
if [ -f "$PID_FILE" ]; then
  OLD_PID=$(cat "$PID_FILE")
  if kill -0 "$OLD_PID" 2>/dev/null; then
    echo "Stopping previous instance (PID $OLD_PID)…"
    kill "$OLD_PID"
    sleep 1
  fi
fi

echo "Starting Sourdough Monitor…"
cd "$REPO_DIR"
nohup python -m bots.polymarket.monitor_sourdough \
  >> "$LOG_FILE" 2>&1 &
echo $! > "$PID_FILE"
echo "Started PID $(cat "$PID_FILE") — tail -f $LOG_FILE"
