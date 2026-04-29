"""HRV → Lights daemon. Connects to the Polar H10 WebSocket bridge,
computes rolling RMSSD + trend, and drives Hue + timeBuzzer LEDs.

Run alongside bridge.py:
    python hrv_lights.py [--hue-group 82] [--no-buzzer] [--dashboard 8081]

Data is persisted to hrv_log.jsonl for trend analysis across sessions.
"""
import asyncio
import argparse
import colorsys
import json
import math
import os
import sqlite3
import subprocess
import sys
import time
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

try:
    import websockets
except ImportError:
    sys.exit("websockets required: pip install websockets")

WS_URL = "ws://localhost:8765"
HUE_CONFIG = os.path.expanduser("~/.config/hue/config.json")
LOG_FILE = Path(__file__).parent / "hrv_log.jsonl"
DB_FILE = Path(__file__).parent / "hrv_data.db"
CONFIG_FILE = Path(__file__).parent / "hrv_lights_config.json"

# ── Configuration ───────────────────────────────────────────────────────
#
# Edit hrv_lights_config.json (created on first run) or change defaults here.
#
# HOW THE COLORS WORK:
#   Your RMSSD is mapped to a position between rmssd_low and rmssd_high.
#   Low RMSSD  (sympathetic / stressed) → cool colors (blue/teal), faster pulse
#   High RMSSD (parasympathetic / calm)  → warm colors (amber/orange), slow pulse
#
#   Movement adds energy: chest motion shifts the color warmer and increases
#   brightness briefly, so you see a "flash" when you move or gesture.

DEFAULT_CONFIG = {
    # -- RMSSD range (calibrate to YOUR body) --
    # These define the full color range. Values outside clip to the ends.
    "rmssd_low": 8,        # ms — below this is "max stress" (deep blue)
    "rmssd_high": 35,      # ms — above this is "max calm" (warm amber)

    # -- Color anchors (HSV hue, 0.0-1.0) --
    "color_calm": 0.08,    # warm amber/orange
    "color_mid": 0.45,     # teal
    "color_stress": 0.62,  # deep blue

    # -- Brightness --
    "brightness_min": 0.20,
    "brightness_max": 0.65,

    # -- Pulse (breathing rate of the light) --
    "pulse_bpm_calm": 6,   # slow breathing when relaxed
    "pulse_bpm_stress": 24,# faster when HRV is low

    # -- Movement reactivity --
    "movement_enabled": True,
    "movement_brightness_boost": 0.25,  # how much brighter on movement (0-1)
    "movement_warmth_shift": 0.06,      # shift hue toward warm on movement
    "movement_decay": 0.92,             # how fast movement effect fades (0.9=fast, 0.99=slow)

    # -- Smoothing (0.01=very smooth, 0.2=responsive) --
    "color_smoothing": 0.04,
    "brightness_smoothing": 0.05,
    "pulse_smoothing": 0.05,
}


def load_config() -> dict:
    cfg = DEFAULT_CONFIG.copy()
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE) as f:
                user_cfg = json.load(f)
            cfg.update(user_cfg)
        except Exception:
            pass
    else:
        with open(CONFIG_FILE, "w") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2)
        print(f"config written to {CONFIG_FILE}")
    return cfg


# ── HRV computation ─────────────────────────────────────────────────────

