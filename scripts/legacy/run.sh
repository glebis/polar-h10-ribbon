#!/bin/bash
# Auto-restart wrapper for bridge + daemon
# Usage: ./run.sh [--lights 4,3,2] [--mode monitor]
cd "$(dirname "$0")"
source .venv/bin/activate

LIGHTS="${1:-4,3,2}"
MODE="${2:-monitor}"

cleanup() {
    echo "Stopping..."
    kill $BRIDGE_PID $DAEMON_PID 2>/dev/null
    exit 0
}
trap cleanup INT TERM

while true; do
    echo "[$(date +%H:%M)] Starting bridge..."
    python bridge.py &
    BRIDGE_PID=$!
    sleep 5

    echo "[$(date +%H:%M)] Starting daemon (mode=$MODE, lights=$LIGHTS)..."
    python hrv_daemon.py --lights "$LIGHTS" --mode "$MODE" --db hrv_data.db &
    DAEMON_PID=$!

    # Wait for either to exit
    wait -n $BRIDGE_PID $DAEMON_PID 2>/dev/null

    echo "[$(date +%H:%M)] Process exited. Restarting in 5s..."
    kill $BRIDGE_PID $DAEMON_PID 2>/dev/null
    sleep 5
done
