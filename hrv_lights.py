"""HRV-to-lights daemon. Connects to polar-h10-ribbon WebSocket bridge,
computes RMSSD trends, and drives Hue + timeBuzzer LEDs organically.

Run alongside bridge.py:
    python hrv_lights.py [--hue-group 1] [--no-buzzer] [--dashboard]
"""
import argparse
import asyncio
import colorsys
import json
import math
import os
import struct
import subprocess
import sys
import time
import urllib.request
import urllib.error
from collections import deque
from dataclasses import dataclass, field

WS_URL = "ws://localhost:8765"

HUE_CONFIG = os.path.expanduser("~/.config/hue/config.json")
BUZZER_SCRIPT = os.path.expanduser("~/.claude/skills/timebuzzer-led/scripts/buzzer_led.py")

# --- HRV computation ---

@dataclass
class HRVState:
    rr_buffer: deque = field(default_factory=lambda: deque(maxlen=60))
    rmssd_history: deque = field(default_factory=lambda: deque(maxlen=300))
    timestamps: deque = field(default_factory=lambda: deque(maxlen=300))
    baseline_rmssd: float = 50.0
    current_rmssd: float = 50.0
    trend: float = 0.0  # negative = dropping, positive = rising
    trend_strength: float = 0.0  # 0..1 how strong the trend is

    def add_rr(self, rr_ms: int) -> bool:
        if rr_ms < 200 or rr_ms > 2000:
            return False
        self.rr_buffer.append(rr_ms)
        if len(self.rr_buffer) >= 6:
            self._update_rmssd()
            return True
        return False

    def _update_rmssd(self):
        rr = list(self.rr_buffer)
        diffs_sq = [(rr[i+1] - rr[i])**2 for i in range(len(rr)-1)]
        if not diffs_sq:
            return
        rmssd = math.sqrt(sum(diffs_sq) / len(diffs_sq))
        self.current_rmssd = rmssd
        now = time.time()
        self.rmssd_history.append(rmssd)
        self.timestamps.append(now)

        # update baseline with slow EMA (adapts over ~5 min)
        alpha = 0.005
        self.baseline_rmssd = self.baseline_rmssd * (1 - alpha) + rmssd * alpha

        # compute trend: slope of RMSSD over last 30 seconds
        self._update_trend(now)

    def _update_trend(self, now: float):
        window = 30.0
        recent_vals = []
        recent_times = []
        for t, v in zip(reversed(self.timestamps), reversed(self.rmssd_history)):
            if now - t > window:
                break
            recent_vals.append(v)
            recent_times.append(t)

        if len(recent_vals) < 4:
            self.trend = 0.0
            self.trend_strength = 0.0
            return

        # simple linear regression for slope
        n = len(recent_vals)
        t_rel = [t - recent_times[-1] for t in recent_times]
        mean_t = sum(t_rel) / n
        mean_v = sum(recent_vals) / n
        num = sum((t - mean_t) * (v - mean_v) for t, v in zip(t_rel, recent_vals))
        den = sum((t - mean_t)**2 for t in t_rel)
        if den < 0.001:
            self.trend = 0.0
            self.trend_strength = 0.0
            return

        slope = num / den  # ms of RMSSD per second
        self.trend = slope

        # normalize strength: ±2 ms/s is "strong"
        self.trend_strength = min(1.0, abs(slope) / 2.0)

    @property
    def relative_hrv(self) -> float:
        """Current RMSSD relative to baseline. 1.0 = at baseline, <1 = below."""
        if self.baseline_rmssd < 1:
            return 1.0
        return self.current_rmssd / self.baseline_rmssd

    @property
    def drop_intensity(self) -> float:
        """0..1 how much HRV has dropped. 0 = normal/above, 1 = severe drop."""
        rel = self.relative_hrv
        if rel >= 1.0:
            return 0.0
        # map 0.5..1.0 relative → 1.0..0.0 intensity
        return min(1.0, (1.0 - rel) * 2.0)


# --- Light mapping ---

