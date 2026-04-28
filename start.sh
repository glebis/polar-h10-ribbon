#!/bin/bash
# Start the full HRV biofeedback stack.
# All services are accessible over Tailscale automatically.
set -e
cd "$(dirname "$0")"

# colors
G='\033[0;32m'; Y='\033[0;33m'; C='\033[0;36m'; R='\033[0m'; B='\033[1m'

echo ""
echo -e "${B}  ♥  polar-h10-ribbon${R}"
echo ""

# activate venv
source .venv/bin/activate 2>/dev/null || { echo "run: python3 -m venv .venv && pip install -r requirements.txt"; exit 1; }

# get tailscale hostname
TS_HOST=$(tailscale status --json 2>/dev/null | python3 -c "import json,sys; print(json.load(sys.stdin)['Self']['DNSName'].rstrip('.'))" 2>/dev/null || echo "localhost")

# interactive mode selection
echo -e "  ${B}What do you want to run?${R}"
echo ""
echo -e "  ${G}1${R}  full stack    (bridge + lights + dashboard + protocol API)"
echo -e "  ${G}2${R}  live session  (relay + web server — for group meditation)"
echo -e "  ${G}3${R}  assessment    (bridge + web server — for HRV testing)"
echo -e "  ${G}4${R}  zone 2 coach  (bridge + voice coach — kettlebell/exercise)"
echo -e "  ${G}5${R}  everything    (all of the above)"
echo ""
read -p "  choice [1-5]: " choice

# kill any existing processes
pkill -f 'python bridge.py' 2>/dev/null || true
pkill -f 'python hrv_lights.py' 2>/dev/null || true
pkill -f 'python relay.py' 2>/dev/null || true
pkill -f 'python protocol_api.py' 2>/dev/null || true
pkill -f 'python zone2_coach.py' 2>/dev/null || true
pkill -f 'python3 -m http.server 8080' 2>/dev/null || true
sleep 1

start_web() {
  python3 -m http.server 8080 --bind 0.0.0.0 &>/dev/null &
  echo -e "  ${G}✓${R} web server     ${C}http://${TS_HOST}:8080${R}"
}

start_bridge() {
  echo -e "  ${Y}…${R} scanning for Polar H10 (10s)…"
  python bridge.py &>/dev/null &
  sleep 12
  if lsof -i :8765 2>/dev/null | grep -q LISTEN; then
    echo -e "  ${G}✓${R} bridge         ws://localhost:8765"
  else
    echo -e "  ${R}✗${R} bridge failed — check strap"
  fi
}

start_lights() {
  python hrv_lights.py --preset candle &>/dev/null &
  sleep 3
  echo -e "  ${G}✓${R} lights daemon  candle preset"
}

start_relay() {
  python relay.py --port 9000 &>/dev/null &
  sleep 1
  echo -e "  ${G}✓${R} relay server   ws://${TS_HOST}:9000"
}

start_protocol() {
  python protocol_api.py &>/dev/null &
  sleep 1
  echo -e "  ${G}✓${R} protocol API   http://localhost:8090"
}

start_coach() {
  read -p "  warm-up min [3]: " wu; wu=${wu:-3}
  read -p "  zone 2 min [15]: " dur; dur=${dur:-15}
  read -p "  cool-down min [3]: " cd; cd=${cd:-3}
  python zone2_coach.py --age 42 --resting-hr 65 --warmup $wu --duration $dur --cooldown $cd &
  echo -e "  ${G}✓${R} zone 2 coach   ${wu}+${dur}+${cd} min"
}

echo ""

case $choice in
  1)
    start_web
    start_bridge
    start_lights
    start_protocol
    ;;
  2)
    start_web
    start_relay
    echo ""
    echo -e "  ${B}Share with participants:${R}"
    echo -e "  ${C}http://${TS_HOST}:8080/join.html${R}"
    echo ""
    echo -e "  ${B}Open visualization:${R}"
    echo -e "  ${C}http://${TS_HOST}:8080/multiplayer.html${R}"
    ;;
  3)
    start_web
    start_bridge
    echo ""
    echo -e "  ${B}Open:${R}"
    echo -e "  ${C}http://${TS_HOST}:8080/assess.html${R}"
    ;;
  4)
    start_bridge
    start_coach
    ;;
  5)
    start_web
    start_bridge
    start_lights
    start_relay
    start_protocol
    echo ""
    echo -e "  ${B}Pages:${R}"
    echo -e "  ${C}http://${TS_HOST}:8080${R}              ribbon"
    echo -e "  ${C}http://${TS_HOST}:8080/coherence.html${R}  breathing"
    echo -e "  ${C}http://${TS_HOST}:8080/heartbeat.html${R}  audio"
    echo -e "  ${C}http://${TS_HOST}:8080/assess.html${R}     assessment"
    echo -e "  ${C}http://${TS_HOST}:8080/protocol.html${R}   protocol"
    echo -e "  ${C}http://${TS_HOST}:8080/join.html${R}       live session"
    ;;
  *)
    echo "  invalid choice"
    exit 1
    ;;
esac

echo ""
echo -e "  ${Y}Press Ctrl+C to stop all services${R}"
echo ""

# wait and clean up on exit
trap 'echo ""; echo "  shutting down…"; pkill -f "python bridge.py" 2>/dev/null; pkill -f "python hrv_lights.py" 2>/dev/null; pkill -f "python relay.py" 2>/dev/null; pkill -f "python protocol_api.py" 2>/dev/null; pkill -f "python zone2_coach.py" 2>/dev/null; pkill -f "python3 -m http.server 8080" 2>/dev/null; echo "  done."; exit 0' INT TERM

wait