@dataclass
class HRVState:
    rr_buffer: deque = field(default_factory=lambda: deque(maxlen=60))
    rmssd_history: list = field(default_factory=list)
    baseline_rmssd: float = 15.0  # adaptive, seeded from DB
    current_rmssd: float = 23.0
    current_hr: int = 0
    trend: float = 0.0
    trend_strength: float = 0.0
    movement: float = 0.0         # 0 = still, 1 = strong motion
    movement_decay: float = 0.92

    def add_rr(self, rr_ms: int) -> bool:
        if rr_ms < 200 or rr_ms > 2000:
            return False
        self.rr_buffer.append(rr_ms)
        if len(self.rr_buffer) >= 6:
            self._update_rmssd()
            return True
        return False

    def add_acc(self, samples: list):
        if not samples:
            return
        magnitudes = [math.sqrt(x*x + y*y + z*z) for x, y, z in samples]
        mean_mag = sum(magnitudes) / len(magnitudes)
        deviation = abs(mean_mag - 1000) / 1000  # 1g = ~1000 in raw units
        instant = min(1.0, deviation * 3)
        self.movement = max(instant, self.movement * self.movement_decay)

    def _update_rmssd(self):
        rr = list(self.rr_buffer)
        diffs_sq = [(rr[i+1] - rr[i])**2 for i in range(len(rr)-1)]
        if not diffs_sq:
            return
        rmssd = math.sqrt(sum(diffs_sq) / len(diffs_sq))
        self.current_rmssd = rmssd
        now = time.time()
        self.rmssd_history.append((now, rmssd))

        alpha = 0.005
        self.baseline_rmssd = self.baseline_rmssd * (1 - alpha) + rmssd * alpha

        self._update_trend(now)

        # trim to last 10 min in memory
        cutoff = now - 600
        while self.rmssd_history and self.rmssd_history[0][0] < cutoff:
            self.rmssd_history.pop(0)

    def _update_trend(self, now: float):
        window = 30.0
        recent = [(t, v) for t, v in self.rmssd_history if now - t <= window]
        if len(recent) < 4:
            self.trend = 0.0
            self.trend_strength = 0.0
            return

        n = len(recent)
        t0 = recent[0][0]
        ts = [t - t0 for t, _ in recent]
        vs = [v for _, v in recent]
        mt = sum(ts) / n
        mv = sum(vs) / n
        num = sum((t - mt) * (v - mv) for t, v in zip(ts, vs))
        den = sum((t - mt)**2 for t in ts)
        if den < 0.001:
            self.trend = 0.0
            self.trend_strength = 0.0
            return
        self.trend = num / den
        self.trend_strength = min(1.0, abs(self.trend) / 2.0)

    @property
    def relative_hrv(self) -> float:
        if self.baseline_rmssd < 1:
            return 1.0
        return self.current_rmssd / self.baseline_rmssd

    @property
    def drop_intensity(self) -> float:
        low, high = 3.0, 30.0
        t = (self.current_rmssd - low) / (high - low)
        return 1.0 - max(0.0, min(1.0, t))


# ── Signal quality monitor ──────────────────────────────────────────────