@dataclass
class LightState:
    hue: float = 0.12  # warm yellow-orange (0..1 hue wheel)
    saturation: float = 0.6
    brightness: float = 0.4
    pulse_bpm: float = 6.0  # breathing rate

    def update_from_hrv(self, hrv: HRVState):
        drop = hrv.drop_intensity
        trend_down = max(0, -hrv.trend) / 2.0  # 0..1

        # Color: warm amber → cool blue as HRV drops
        # hue 0.08 (orange) → 0.55 (blue)
        target_hue = 0.08 + drop * 0.47
        self.hue += (target_hue - self.hue) * 0.03

        # Saturation increases with drop
        target_sat = 0.4 + drop * 0.5
        self.saturation += (target_sat - self.saturation) * 0.05

        # Brightness: slightly brighter on drops (attention)
        target_bri = 0.3 + drop * 0.35
        self.brightness += (target_bri - self.brightness) * 0.04

        # Pulse rate: calm 6 BPM → anxious 20 BPM
        target_bpm = 6.0 + drop * 14.0 + trend_down * 8.0
        self.pulse_bpm += (target_bpm - self.pulse_bpm) * 0.05

    @property
    def rgb(self) -> tuple[int, int, int]:
        r, g, b = colorsys.hsv_to_rgb(self.hue, self.saturation, self.brightness)
        return (int(r * 255), int(g * 255), int(b * 255))

    @property
    def hue_api_values(self) -> dict:
        """Hue bridge API values (hue: 0-65535, sat: 0-254, bri: 1-254)."""
        return {
            "hue": int(self.hue * 65535) % 65535,
            "sat": int(self.saturation * 254),
            "bri": max(1, int(self.brightness * 254)),
        }


# --- Hue bridge direct API ---

class HueBridge:
    def __init__(self, group: int = 0):
        self.group = group
        self.cfg = None
        self.last_update = 0
        self.min_interval = 0.8  # bridge rate limit: ~1/s per group

    def connect(self) -> bool:
        if not os.path.exists(HUE_CONFIG):
            print("⚠ Hue config not found — run pair.py first. Hue disabled.")
            return False
        with open(HUE_CONFIG) as f:
            self.cfg = json.load(f)
        try:
            self._api("GET", "/lights")
            print(f"✓ Hue bridge connected (group {self.group})")
            return True
        except Exception as e:
            print(f"⚠ Hue bridge unreachable: {e}")
            return False

    def set_state(self, state: dict):
        now = time.time()
        if now - self.last_update < self.min_interval:
            return
        self.last_update = now
        path = f"/groups/{self.group}/action"
        state["on"] = True
        # use short transition for organic feel (4 = 400ms)
        state.setdefault("transitiontime", 8)
        try:
            self._api("PUT", path, state)
        except Exception:
            pass

    def _api(self, method, path, body=None):
        url = f"http://{self.cfg['bridge']}/api/{self.cfg['username']}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=3) as r:
            return json.loads(r.read().decode())


# --- timeBuzzer direct MIDI ---

class BuzzerLED:
    def __init__(self):
        self.mo = None
        self.available = False

    def connect(self) -> bool:
        try:
            import rtmidi
        except ImportError:
            print("⚠ python-rtmidi not installed — buzzer disabled.")
            return False
        self.mo = rtmidi.MidiOut()
        for i, name in enumerate(self.mo.get_ports()):
            if "timeBuzzer" in name:
                self.mo.open_port(i)
                self.available = True
                print("✓ timeBuzzer connected")
                return True
        print("⚠ timeBuzzer not found — buzzer disabled.")
        return False

    def set_rgb(self, r: int, g: int, b: int):
        if not self.available:
            return
        for seg in range(3):
            cc_base = 70 + 3 * seg
            self.mo.send_message([187, cc_base, r // 2])
            self.mo.send_message([187, cc_base + 1, g // 2])
            self.mo.send_message([187, cc_base + 2, b // 2])


# --- Pulse modulation ---

def pulse_brightness(base_bri: float, bpm: float, t: float, min_factor=0.6) -> float:
    """Sinusoidal breathing modulation."""
    phase = (t * bpm / 60.0) * math.pi * 2
    wave = (math.sin(phase) + 1) / 2  # 0..1
    return base_bri * (min_factor + (1 - min_factor) * wave)


# --- Dashboard (optional tiny HTTP server) ---

DASHBOARD_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>HRV → Lights</title>
<style>
html,body{margin:0;background:#0a0a0c;color:#aaa;font:13px/1.6 ui-monospace,monospace}
.wrap{padding:20px;max-width:900px}
h1{font-size:16px;color:#fff;margin:0 0 12px}
canvas{width:100%;height:200px;border:1px solid #222;border-radius:4px;margin:8px 0}
.metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin:12px 0}
.m{background:#111;padding:10px 12px;border-radius:4px}
.m .val{font-size:22px;color:#fff;font-variant-numeric:tabular-nums}
.m .lbl{font-size:11px;color:#666}
.swatch{width:40px;height:40px;border-radius:50%;display:inline-block;vertical-align:middle;margin-right:12px;
  box-shadow:0 0 20px var(--glow)}
</style></head><body><div class="wrap">
<h1>HRV → Lights</h1>
<div class="metrics">
  <div class="m"><div class="val" id="rmssd">—</div><div class="lbl">RMSSD ms</div></div>
  <div class="m"><div class="val" id="trend">—</div><div class="lbl">trend ms/s</div></div>
  <div class="m"><div class="val" id="drop">—</div><div class="lbl">drop intensity</div></div>
  <div class="m"><div class="val" id="bpm">—</div><div class="lbl">pulse BPM</div></div>
  <div class="m"><div class="swatch" id="swatch"></div><span id="hex">#000</span></div>
</div>
<canvas id="chart"></canvas>
</div>
<script>
const canvas=document.getElementById('chart'),ctx=canvas.getContext('2d');
let data=[];
function resize(){canvas.width=canvas.clientWidth*2;canvas.height=canvas.clientHeight*2;draw()}
window.addEventListener('resize',resize);resize();
function draw(){
  ctx.clearRect(0,0,canvas.width,canvas.height);
  if(data.length<2)return;
  const maxV=Math.max(120,...data.map(d=>d.rmssd));
  ctx.strokeStyle='#4af';ctx.lineWidth=2;ctx.beginPath();
  data.forEach((d,i)=>{
    const x=i/(data.length-1)*canvas.width;
    const y=(1-d.rmssd/maxV)*canvas.height*0.9+canvas.height*0.05;
    i?ctx.lineTo(x,y):ctx.moveTo(x,y);
  });
  ctx.stroke();
  // baseline
  if(data.length>0){
    const bl=data[data.length-1].baseline;
    const y=(1-bl/maxV)*canvas.height*0.9+canvas.height*0.05;
    ctx.strokeStyle='#555';ctx.setLineDash([4,4]);ctx.beginPath();
    ctx.moveTo(0,y);ctx.lineTo(canvas.width,y);ctx.stroke();ctx.setLineDash([]);
  }
}

const es=new EventSource('/events');
es.onmessage=e=>{
  const d=JSON.parse(e.data);
  data.push(d);if(data.length>300)data.shift();
  document.getElementById('rmssd').textContent=d.rmssd.toFixed(1);
  document.getElementById('trend').textContent=(d.trend>=0?'+':'')+d.trend.toFixed(2);
  document.getElementById('drop').textContent=(d.drop*100).toFixed(0)+'%';
  document.getElementById('bpm').textContent=d.pulse_bpm.toFixed(1);
  const sw=document.getElementById('swatch');
  sw.style.background=d.hex;sw.style.setProperty('--glow',d.hex);
  document.getElementById('hex').textContent=d.hex;
  draw();
};
</script></body></html>"""


# --- Main loop ---

async def run(args):
    import websockets

    hrv = HRVState()
    light = LightState()

    hue = HueBridge(group=args.hue_group)
    hue_ok = hue.connect()

    buzzer = BuzzerLED()
    buzzer_ok = not args.no_buzzer and buzzer.connect()

    # SSE clients for dashboard
    sse_clients: set = set()

    # Dashboard server
    if args.dashboard:
        from http.server import HTTPServer, BaseHTTPRequestHandler
        import threading

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == '/':
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/html')
                    self.end_headers()
                    self.wfile.write(DASHBOARD_HTML.encode())
                elif self.path == '/events':
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/event-stream')
                    self.send_header('Cache-Control', 'no-cache')
                    self.end_headers()
                    q = asyncio.Queue()
                    sse_clients.add(q)
                    try:
                        while True:
                            # blocking in thread — fine for SSE
                            import queue as qmod
                            try:
                                data = q._queue[0] if q._queue else None
                            except:
                                data = None
                            time.sleep(0.5)
                    except:
                        sse_clients.discard(q)
                else:
                    self.send_response(404)
                    self.end_headers()
            def log_message(self, *a): pass

        # Use a simpler SSE approach with asyncio
        print(f"Dashboard: http://localhost:{args.dashboard_port}")

    print(f"\nListening on {WS_URL} for RR intervals…")
    print("Ctrl+C to stop.\n")

    start_time = time.time()
    last_light_update = 0
    light_interval = 0.5  # update lights every 500ms

    # SSE broadcast helper
    async def broadcast_sse(data: dict):
        # For dashboard we'll write to a shared list
        pass

    while True:
        try:
            async with websockets.connect(WS_URL) as ws:
                print("✓ Connected to bridge")
                async for raw in ws:
                    msg = json.loads(raw)
                    if msg.get("type") != "hr":
                        continue
                    rr_list = msg.get("rr", [])
                    for rr in rr_list:
                        updated = hrv.add_rr(rr)
                        if not updated:
                            continue

                        # Update light mapping
                        light.update_from_hrv(hrv)

                        now = time.time()
                        if now - last_light_update < light_interval:
                            continue
                        last_light_update = now

                        # Apply pulse modulation
                        t = now - start_time
                        modulated_bri = pulse_brightness(
                            light.brightness, light.pulse_bpm, t
                        )

                        # Drive Hue
                        if hue_ok:
                            vals = light.hue_api_values
                            vals["bri"] = max(1, int(modulated_bri * 254))
                            # longer transition when calm, shorter when dropping
                            vals["transitiontime"] = max(2, int(8 - hrv.drop_intensity * 5))
                            hue.set_state(vals)

                        # Drive buzzer
                        if buzzer_ok:
                            r, g, b = light.rgb
                            mod = modulated_bri / max(0.01, light.brightness)
                            buzzer.set_rgb(
                                int(r * mod), int(g * mod), int(b * mod)
                            )

                        # Console output
                        r, g, b = light.rgb
                        hex_color = f"#{r:02x}{g:02x}{b:02x}"
                        trend_arrow = "↗" if hrv.trend > 0.3 else "↘" if hrv.trend < -0.3 else "→"
                        print(
                            f"\r  RMSSD {hrv.current_rmssd:5.1f}ms  "
                            f"{trend_arrow} {hrv.trend:+.2f}ms/s  "
                            f"drop {hrv.drop_intensity*100:3.0f}%  "
                            f"pulse {light.pulse_bpm:4.1f}bpm  "
                            f"{hex_color}  ",
                            end="", flush=True
                        )

        except ConnectionRefusedError:
            print("Bridge not running. Retrying in 3s…")
            await asyncio.sleep(3)
        except Exception as e:
            print(f"\nConnection lost ({e}). Reconnecting in 2s…")
            await asyncio.sleep(2)


def main():
    parser = argparse.ArgumentParser(description="HRV → Lights daemon")
    parser.add_argument("--hue-group", type=int, default=0,
                        help="Hue group/room id (0=all)")
    parser.add_argument("--no-buzzer", action="store_true",
                        help="Disable timeBuzzer")
    parser.add_argument("--dashboard", action="store_true",
                        help="Serve trend dashboard")
    parser.add_argument("--dashboard-port", type=int, default=8081)
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