def notify(title: str, message: str, sound: str = "Basso", speak: str = ""):
    subprocess.Popen([
        "osascript", "-e",
        f'display notification "{message}" with title "{title}" sound name "{sound}"'
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if speak:
        subprocess.Popen(["say", "-v", "Samantha", "-r", "180", speak],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@dataclass
class SignalQuality:
    # artifact detection
    artifact_count: int = 0
    artifact_window: deque = field(default_factory=lambda: deque(maxlen=30))
    last_notification: float = 0
    NOTIFY_COOLDOWN: float = 30.0  # don't spam

    # connection quality
    last_hr_time: float = 0
    last_rr_time: float = 0
    rr_gap_count: int = 0

    # battery
    last_battery: int = -1
    battery_warned: bool = False

    def check_rr(self, rr_ms: int, hr: int, rmssd: float) -> str | None:
        now = time.time()
        self.last_rr_time = now

        # artifact: RMSSD > 150ms with HR > 80 = missed beats
        is_artifact = rmssd > 150 and hr > 80
        self.artifact_window.append(1 if is_artifact else 0)
        artifact_pct = sum(self.artifact_window) / len(self.artifact_window) if self.artifact_window else 0

        if artifact_pct > 0.5 and len(self.artifact_window) >= 10:
            return self._warn("poor_contact",
                "Poor strap contact — adjust or wet electrodes",
                f"RMSSD {rmssd:.0f}ms is artifact ({artifact_pct*100:.0f}% bad readings)",
                speak="Poor strap contact. Adjust or wet the electrodes.")

        if rr_ms < 250:
            return self._warn("rr_too_short",
                "RR interval too short — possible double-detection",
                f"RR={rr_ms}ms ({60000/rr_ms:.0f}bpm)",
                speak="Signal error. Double beat detected.")

        if rr_ms > 1800:
            return self._warn("rr_too_long",
                "RR interval too long — possible missed beats",
                f"RR={rr_ms}ms ({60000/rr_ms:.0f}bpm)",
                speak="Signal error. Missed heartbeat.")

        # sudden HR jump (>40 bpm change between beats)
        if len(self.artifact_window) >= 2:
            prev_rr = None
            buf = list(self.artifact_window)
            # we don't store RR in the window, but RMSSD spike is the proxy
            pass

        return None

    def check_hr_timeout(self) -> str | None:
        now = time.time()
        if self.last_rr_time > 0 and now - self.last_rr_time > 10:
            return self._warn("no_rr",
                "No RR intervals for 10s",
                "Strap may have lost contact",
                speak="No heartbeat signal. Check the strap.")
        return None

    def check_battery(self, pct: int) -> str | None:
        if pct == self.last_battery:
            return None
        self.last_battery = pct
        if pct <= 15 and not self.battery_warned:
            self.battery_warned = True
            return self._warn("low_battery",
                f"Polar H10 battery low: {pct}%",
                "Charge after this session", sound="Purr",
                speak=f"Battery at {pct} percent.")
        return None

    def _warn(self, kind: str, title: str, message: str, sound: str = "Basso", speak: str = "") -> str:
        now = time.time()
        if now - self.last_notification < self.NOTIFY_COOLDOWN:
            return f"[{kind}] {message}"
        self.last_notification = now
        notify(title, message, sound, speak=speak)
        return f"[{kind}] {title}: {message}"


# ── Light presets ────────────────────────────────────────────────────────
#
# Each preset is a dict defining how HRV maps to light.
# Cycle with --preset NAME or send {"preset": "name"} via dashboard WS.

PRESETS = {
    "sleep": {
        # MacBook sleep indicator. Deep red, barely visible, very slow breath.
        "name": "sleep",
        "desc": "dim red breathing — like MacBook sleep light",
        "calm":   {"h": 0.00, "s": 0.90, "b": 0.12, "pulse": 3.5},
        "stress": {"h": 0.03, "s": 0.80, "b": 0.20, "pulse": 7.0},
        "smooth": 0.02,
    },
    "candle": {
        # High HRV = deep red glow. Low HRV = pale amber/yellow.
        # The redder the room, the calmer you are.
        "name": "candle",
        "desc": "redder = calmer — deep red when HRV high, amber when low",
        "calm":   {"h": 0.00, "s": 0.95, "b": 0.55, "pulse": 4.0},
        "stress": {"h": 0.09, "s": 0.70, "b": 0.70, "pulse": 14.0},
        "smooth": 0.06,
    },
    "sunset": {
        # Sunset gradient. Gold when calm, deep pink-red when stressed.
        "name": "sunset",
        "desc": "gold → rose — color tells you the state",
        "calm":   {"h": 0.11, "s": 0.70, "b": 0.50, "pulse": 5.0},
        "stress": {"h": 0.95, "s": 0.85, "b": 0.65, "pulse": 12.0},
        "smooth": 0.06,
    },
    "ocean": {
        # Calm teal to stormy blue. Cool palette.
        "name": "ocean",
        "desc": "teal → deep blue — cool and clear",
        "calm":   {"h": 0.48, "s": 0.50, "b": 0.45, "pulse": 5.0},
        "stress": {"h": 0.62, "s": 0.90, "b": 0.65, "pulse": 16.0},
        "smooth": 0.06,
    },
    "traffic": {
        # Green → yellow → red. Unmistakable. You always know.
        "name": "traffic",
        "desc": "green/yellow/red — impossible to misread",
        "calm":   {"h": 0.33, "s": 0.80, "b": 0.45, "pulse": 4.0},
        "stress": {"h": 0.00, "s": 0.90, "b": 0.65, "pulse": 18.0},
        "smooth": 0.08,
    },
}

DEFAULT_PRESET = "candle"


# ── Light state mapping ─────────────────────────────────────────────────

@dataclass
class LightState:
    hue: float = 0.07
    saturation: float = 0.85
    brightness: float = 0.30
    pulse_bpm: float = 4.0
    preset_name: str = DEFAULT_PRESET

    def set_preset(self, name: str):
        if name not in PRESETS:
            return
        self.preset_name = name
        p = PRESETS[name]["calm"]
        self.hue = p["h"]
        self.saturation = p["s"]
        self.brightness = p["b"]
        self.pulse_bpm = p["pulse"]

    def update_from_hrv(self, hrv: HRVState):
        p = PRESETS.get(self.preset_name, PRESETS[DEFAULT_PRESET])
        drop = hrv.drop_intensity
        trend_down = max(0, -hrv.trend) / 2.0
        calm, stress = p["calm"], p["stress"]
        smooth = p["smooth"]

        target_h = calm["h"] + (stress["h"] - calm["h"]) * drop
        target_s = calm["s"] + (stress["s"] - calm["s"]) * drop
        target_b = calm["b"] + (stress["b"] - calm["b"]) * drop
        target_p = calm["pulse"] + (stress["pulse"] - calm["pulse"]) * drop + trend_down * 3.0

        # handle hue wrap (e.g. sunset: 0.11 → 0.95 should go backward)
        if abs(target_h - self.hue) > 0.5:
            if target_h > self.hue:
                target_h -= 1.0
            else:
                target_h += 1.0

        self.hue = (self.hue + (target_h - self.hue) * smooth) % 1.0
        self.saturation += (target_s - self.saturation) * smooth
        self.brightness += (target_b - self.brightness) * smooth
        self.pulse_bpm += (target_p - self.pulse_bpm) * smooth

    @property
    def rgb(self) -> tuple[int, int, int]:
        r, g, b = colorsys.hsv_to_rgb(self.hue % 1.0, self.saturation, self.brightness)
        return int(r * 255), int(g * 255), int(b * 255)

    @property
    def hue_api_values(self) -> dict:
        return {
            "hue": int((self.hue % 1.0) * 65535),
            "sat": int(self.saturation * 254),
            "bri": max(1, int(self.brightness * 254)),
        }


def pulse_mod(base_bri: float, bpm: float, t: float) -> float:
    phase = (t * bpm / 60.0) * math.pi * 2
    wave = (math.sin(phase) + 1) / 2
    wave = wave * wave
    # never go below 40% of base — a candle doesn't go dark
    return base_bri * (0.40 + 0.60 * wave)


# ── Hue bridge ──────────────────────────────────────────────────────────

class HueBridge:
    def __init__(self, lights: list = None):
        self.lights = lights or [2, 3, 4, 6]
        self.cfg = None
        self.last_update = 0
        self.last_beat_time = 0
        self.heartbeat_intensity = 0.5  # 0=invisible, 1=max flash

    def connect(self) -> bool:
        if not os.path.exists(HUE_CONFIG):
            print("hue config not found — disabled")
            return False
        with open(HUE_CONFIG) as f:
            self.cfg = json.load(f)
        try:
            self._api("GET", "/lights")
            print(f"hue bridge connected (lights {self.lights})")
            return True
        except Exception as e:
            print(f"hue bridge unreachable: {e}")
            return False

    def on_heartbeat(self):
        self.last_beat_time = time.time()

    def apply_gradient(self, base_state: dict, t: float, pulse_bpm: float):
        now = time.time()
        if now - self.last_update < 0.15:  # faster updates for heartbeat visibility
            return
        self.last_update = now

        # heartbeat flash: wide enough for Hue bridge to render (~300ms visible)
        beat_age = now - self.last_beat_time
        beat_flash = math.exp(-beat_age * 2.5) if beat_age < 2 else 0
        beat_flash *= self.heartbeat_intensity

        n = len(self.lights)
        for i, lid in enumerate(self.lights):
            # breathing wave with per-light phase offset
            offset = i / n
            phase = (t * pulse_bpm / 60.0 + offset) * math.pi * 2
            wave = (math.sin(phase) + 1) / 2
            wave = wave * wave

            # heartbeat ripple: each light flashes with a slight delay
            beat_delay = i * 0.12
            local_age = beat_age - beat_delay
            local_beat = math.exp(-local_age * 2.5) if 0 < local_age < 2 else 0
            local_beat *= self.heartbeat_intensity

            # hue drift
            hue_spread = 3000
            hue_drift = int(math.sin(t * 0.03 + i * 1.5) * hue_spread)

            bri = base_state["bri"]
            # combine breathing + heartbeat flash
            breathing_bri = bri * (0.30 + 0.70 * wave)
            # heartbeat boosts to near-max then decays
            flash_bri = 254 * local_beat
            mod_bri = int(min(254, max(breathing_bri, flash_bri)))

            # transition: instant on heartbeat flash, smooth otherwise
            trans = 0 if local_beat > 0.2 else (2 if local_beat > 0.05 else base_state.get("transitiontime", 8))

            state = {
                "on": True,
                "hue": (base_state["hue"] + hue_drift) % 65535,
                "sat": base_state["sat"],
                "bri": max(1, mod_bri),
                "transitiontime": trans,
            }
            try:
                self._api("PUT", f"/lights/{lid}/state", state)
            except Exception:
                pass

    def _api(self, method, path, body=None):
        url = f"http://{self.cfg['bridge']}/api/{self.cfg['username']}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=3) as r:
            return json.loads(r.read().decode())


# ── timeBuzzer ──────────────────────────────────────────────────────────

class BuzzerLED:
    def __init__(self):
        self.mo = None
        self.mi = None
        self.available = False
        self.last_rgb = (-1, -1, -1)
        self.last_press_time = 0
        self.press_count = 0

    def connect(self) -> bool:
        try:
            import rtmidi
        except ImportError:
            print("python-rtmidi not installed — buzzer disabled")
            return False
        self.mo = rtmidi.MidiOut()
        for i, name in enumerate(self.mo.get_ports()):
            if "timeBuzzer" in name:
                self.mo.open_port(i)
                self.available = True
                print("timeBuzzer LED connected")
                break

        # try MIDI input for button press
        try:
            self.mi = rtmidi.MidiIn()
            for i, name in enumerate(self.mi.get_ports()):
                if "timeBuzzer" in name:
                    self.mi.open_port(i)
                    self.mi.set_callback(self._midi_callback)
                    # init rotation tracking
                    self.mo.send_message([187, 80, 64])
                    print("timeBuzzer input connected (double-press → dictation)")
                    break
        except Exception as e:
            print(f"timeBuzzer input not available: {e}")

        return self.available

    def _midi_callback(self, event, _):
        msg, _ = event
        if len(msg) < 3:
            return
        status, cc, value = msg[0], msg[1], msg[2]
        if status != 187:  # CC on channel 12
            return
        if cc == 82 and value == 127:  # press down
            now = time.time()
            if now - self.last_press_time < 0.5:
                self.press_count += 1
            else:
                self.press_count = 1
            self.last_press_time = now

            if self.press_count >= 2:
                self.press_count = 0
                self._trigger_dictation()

    recording = False

    def _trigger_dictation(self):
        self.recording = not self.recording
        print(f"\n  buzzer double-press → {'recording' if self.recording else 'stopped'}")
        subprocess.Popen(
            ["cliclick", "kd:shift", "kd:alt", "t:d", "ku:alt", "ku:shift"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        if self.recording:
            self._recording_start = time.time()

    def set_rgb(self, r: int, g: int, b: int):
        if not self.available:
            return
        rgb = (r, g, b)
        if rgb == self.last_rgb:
            return
        self.last_rgb = rgb
        for seg in range(3):
            cc = 70 + 3 * seg
            self.mo.send_message([187, cc, r // 2])
            self.mo.send_message([187, cc + 1, g // 2])
            self.mo.send_message([187, cc + 2, b // 2])

    def close(self):
        if self.available:
            self.set_rgb(0, 0, 0)
            self.mo.close_port()


# ── Data persistence ────────────────────────────────────────────────────

def load_baseline_from_db() -> tuple[float, int]:
    """Load baseline RMSSD from SQLite hrv_samples across all sessions.
    Returns (baseline_rmssd, sample_count).
    """
    if not DB_FILE.exists():
        return 23.0, 0
    try:
        conn = sqlite3.connect(str(DB_FILE))
        cur = conn.execute(
            "SELECT AVG(rmssd), COUNT(*) FROM hrv_samples "
            "WHERE rmssd > 5 AND rmssd < 200"
        )
        avg, cnt = cur.fetchone()
        conn.close()
        if avg and cnt > 0:
            return avg, cnt
    except Exception:
        pass
    return 23.0, 0


def load_history_from_db(max_age_hours: int = 24) -> list[dict]:
    """Load recent RMSSD history from SQLite for dashboard replay."""
    if not DB_FILE.exists():
        return []
    try:
        conn = sqlite3.connect(str(DB_FILE))
        cutoff = time.time() - max_age_hours * 3600
        rows = conn.execute(
            "SELECT h.ts, h.rmssd, h.baseline_rmssd, h.hr_mean, h.trend_slope "
            "FROM hrv_samples h WHERE h.ts > ? ORDER BY h.ts",
            (cutoff,)
        ).fetchall()
        conn.close()
        return [
            {
                "ts": r[0], "rmssd": round(r[1], 1),
                "baseline": round(r[2] or 23, 1),
                "hr": int(r[3] or 0),
                "trend": round(r[4] or 0, 3),
                "drop": 0, "hex": "#997744", "pulse_bpm": 6,
            }
            for r in rows
        ]
    except Exception:
        return []


def load_history(max_age_hours: int = 24) -> list[dict]:
    """Load from SQLite first, fall back to JSONL."""
    db_hist = load_history_from_db(max_age_hours)
    if db_hist:
        return db_hist
    if not LOG_FILE.exists():
        return []
    cutoff = time.time() - max_age_hours * 3600
    rows = []
    for line in LOG_FILE.read_text().splitlines():
        try:
            row = json.loads(line)
            if row.get("ts", 0) > cutoff:
                rows.append(row)
        except json.JSONDecodeError:
            continue
    return rows


def append_log(row: dict):
    with open(LOG_FILE, "a") as f:
        f.write(json.dumps(row) + "\n")


# ── Dashboard WebSocket ─────────────────────────────────────────────────

dash_clients: set = set()
_light_ref: list = [None]  # mutable ref for preset switching from dashboard


async def dash_handler(ws):
    dash_clients.add(ws)
    try:
        # send preset list + historical data on connect
        await ws.send(json.dumps({
            "type": "presets",
            "presets": {k: v["desc"] for k, v in PRESETS.items()},
            "active": _light_ref[0].preset_name if _light_ref[0] else DEFAULT_PRESET,
        }))
        hist = load_history()
        if hist:
            await ws.send(json.dumps({"type": "history", "data": hist[-300:]}))
        async for raw in ws:
            try:
                msg = json.loads(raw)
                if msg.get("cmd") == "preset" and _light_ref[0]:
                    _light_ref[0].set_preset(msg["name"])
                    print(f"\n  preset → {msg['name']}")
                    await dash_broadcast({"type": "preset_changed", "name": msg["name"]})
            except Exception:
                pass
    finally:
        dash_clients.discard(ws)


async def dash_broadcast(msg: dict):
    if not dash_clients:
        return
    payload = json.dumps(msg)
    dead = []
    for ws in list(dash_clients):
        try:
            await ws.send(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        dash_clients.discard(ws)


# ── Main loop ───────────────────────────────────────────────────────────

async def run(args):
    hrv = HRVState()
    light = LightState()
    light.set_preset(args.preset)
    _light_ref[0] = light
    sig = SignalQuality()

    # seed baseline from SQLite (all historical sessions)
    db_baseline, db_count = load_baseline_from_db()
    hrv.baseline_rmssd = db_baseline
    print(f"baseline from {db_count} historical samples: {db_baseline:.1f}ms")
    print(f"preset: {args.preset} — {PRESETS[args.preset]['desc']}")

    hue = HueBridge(lights=args.hue_lights)
    hue.heartbeat_intensity = args.heartbeat
    hue_ok = hue.connect()

    buzzer = BuzzerLED()
    buzzer_ok = not args.no_buzzer and buzzer.connect()

    dash_ws = await websockets.serve(dash_handler, "localhost", args.dashboard + 1)
    print(f"dashboard data: ws://localhost:{args.dashboard + 1}")
    print(f"dashboard page: http://localhost:{args.dashboard}")
    print(f"listening on {WS_URL}…\n")

    start_t = time.time()
    last_light_t = 0
    last_log_t = 0

    while True:
        try:
            async with websockets.connect(WS_URL) as ws:
                print("connected to polar bridge")
                async for raw in ws:
                    msg = json.loads(raw)

                    if msg.get("type") == "acc":
                        hrv.add_acc(msg.get("samples", []))

                    if msg.get("type") == "battery":
                        sig.check_battery(msg.get("pct", -1))

                    if msg.get("type") == "hr":
                        hrv.current_hr = msg["bpm"]
                        for rr in msg.get("rr", []):
                            # each RR = one heartbeat — trigger flash
                            if hue_ok:
                                hue.on_heartbeat()
                            if not hrv.add_rr(rr):
                                continue

                            sig.check_rr(rr, hrv.current_hr, hrv.current_rmssd)

                            light.update_from_hrv(hrv)
                            now = time.time()
                            if now - last_light_t < 0.5:
                                continue
                            last_light_t = now

                            t = now - start_t
                            mv = hrv.movement

                            if hue_ok:
                                vals = light.hue_api_values
                                # movement makes transitions snappier
                                vals["transitiontime"] = max(3, 8 - int(mv * 4))
                                # movement boosts brightness
                                if mv > 0.05:
                                    vals["bri"] = min(254, vals["bri"] + int(mv * 60))
                                hue.apply_gradient(vals, t, light.pulse_bpm)

                            # buzzer: use pulse_mod for its own breathing
                            mod_bri = pulse_mod(light.brightness, light.pulse_bpm, t)
                            if mv > 0.05:
                                mod_bri = min(0.95, mod_bri + mv * 0.15)

                            if buzzer_ok:
                                if buzzer.recording:
                                    # pulsating red during dictation
                                    rec_age = now - buzzer._recording_start
                                    pulse = (math.sin(rec_age * 4) + 1) / 2  # ~0.6 Hz pulse
                                    br = int(80 + pulse * 175)
                                    bg = int(pulse * 15)
                                    bb = int(pulse * 10)
                                else:
                                    # normal: heartbeat flash + base color
                                    beat_age = now - hue.last_beat_time if hue_ok else 1
                                    buzz_flash = math.exp(-beat_age * 4) * args.heartbeat if beat_age < 1.5 else 0

                                    if buzz_flash > 0.3:
                                        br = int(min(255, 200 + buzz_flash * 55))
                                        bg = int(min(255, 100 + buzz_flash * 100))
                                        bb = int(min(255, 80 + buzz_flash * 80))
                                    else:
                                        r, g, b = light.rgb
                                        scale = max(1, 120 / max(r, g, b, 1))
                                        br = int(min(255, r * scale))
                                        bg = int(min(255, g * scale))
                                        bb = int(min(255, b * scale))
                                buzzer.set_rgb(br, bg, bb)

                            r, g, b = light.rgb
                            hex_c = f"#{r:02x}{g:02x}{b:02x}"
                            arrow = "↗" if hrv.trend > 0.3 else "↘" if hrv.trend < -0.3 else "→"

                            row = {
                                "ts": now,
                                "hr": hrv.current_hr,
                                "rmssd": round(hrv.current_rmssd, 1),
                                "baseline": round(hrv.baseline_rmssd, 1),
                                "trend": round(hrv.trend, 3),
                                "drop": round(hrv.drop_intensity, 3),
                                "hex": hex_c,
                                "pulse_bpm": round(light.pulse_bpm, 1),
                                "movement": round(hrv.movement, 3),
                                "preset": light.preset_name,
                                "signal_ok": sum(sig.artifact_window) < len(sig.artifact_window) * 0.3 if sig.artifact_window else True,
                            }

                            await dash_broadcast({"type": "tick", **row})

                            if now - last_log_t >= 5:
                                last_log_t = now
                                append_log(row)

                            mv_bar = "█" * int(mv * 10) if mv > 0.05 else ""
                            print(
                                f"\r  [{light.preset_name}] hr={hrv.current_hr}  "
                                f"rmssd={hrv.current_rmssd:5.1f}ms  "
                                f"{arrow} {hrv.trend:+.2f}ms/s  "
                                f"drop={hrv.drop_intensity*100:3.0f}%  "
                                f"pulse={light.pulse_bpm:4.1f}bpm  "
                                f"{hex_c}  {mv_bar}",
                                end="   \x1b[K", flush=True
                            )

        except (ConnectionRefusedError, OSError):
            print("\rpolar bridge not running, retrying in 3s…", end="", flush=True)
            await asyncio.sleep(3)
        except websockets.ConnectionClosed:
            print("\nbridge disconnected, reconnecting…")
            await asyncio.sleep(1)
        except KeyboardInterrupt:
            break

    if buzzer_ok:
        buzzer.close()
    dash_ws.close()
    await dash_ws.wait_closed()
    print("\nshutdown.")


def main():
    parser = argparse.ArgumentParser(
        description="HRV → Lights daemon",
        epilog="Presets: " + ", ".join(f"{k} ({v['desc']})" for k, v in PRESETS.items()))
    parser.add_argument("--hue-lights", type=int, nargs="+", default=[2, 3, 4, 6],
                        help="Hue light IDs to control (default: 2 3 4 6)")
    parser.add_argument("--heartbeat", type=float, default=0.5,
                        help="Heartbeat flash intensity 0-1 (default: 0.5, 0=off)")
    parser.add_argument("--no-buzzer", action="store_true")
    parser.add_argument("--preset", default=DEFAULT_PRESET,
                        choices=list(PRESETS.keys()),
                        help=f"Light preset (default: {DEFAULT_PRESET})")
    parser.add_argument("--dashboard", type=int, default=8081,
                        help="Dashboard HTTP port (WebSocket on port+1)")
    args = parser.parse_args()

    print(f"hrv-lights · hue lights {args.hue_lights} · "
          f"buzzer {'off' if args.no_buzzer else 'on'}")

    asyncio.run(run(args))


if __name__ == "__main__":
    main()
