"""Polar H10 HRV daemon — tracks stress, movement, HRV trends.
Stores all data in SQLite. Drives Hue + timeBuzzer ambient lighting.

Run alongside bridge.py:
    python hrv_daemon.py [--hue-group 1] [--no-buzzer] [--no-lights] [--db path]
"""
import argparse
import asyncio
import colorsys
import json
import math
import os
import sqlite3
import struct
import subprocess
import time
import urllib.request
import urllib.error
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

WS_URL = "ws://localhost:8765"
HUE_CONFIG = os.path.expanduser("~/.config/hue/config.json")
DEFAULT_DB = os.path.expanduser("~/ai_projects/polar-h10-ribbon/hrv_data.db")


# === DATABASE ===

def init_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY,
            started_at TEXT NOT NULL,
            ended_at TEXT,
            notes TEXT
        );

        CREATE TABLE IF NOT EXISTS rr_intervals (
            id INTEGER PRIMARY KEY,
            session_id INTEGER NOT NULL,
            ts REAL NOT NULL,
            rr_ms INTEGER NOT NULL,
            hr_bpm INTEGER,
            FOREIGN KEY (session_id) REFERENCES sessions(id)
        );

        CREATE TABLE IF NOT EXISTS hrv_samples (
            id INTEGER PRIMARY KEY,
            session_id INTEGER NOT NULL,
            ts REAL NOT NULL,
            rmssd REAL NOT NULL,
            sdnn REAL,
            hr_mean REAL,
            baseline_rmssd REAL,
            relative_hrv REAL,
            trend_slope REAL,
            FOREIGN KEY (session_id) REFERENCES sessions(id)
        );

        CREATE TABLE IF NOT EXISTS stress_events (
            id INTEGER PRIMARY KEY,
            session_id INTEGER NOT NULL,
            ts REAL NOT NULL,
            duration_s REAL,
            severity REAL NOT NULL,
            rmssd_at_event REAL,
            baseline_at_event REAL,
            trigger_type TEXT,
            FOREIGN KEY (session_id) REFERENCES sessions(id)
        );

        CREATE TABLE IF NOT EXISTS movement_samples (
            id INTEGER PRIMARY KEY,
            session_id INTEGER NOT NULL,
            ts REAL NOT NULL,
            stillness REAL NOT NULL,
            magnitude REAL NOT NULL,
            breath_rate REAL,
            FOREIGN KEY (session_id) REFERENCES sessions(id)
        );

        CREATE TABLE IF NOT EXISTS stillness_alerts (
            id INTEGER PRIMARY KEY,
            session_id INTEGER NOT NULL,
            ts REAL NOT NULL,
            still_duration_s REAL NOT NULL,
            FOREIGN KEY (session_id) REFERENCES sessions(id)
        );

        CREATE TABLE IF NOT EXISTS markers (
            id INTEGER PRIMARY KEY,
            session_id INTEGER NOT NULL,
            ts REAL NOT NULL,
            label TEXT,
            hr_bpm INTEGER,
            rmssd REAL,
            FOREIGN KEY (session_id) REFERENCES sessions(id)
        );

        CREATE TABLE IF NOT EXISTS hrv_advanced (
            id INTEGER PRIMARY KEY,
            session_id INTEGER NOT NULL,
            ts REAL NOT NULL,
            ln_rmssd REAL,
            dfa_alpha1 REAL,
            sample_entropy REAL,
            sd1 REAL,
            sd2 REAL,
            pnn50 REAL,
            hf_power REAL,
            lf_power REAL,
            vlf_power REAL,
            FOREIGN KEY (session_id) REFERENCES sessions(id)
        );

        CREATE TABLE IF NOT EXISTS daily_baselines (
            id INTEGER PRIMARY KEY,
            date TEXT NOT NULL UNIQUE,
            ln_rmssd_mean REAL,
            ln_rmssd_cv REAL,
            rmssd_mean REAL,
            hr_mean REAL,
            dfa_alpha1_mean REAL,
            sample_entropy_mean REAL,
            z_score REAL,
            notes TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_rr_ts ON rr_intervals(ts);
        CREATE INDEX IF NOT EXISTS idx_hrv_ts ON hrv_samples(ts);
        CREATE INDEX IF NOT EXISTS idx_move_ts ON movement_samples(ts);
        CREATE INDEX IF NOT EXISTS idx_adv_ts ON hrv_advanced(ts);
    """)
    conn.commit()
    return conn


# === HRV COMPUTATION ===

@dataclass
class HRVEngine:
    rr_buffer: deque = field(default_factory=lambda: deque(maxlen=120))
    rmssd_history: deque = field(default_factory=lambda: deque(maxlen=600))
    timestamps: deque = field(default_factory=lambda: deque(maxlen=600))
    baseline_rmssd: float = 50.0
    current_rmssd: float = 50.0
    current_sdnn: float = 50.0
    trend: float = 0.0
    hr_from_rr: float = 0.0

    # stress tracking
    stress_active: bool = False
    stress_start: float = 0.0
    stress_severity: float = 0.0

    def add_rr(self, rr_ms: int, now: float) -> bool:
        if rr_ms < 200 or rr_ms > 2000:
            return False
        self.rr_buffer.append(rr_ms)
        if len(self.rr_buffer) >= 2:
            self.hr_from_rr = 60000.0 / rr_ms
        if len(self.rr_buffer) >= 6:
            self._compute(now)
            return True
        return False

    def _compute(self, now: float):
        rr = list(self.rr_buffer)
        n = len(rr)

        # RMSSD
        diffs_sq = [(rr[i+1] - rr[i])**2 for i in range(n-1)]
        self.current_rmssd = math.sqrt(sum(diffs_sq) / len(diffs_sq))

        # SDNN
        mean_rr = sum(rr) / n
        var = sum((r - mean_rr)**2 for r in rr) / n
        self.current_sdnn = math.sqrt(var)

        self.rmssd_history.append(self.current_rmssd)
        self.timestamps.append(now)

        # baseline: slow EMA (~5 min adaptation)
        alpha = 0.003
        self.baseline_rmssd = self.baseline_rmssd * (1 - alpha) + self.current_rmssd * alpha

        # trend: slope over last 30s
        self._compute_trend(now)

    def _compute_trend(self, now: float):
        window = 30.0
        vals, times = [], []
        for t, v in zip(reversed(self.timestamps), reversed(self.rmssd_history)):
            if now - t > window:
                break
            vals.append(v)
            times.append(t)
        if len(vals) < 4:
            self.trend = 0.0
            return
        n = len(vals)
        t_rel = [t - times[-1] for t in times]
        mean_t = sum(t_rel) / n
        mean_v = sum(vals) / n
        num = sum((t - mean_t) * (v - mean_v) for t, v in zip(t_rel, vals))
        den = sum((t - mean_t)**2 for t in t_rel)
        self.trend = num / den if den > 0.001 else 0.0

    @property
    def relative_hrv(self) -> float:
        if self.baseline_rmssd < 1:
            return 1.0
        return self.current_rmssd / self.baseline_rmssd

    @property
    def drop_intensity(self) -> float:
        rel = self.relative_hrv
        if rel >= 1.0:
            return 0.0
        return min(1.0, (1.0 - rel) * 2.0)

    def check_stress(self, now: float) -> dict | None:
        """Returns a stress event dict when stress ends, None otherwise."""
        is_stressed = self.drop_intensity > 0.3 and self.trend < -0.2
        if is_stressed and not self.stress_active:
            self.stress_active = True
            self.stress_start = now
            self.stress_severity = self.drop_intensity
        elif is_stressed and self.stress_active:
            self.stress_severity = max(self.stress_severity, self.drop_intensity)
        elif not is_stressed and self.stress_active:
            self.stress_active = False
            duration = now - self.stress_start
            if duration > 5:  # ignore < 5s blips
                return {
                    "ts": self.stress_start,
                    "duration_s": duration,
                    "severity": self.stress_severity,
                    "rmssd_at_event": self.current_rmssd,
                    "baseline_at_event": self.baseline_rmssd,
                }
        return None


# === ADVANCED HRV ANALYSIS ===

def compute_dfa_alpha1(rr_intervals: list[int], min_box=4, max_box=16) -> float | None:
    """Detrended Fluctuation Analysis — short-term scaling exponent.
    α1 ≈ 1.0 = healthy fractal, < 0.75 = high risk, > 1.5 = rigid."""
    n = len(rr_intervals)
    if n < max_box * 4:
        return None
    # Integrate the mean-subtracted series
    mean_rr = sum(rr_intervals) / n
    y = []
    cumsum = 0
    for rr in rr_intervals:
        cumsum += (rr - mean_rr)
        y.append(cumsum)
    # Compute fluctuation for each box size
    box_sizes = []
    fluctuations = []
    box = min_box
    while box <= max_box:
        num_boxes = n // box
        if num_boxes < 2:
            break
        f_sum = 0
        count = 0
        for i in range(num_boxes):
            seg = y[i * box:(i + 1) * box]
            # Linear detrend
            x_mean = (box - 1) / 2.0
            y_mean = sum(seg) / box
            num = sum((j - x_mean) * (seg[j] - y_mean) for j in range(box))
            den = sum((j - x_mean) ** 2 for j in range(box))
            if den == 0:
                continue
            slope = num / den
            intercept = y_mean - slope * x_mean
            for j in range(box):
                resid = seg[j] - (slope * j + intercept)
                f_sum += resid * resid
            count += box
        if count > 0:
            fluctuations.append(math.sqrt(f_sum / count))
            box_sizes.append(box)
        box += 1
    if len(box_sizes) < 3:
        return None
    # Log-log linear regression
    log_n = [math.log(b) for b in box_sizes]
    log_f = [math.log(f) if f > 0 else -10 for f in fluctuations]
    n_pts = len(log_n)
    mean_x = sum(log_n) / n_pts
    mean_y = sum(log_f) / n_pts
    num = sum((log_n[i] - mean_x) * (log_f[i] - mean_y) for i in range(n_pts))
    den = sum((log_n[i] - mean_x) ** 2 for i in range(n_pts))
    if den < 1e-10:
        return None
    return num / den


def compute_sample_entropy(rr_intervals: list[int], m=2, r_factor=0.2) -> float | None:
    """Sample Entropy — regularity measure. Higher = more complex = healthier."""
    n = len(rr_intervals)
    if n < 50:
        return None
    sd = math.sqrt(sum((x - sum(rr_intervals)/n)**2 for x in rr_intervals) / n)
    r = r_factor * sd
    if r < 0.1:
        return None

    def count_matches(length):
        count = 0
        for i in range(n - length):
            for j in range(i + 1, n - length):
                match = True
                for k in range(length):
                    if abs(rr_intervals[i + k] - rr_intervals[j + k]) > r:
                        match = False
                        break
                if match:
                    count += 1
        return count

    a = count_matches(m + 1)
    b = count_matches(m)
    if b == 0:
        return None
    return -math.log(a / b) if a > 0 else None


def compute_poincare_metrics(rr_intervals: list[int]) -> tuple[float, float, float] | None:
    """Returns (SD1, SD2, SD1/SD2) from RR intervals."""
    if len(rr_intervals) < 10:
        return None
    diffs = [rr_intervals[i+1] - rr_intervals[i] for i in range(len(rr_intervals)-1)]
    sums = [rr_intervals[i+1] + rr_intervals[i] for i in range(len(rr_intervals)-1)]
    sd1 = math.sqrt(sum(d**2 for d in diffs) / len(diffs)) / math.sqrt(2)
    mean_s = sum(sums) / len(sums)
    sd2 = math.sqrt(sum((s - mean_s)**2 for s in sums) / len(sums)) / math.sqrt(2)
    ratio = sd1 / sd2 if sd2 > 0 else 0
    return sd1, sd2, ratio


def compute_pnn50(rr_intervals: list[int]) -> float:
    if len(rr_intervals) < 2:
        return 0
    count = sum(1 for i in range(len(rr_intervals)-1) if abs(rr_intervals[i+1] - rr_intervals[i]) > 50)
    return count / (len(rr_intervals) - 1) * 100


class AdvancedAnalyzer:
    """Runs advanced analysis periodically (every 30s) on accumulated RR data."""
    def __init__(self):
        self.last_compute = 0
        self.interval = 30  # seconds
        self.dfa_alpha1: float | None = None
        self.sample_ent: float | None = None
        self.sd1: float | None = None
        self.sd2: float | None = None
        self.pnn50: float = 0
        self.ln_rmssd: float = 0

    def update(self, rr_buffer: deque, rmssd: float, now: float) -> bool:
        if now - self.last_compute < self.interval:
            return False
        self.last_compute = now
        rr = list(rr_buffer)
        if len(rr) < 30:
            return False

        self.ln_rmssd = math.log(max(1, rmssd))
        self.dfa_alpha1 = compute_dfa_alpha1(rr)
        self.pnn50 = compute_pnn50(rr)

        poincare = compute_poincare_metrics(rr)
        if poincare:
            self.sd1, self.sd2, _ = poincare

        # SampEn is expensive — only compute with enough data and not too often
        if len(rr) >= 60:
            self.sample_ent = compute_sample_entropy(rr[-120:])  # cap at last 120 beats

        return True

    def log_line(self) -> str:
        alpha = f"α1={self.dfa_alpha1:.2f}" if self.dfa_alpha1 else "α1=—"
        ent = f"SampEn={self.sample_ent:.2f}" if self.sample_ent else "SampEn=—"
        return f"ln={self.ln_rmssd:.2f} {alpha} {ent} pNN50={self.pnn50:.0f}%"


# === NOTIFICATIONS ===

def notify(title: str, message: str, sound: str = "Glass"):
    subprocess.Popen(["osascript", "-e",
        f'display notification "{message}" with title "{title}" sound name "{sound}"'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class HealthMonitor:
    """Monitors battery, stillness, HR extremes, and sends macOS notifications."""
    def __init__(self):
        self.battery_pct: int = 100
        self.battery_warned: bool = False
        self.battery_critical: bool = False
        self.last_move_reminder: float = 0
        self.MOVE_REMIND_INTERVAL: float = 1800  # 30 min
        self.last_hr_alert: float = 0
        self.last_status_notify: float = 0

    def update_battery(self, pct: int, now: float):
        self.battery_pct = pct
        if pct <= 10 and not self.battery_critical:
            self.battery_critical = True
            notify("Polar H10 Battery Critical", f"Battery at {pct}% — replace soon!", "Basso")
            print(f"\n  🔋 BATTERY CRITICAL: {pct}%")
        elif pct <= 25 and not self.battery_warned:
            self.battery_warned = True
            notify("Polar H10 Battery Low", f"Battery at {pct}%", "Glass")
            print(f"\n  🔋 Battery low: {pct}%")

    def check_stillness(self, stillness: float, still_duration_s: float, now: float):
        if stillness > 0.85 and still_duration_s > 1800 and now - self.last_move_reminder > self.MOVE_REMIND_INTERVAL:
            self.last_move_reminder = now
            mins = int(still_duration_s / 60)
            notify("Time to Move", f"You've been still for {mins} minutes. Stand up, stretch, walk around.", "Purr")

    def check_hr(self, hr: float, now: float):
        if now - self.last_hr_alert < 300:  # 5 min cooldown
            return
        if hr > 120 and now - self.last_hr_alert > 60:
            # Only alert if sustained (not exercise)
            pass  # HR alerts are handled by the alert engine
        elif hr < 45 and hr > 0:
            self.last_hr_alert = now
            notify("Low Heart Rate", f"HR dropped to {hr:.0f} bpm", "Glass")


# === MOVEMENT / STILLNESS ===

@dataclass
class MovementTracker:
    mag_buffer: deque = field(default_factory=lambda: deque(maxlen=250))  # 5s at 50Hz
    breath_zero_crossings: deque = field(default_factory=lambda: deque(maxlen=60))
    breath_signal: float = 0.0
    breath_baseline: float = 0.0
    stillness: float = 1.0
    magnitude: float = 1.0
    breath_rate: float = 0.0
    last_breath_sign: bool = False

    # stillness alert
    still_since: float = 0.0
    still_alerted: bool = False
    STILL_THRESHOLD: float = 0.85
    STILL_ALERT_MINUTES: float = 30.0

    def add_acc_samples(self, samples: list, now: float):
        for x, y, z in samples:
            mag = math.sqrt(x*x + y*y + z*z) / 1000.0  # normalize to g
            self.mag_buffer.append(mag)

        if len(self.mag_buffer) < 50:
            return

        # stillness: 1 - mean deviation from 1g
        recent = list(self.mag_buffer)[-250:]
        self.magnitude = sum(recent) / len(recent)
        deviations = [abs(m - 1.0) for m in recent]
        self.stillness = max(0, 1.0 - sum(deviations) / len(deviations) * 10)

        # breath from Z-axis (simplified)
        if samples:
            z_val = samples[-1][2] / 1000.0
            alpha_lp = 0.1
            alpha_bl = 0.005
            self.breath_signal = self.breath_signal * (1 - alpha_lp) + z_val * alpha_lp
            self.breath_baseline = self.breath_baseline * (1 - alpha_bl) + self.breath_signal * alpha_bl
            centered = self.breath_signal - self.breath_baseline
            sign = centered > 0
            if sign and not self.last_breath_sign:
                self.breath_zero_crossings.append(now)
            self.last_breath_sign = sign

        # breath rate from zero crossings in last 30s
        cutoff = now - 30
        valid = [t for t in self.breath_zero_crossings if t > cutoff]
        self.breath_rate = len(valid) * 2.0  # breaths per minute

    def check_stillness_alert(self, now: float) -> dict | None:
        if self.stillness > self.STILL_THRESHOLD:
            if self.still_since == 0:
                self.still_since = now
            elif not self.still_alerted:
                duration = (now - self.still_since) / 60.0
                if duration >= self.STILL_ALERT_MINUTES:
                    self.still_alerted = True
                    return {"ts": now, "still_duration_s": duration * 60}
        else:
            self.still_since = 0
            self.still_alerted = False
        return None


# === ALERT ENGINE ===

@dataclass
class AlertEngine:
    """Detects conditions that should grab the user's attention."""
    # Sustained decline: RMSSD trending down for N seconds
    decline_start: float = 0.0
    decline_alerted_at: float = 0.0
    DECLINE_SECONDS: float = 45.0  # alert after 45s of steady decline
    DECLINE_COOLDOWN: float = 120.0  # don't re-alert for 2 min

    # HR spike
    hr_baseline: float = 70.0
    spike_alerted_at: float = 0.0
    SPIKE_THRESHOLD: float = 20.0  # bpm above baseline
    SPIKE_COOLDOWN: float = 60.0

    # Recovery (positive reinforcement)
    was_stressed: bool = False
    recovery_alerted_at: float = 0.0
    RECOVERY_COOLDOWN: float = 180.0

    def update(self, hrv: HRVEngine, now: float) -> str | None:
        """Returns alert type or None: 'decline', 'spike', 'recovery'."""
        # HR baseline (slow EMA)
        if hrv.hr_from_rr > 0:
            self.hr_baseline = self.hr_baseline * 0.995 + hrv.hr_from_rr * 0.005

        # Sustained decline
        if hrv.trend < -0.3:
            if self.decline_start == 0:
                self.decline_start = now
            elif (now - self.decline_start >= self.DECLINE_SECONDS and
                  now - self.decline_alerted_at >= self.DECLINE_COOLDOWN):
                self.decline_alerted_at = now
                self.decline_start = 0
                return "decline"
        else:
            self.decline_start = 0

        # HR spike
        if (hrv.hr_from_rr > self.hr_baseline + self.SPIKE_THRESHOLD and
                now - self.spike_alerted_at >= self.SPIKE_COOLDOWN):
            self.spike_alerted_at = now
            return "spike"

        # Recovery: was stressed, now recovering
        if hrv.drop_intensity > 0.4:
            self.was_stressed = True
        elif self.was_stressed and hrv.drop_intensity < 0.15:
            self.was_stressed = False
            if now - self.recovery_alerted_at >= self.RECOVERY_COOLDOWN:
                self.recovery_alerted_at = now
                return "recovery"

        return None


# === LIGHT OUTPUT ===

# Modes: training (reactive), monitor (subtle, alerts only), focus (minimal)
LIGHT_MODES = {
    "training": {"reactivity": 0.08, "bri_range": (0.2, 0.8), "pulse_range": (6, 18)},
    "monitor":  {"reactivity": 0.02, "bri_range": (0.4, 0.6), "pulse_range": (4, 8)},
    "focus":    {"reactivity": 0.01, "bri_range": (0.3, 0.5), "pulse_range": (4, 6)},
}

@dataclass
class LightState:
    hue: float = 0.0
    saturation: float = 1.0
    brightness: float = 0.7
    pulse_bpm: float = 6.0
    mode: str = "training"

    # Alert flash state
    alert_flash: float = 0.0
    alert_color: tuple = (255, 255, 255)
    _last_rmssd: float = 30.0

    def fire_alert(self, alert_type: str):
        self.alert_flash = 1.0
        if alert_type == "decline":
            self.alert_color = (100, 50, 255)  # purple flash
        elif alert_type == "spike":
            self.alert_color = (255, 80, 0)  # orange flash
        elif alert_type == "recovery":
            self.alert_color = (0, 255, 100)  # green flash

    def update(self, hrv: HRVEngine, movement: MovementTracker):
        cfg = LIGHT_MODES[self.mode]
        drop = hrv.drop_intensity
        trend_down = max(0, -hrv.trend) / 2.0
        self._last_rmssd = hrv.current_rmssd

        bri_lo, bri_hi = cfg["bri_range"]
        target_bri = bri_hi - drop * (bri_hi - bri_lo)
        self.brightness += (target_bri - self.brightness) * cfg["reactivity"]

        pulse_lo, pulse_hi = cfg["pulse_range"]
        target_bpm = pulse_lo + drop * (pulse_hi - pulse_lo) + trend_down * 4.0
        self.pulse_bpm += (target_bpm - self.pulse_bpm) * cfg["reactivity"]

        self.hue = 0.02 * (1.0 - drop)
        self.saturation = 0.85 + drop * 0.15

        # Decay alert flash
        if self.alert_flash > 0.01:
            self.alert_flash *= 0.92
        else:
            self.alert_flash = 0

    @property
    def rgb(self) -> tuple[int, int, int]:
        r, g, b = colorsys.hsv_to_rgb(self.hue, self.saturation, self.brightness)
        return (int(r * 255), int(g * 255), int(b * 255))

    def hue_api_state(self, t: float) -> dict:
        phase = (t * self.pulse_bpm / 60.0) * math.pi * 2
        mod = 0.6 + 0.4 * (math.sin(phase) + 1) / 2
        bri = max(1, int(self.brightness * mod * 254))
        return {
            "on": True,
            "hue": int(self.hue * 65535) % 65535,
            "sat": int(self.saturation * 254),
            "bri": bri,
            "transitiontime": max(4, int(12 - self.pulse_bpm / 3)),
        }

    def hue_gradient_states(self, t: float, drop: float, n_lights: int) -> list[dict]:
        """N lights arranged closest→furthest. Color migrates from warm to cool.
        Light 0 (closest): warm red, stays warmest longest.
        Light N-1 (furthest): cool blue, first to turn blue.
        As stress rises, blue creeps inward from the far light toward you."""
        tt = max(4, int(12 - self.pulse_bpm / 3))
        states = []
        for i in range(n_lights):
            # Position 0..1 from closest to furthest
            pos = i / max(1, n_lights - 1) if n_lights > 1 else 0
            # Phase offset per light for organic breathing
            phase = (t * self.pulse_bpm / 60.0) * math.pi * 2 + pos * 0.6
            mod = 0.6 + 0.4 * (math.sin(phase) + 1) / 2
            # Slow drift so gradient gently shifts even when stable
            drift = math.sin(t * 0.08 + pos * 1.5) * 0.06
            # Absolute RMSSD → blue amount. Each light has its own range.
            # Closest stays red longest, furthest goes blue first.
            rmssd_red = 20 + pos * 20   # closest: red above 20, far: red above 40
            rmssd_blue = 5 + pos * 10   # closest: full blue at 5, far: full blue at 15
            abs_blue = max(0, min(1, (rmssd_red - self._last_rmssd) / max(1, rmssd_red - rmssd_blue) + drift))
            # Interpolate hue: 0 (red) → 46920 (blue)
            h = int(abs_blue * 46920)
            # Brightness: closest stays brightest
            closeness = 1.0 - pos * 0.25
            bri = max(1, int(self.brightness * mod * closeness * 254))
            states.append({
                "on": True,
                "hue": h,
                "sat": 254,
                "bri": bri,
                "transitiontime": tt,
            })
        return states

    def buzzer_rgb(self, t: float) -> tuple[int, int, int]:
        phase = (t * self.pulse_bpm / 60.0) * math.pi * 2
        mod = 0.6 + 0.4 * (math.sin(phase) + 1) / 2
        r, g, b = self.rgb
        return (int(r * mod), int(g * mod), int(b * mod))


# === CONTEXT-AWARE KNOB ROUTING ===

CONTEXT_ROUTES = {
    "Safari": {"rotate": "scroll_page", "press": "mark_moment"},
    "Google Chrome": {"rotate": "scroll_page", "press": "mark_moment"},
    "Firefox": {"rotate": "scroll_page", "press": "mark_moment"},
    "Arc": {"rotate": "scroll_page", "press": "mark_moment"},
    "Code": {"rotate": "font_size", "press": "toggle_zen"},
    "Terminal": {"rotate": "history_cycle", "press": "mark_moment"},
    "iTerm2": {"rotate": "history_cycle", "press": "mark_moment"},
    "Alacritty": {"rotate": "history_cycle", "press": "mark_moment"},
    "kitty": {"rotate": "history_cycle", "press": "mark_moment"},
    "Spotify": {"rotate": "volume", "press": "play_pause"},
    "Music": {"rotate": "volume", "press": "play_pause"},
    "zoom.us": {"rotate": "volume", "press": "mute_toggle"},
    "FaceTime": {"rotate": "volume", "press": "mute_toggle"},
    "_overlay": {"rotate": "timeline_zoom", "press": "mark_moment"},
    "_default": {"rotate": "sensitivity", "press": "mark_moment"},
}

# Context color categories for segment 2
CONTEXT_COLORS = {
    "Safari": (0, 80, 255),        # blue = browser
    "Google Chrome": (0, 80, 255),  # blue = browser
    "Code": (160, 32, 240),         # purple = code
    "Spotify": (0, 200, 80),        # green = music
    "Music": (0, 200, 80),          # green = music
    "zoom.us": (255, 140, 0),       # orange = comms
    "FaceTime": (255, 140, 0),      # orange = comms
    "Terminal": (100, 255, 100),    # green = terminal
    "iTerm2": (100, 255, 100),
    "Alacritty": (100, 255, 100),
    "kitty": (100, 255, 100),
    "_default": (255, 255, 255),    # white = default
}


def get_frontmost_app() -> str:
    try:
        result = subprocess.run(
            ["osascript", "-e",
             'tell application "System Events" to get name of first process whose frontmost is true'],
            capture_output=True, text=True, timeout=2
        )
        return result.stdout.strip()
    except Exception:
        return ""


class KnobRouter:
    """Routes buzzer inputs based on the frontmost macOS app."""

    def __init__(self, hrv: 'HRVEngine', db: sqlite3.Connection, session_id: int,
                 buzzer: 'BuzzerIO'):
        self.hrv = hrv
        self.db = db
        self.session_id = session_id
        self.buzzer = buzzer
        self.cached_app: str = ""
        self.touch_active: bool = False
        # Scroll momentum
        self._scroll_acc = 0.0
        self._scroll_last = 0.0
        # Double-tap detection
        self._last_press_time = 0.0
        self._DOUBLE_TAP_WINDOW = 0.4  # seconds

    def _get_route(self) -> dict:
        app = self.cached_app or "_default"
        return CONTEXT_ROUTES.get(app, CONTEXT_ROUTES["_default"])

    def on_touch(self, touched: bool):
        self.touch_active = touched
        if touched:
            self.cached_app = get_frontmost_app()
        else:
            # keep cache until next touch

            pass

    def on_rotate(self, diff: int):
        route = self._get_route()
        action = route.get("rotate", "sensitivity")
        self._exec_rotate(action, diff)

    def on_press(self, pressed: bool):
        if pressed:
            now = time.time()
            if now - self._last_press_time < self._DOUBLE_TAP_WINDOW:
                self._last_press_time = 0
                self._pending_single = False
                self._toggle_dictation()
                return
            self._last_press_time = now
            self._pending_single = True
        else:
            # On release: if single tap is still pending and window has passed, fire it
            pass

    def check_pending_press(self):
        """Call from main loop to fire delayed single-tap actions."""
        if not getattr(self, '_pending_single', False):
            return
        if time.time() - self._last_press_time >= self._DOUBLE_TAP_WINDOW:
            self._pending_single = False
            route = self._get_route()
            action = route.get("press", "mark_moment")
            self._exec_press(action)

    # --- Rotate actions ---

    def _exec_rotate(self, action: str, diff: int):
        if action == "scroll_page":
            self._scroll_page(diff)
        elif action == "font_size":
            self._font_size(diff)
        elif action == "volume":
            self._volume(diff)
        elif action == "timeline_zoom":
            self._timeline_zoom(diff)
        elif action == "sensitivity":
            self._sensitivity(diff)
        elif action == "history_cycle":
            self._history_cycle(diff)
        else:
            self._sensitivity(diff)

    def _scroll_page(self, diff: int):
        now = time.time()
        # Exponential acceleration: rapid consecutive rotations → bigger jumps
        if now - self._scroll_last < 0.3:
            self._scroll_acc = min(self._scroll_acc + abs(diff), 20)
        else:
            self._scroll_acc = abs(diff)
        self._scroll_last = now

        # Exponential: base scroll + acceleration^1.5
        multiplier = 1 + self._scroll_acc ** 1.5
        pixels = int(diff * multiplier * 40)
        script = f'''
            tell application "System Events"
                set scrollDir to {pixels}
                tell process (name of first process whose frontmost is true)
                    -- Use scroll wheel event for smooth scrolling
                end tell
            end tell
        '''
        # Use cliclick or applescript scroll wheel for pixel-level scrolling
        # Fallback: arrow keys with repeat for reliability
        direction = "down" if diff > 0 else "up"
        amount = max(1, int(abs(diff) * multiplier))
        script = f'''
            tell application "System Events"
                repeat {amount} times
                    key code {125 if direction == "down" else 126}
                end repeat
            end tell
        '''
        subprocess.Popen(["osascript", "-e", script],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _font_size(self, diff: int):
        if diff > 0:
            key = "keystroke \"=\" using command down"
        else:
            key = "keystroke \"-\" using command down"
        repeat = abs(diff)
        script = f'''
            tell application "System Events"
                repeat {repeat} times
                    {key}
                end repeat
            end tell
        '''
        subprocess.Popen(["osascript", "-e", script],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _volume(self, diff: int):
        script = f'''
            set curVol to output volume of (get volume settings)
            set newVol to curVol + ({diff} * 2)
            if newVol < 0 then set newVol to 0
            if newVol > 100 then set newVol to 100
            set volume output volume newVol
        '''
        subprocess.Popen(["osascript", "-e", script],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _history_cycle(self, diff: int):
        # Up arrow = previous command, Down arrow = next command
        repeat = abs(diff)
        key = 126 if diff < 0 else 125  # up arrow for prev, down for next
        script = f'''
            tell application "System Events"
                repeat {repeat} times
                    key code {key}
                end repeat
            end tell
        '''
        subprocess.Popen(["osascript", "-e", script],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _timeline_zoom(self, diff: int):
        print(f"\n  ⏩ Timeline zoom: {'+' if diff > 0 else ''}{diff}")

    def _sensitivity(self, diff: int):
        self.hrv.baseline_rmssd += diff * 0.5
        self.hrv.baseline_rmssd = max(10, min(150, self.hrv.baseline_rmssd))

    # --- Press actions ---

    def _exec_press(self, action: str):
        if action == "mark_moment":
            self._mark_moment()
        elif action == "play_pause":
            self._play_pause()
        elif action == "mute_toggle":
            self._mute_toggle()
        elif action == "toggle_zen":
            self._toggle_zen()
        else:
            self._mark_moment()

    def _mark_moment(self):
        now = time.time()
        self.db.execute(
            "INSERT INTO markers (session_id, ts, label, hr_bpm, rmssd) VALUES (?,?,?,?,?)",
            (self.session_id, now, "manual", int(self.hrv.hr_from_rr), self.hrv.current_rmssd)
        )
        self.db.commit()
        print(f"\n  ★ Marker saved (HR {self.hrv.hr_from_rr:.0f}, RMSSD {self.hrv.current_rmssd:.1f})")
        if self.buzzer.available:
            self.buzzer.set_rgb(255, 255, 255)

    def _play_pause(self):
        app = self.cached_app or "Spotify"
        script = f'''
            tell application "{app}" to playpause
        '''
        subprocess.Popen(["osascript", "-e", script],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print(f"\n  ▶⏸ Play/Pause → {app}")

    def _mute_toggle(self):
        script = '''
            tell application "System Events"
                keystroke "a" using {shift down, command down}
            end tell
        '''
        subprocess.Popen(["osascript", "-e", script],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print(f"\n  🔇 Mute toggle → {self.cached_app}")

    def _toggle_dictation(self):
        script = '''
            tell application "System Events"
                key code 63
                delay 0.1
                key code 63
            end tell
        '''
        subprocess.Popen(["osascript", "-e", script],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print("\n  🎤 Dictation toggle")
        # Ripple effect: buzzer segments cascade, then Hue lights near→far
        self._fire_ripple()

    def _fire_ripple(self):
        """Ripple outward: buzzer seg 0→1→2, then Hue lights near→far."""
        import threading
        def _ripple():
            # Buzzer segments: cascade white flash
            if self.buzzer.available:
                for seg in range(3):
                    colors = [(0,0,0)]*3
                    colors[seg] = (255, 255, 255)
                    self.buzzer.set_segments(colors)
                    time.sleep(0.08)
                # All white briefly
                self.buzzer.set_rgb(255, 255, 255)
                time.sleep(0.15)
                # Ease out
                for step in range(8):
                    t = step / 7
                    # Cubic ease-out
                    v = 1 - (1 - t) ** 3
                    bri = int(255 * (1 - v))
                    self.buzzer.set_rgb(bri, bri, bri)
                    time.sleep(0.04)
                self.buzzer.set_rgb(0, 0, 0)
        threading.Thread(target=_ripple, daemon=True).start()

    def _toggle_zen(self):
        # VS Code zen mode: Cmd+K then Cmd+Z
        script = '''
            tell application "System Events"
                keystroke "k" using command down
                delay 0.1
                keystroke "z" using command down
            end tell
        '''
        subprocess.Popen(["osascript", "-e", script],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print("\n  🧘 Toggle Zen Mode")

    # --- LED segment colors ---

    def get_context_color(self) -> tuple[int, int, int]:
        """Return the color for segment 2 based on current context."""
        app = self.cached_app or "_default"
        return CONTEXT_COLORS.get(app, CONTEXT_COLORS["_default"])

    @staticmethod
    def get_trend_color(trend: float) -> tuple[int, int, int]:
        """Return color for segment 1: green=rising, red=dropping, amber=stable."""
        if trend > 0.3:
            return (0, 200, 0)      # green — rising
        elif trend < -0.3:
            return (200, 0, 0)      # red — dropping
        else:
            return (200, 150, 0)    # amber — stable


# === HUE BRIDGE ===

class HueBridge:
    def __init__(self, group: int = 0, lights: list[int] | None = None):
        self.group = group
        self.lights = lights  # individual light ids for dual-light mode
        self.cfg = None
        self.last_update = 0
        self.last_update_b = 0

    def connect(self) -> bool:
        if not os.path.exists(HUE_CONFIG):
            print("  Hue: no config found")
            return False
        with open(HUE_CONFIG) as f:
            self.cfg = json.load(f)
        try:
            self._api("GET", "/lights")
            if self.lights:
                print(f"  Hue: connected (lights {self.lights})")
            else:
                print(f"  Hue: connected (group {self.group})")
            return True
        except Exception as e:
            print(f"  Hue: unreachable ({e})")
            return False

    def set_state(self, state: dict):
        """Set state on group or first light."""
        now = time.time()
        if now - self.last_update < 0.8:
            return
        self.last_update = now
        try:
            if self.lights:
                self._api("PUT", f"/lights/{self.lights[0]}/state", state)
            else:
                self._api("PUT", f"/groups/{self.group}/action", state)
        except Exception:
            pass

    def set_gradient(self, states: list[dict]):
        """Set N lights with individual states. Matches self.lights order.
        Spaces calls to avoid bridge rate limit (~10 cmd/s per light)."""
        if not self.lights:
            if states:
                self.set_state(states[0])
            return
        now = time.time()
        # Need ~0.15s per light, so total interval scales with count
        min_interval = 0.15 * len(self.lights) + 0.3
        if now - self.last_update < min_interval:
            return
        self.last_update = now
        try:
            for lid, state in zip(self.lights, states):
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


# === TIMEBUZZER ===

class BuzzerIO:
    def __init__(self):
        self.mo = None
        self.mi = None
        self.available = False
        self.last_position = 64
        self.touch = False
        self.pressed = False
        self.on_rotate = None  # callback(direction: int)  +1/-1
        self.on_press = None   # callback(pressed: bool)
        self.on_touch = None   # callback(touched: bool)

    def connect(self) -> bool:
        try:
            import rtmidi
        except ImportError:
            print("  Buzzer: python-rtmidi not installed")
            return False

        self.mo = rtmidi.MidiOut()
        self.mi = rtmidi.MidiIn()

        out_port = in_port = None
        for i, name in enumerate(self.mo.get_ports()):
            if "timeBuzzer" in name:
                out_port = i
                break
        for i, name in enumerate(self.mi.get_ports()):
            if "timeBuzzer" in name:
                in_port = i
                break

        if out_port is None or in_port is None:
            print("  Buzzer: not found")
            return False

        self.mo.open_port(out_port)
        self.mi.open_port(in_port)
        self.mi.ignore_types(False, False, False)

        # initialize position tracker
        self.mo.send_message([187, 80, 64])
        self.last_position = 64
        self.available = True
        print("  Buzzer: connected (input + output)")
        return True

    def poll(self):
        if not self.available:
            return
        while True:
            msg = self.mi.get_message()
            if not msg:
                break
            data, _ = msg
            if data[0] != 187:
                continue
            cc, val = data[1], data[2]
            if cc == 80:  # rotation
                diff = val - self.last_position
                # handle wraparound
                if diff > 64:
                    diff -= 128
                elif diff < -64:
                    diff += 128
                if diff != 0 and self.on_rotate:
                    self.on_rotate(diff)
                self.last_position = val
            elif cc == 81:  # touch
                touched = val == 0
                if touched != self.touch:
                    self.touch = touched
                    if self.on_touch:
                        self.on_touch(touched)
            elif cc == 82:  # press
                pressed = val == 127
                if pressed != self.pressed:
                    self.pressed = pressed
                    if self.on_press:
                        self.on_press(pressed)

    def set_rgb(self, r: int, g: int, b: int):
        if not self.available:
            return
        for seg in range(3):
            cc_base = 70 + 3 * seg
            self.mo.send_message([187, cc_base, r // 2])
            self.mo.send_message([187, cc_base + 1, g // 2])
            self.mo.send_message([187, cc_base + 2, b // 2])

    def set_segments(self, colors: list[tuple[int, int, int]]):
        """Set each segment independently. colors = [(r,g,b), (r,g,b), (r,g,b)]"""
        if not self.available:
            return
        for seg, (r, g, b) in enumerate(colors[:3]):
            cc_base = 70 + 3 * seg
            self.mo.send_message([187, cc_base, r // 2])
            self.mo.send_message([187, cc_base + 1, g // 2])
            self.mo.send_message([187, cc_base + 2, b // 2])


# === MAIN DAEMON ===

async def run(args):
    import websockets

    # Database
    db = init_db(args.db)
    session_id = db.execute(
        "INSERT INTO sessions (started_at) VALUES (?)",
        (datetime.now(timezone.utc).isoformat(),)
    ).lastrowid
    db.commit()
    print(f"\n{'='*50}")
    print(f"  HRV Daemon — session #{session_id}")
    print(f"  DB: {args.db}")
    print(f"{'='*50}\n")

    # Engines
    hrv = HRVEngine()
    movement = MovementTracker()
    alerts = AlertEngine()
    advanced = AdvancedAnalyzer()
    health = HealthMonitor()
    light = LightState(mode=args.mode)

    # Hardware
    hue_lights = [int(x) for x in args.lights.split(",")] if args.lights else None
    hue = HueBridge(group=args.hue_group, lights=hue_lights)
    hue_ok = not args.no_lights and hue.connect()

    buzzer = BuzzerIO()
    buzzer_ok = not args.no_buzzer and buzzer.connect()

    # Context-aware buzzer knob routing
    knob_router = KnobRouter(hrv=hrv, db=db, session_id=session_id, buzzer=buzzer)

    if buzzer_ok:
        buzzer.on_press = knob_router.on_press
        buzzer.on_rotate = knob_router.on_rotate
        buzzer.on_touch = knob_router.on_touch

    # Timers
    start_time = time.time()
    last_light_update = 0
    last_hrv_save = 0
    last_move_save = 0
    last_beat_time = 0
    last_buzzer_fade = 0
    rr_batch = []

    print(f"  Connecting to {WS_URL}…\n")

    while True:
        try:
            async with websockets.connect(WS_URL) as ws:
                print("  ✓ Bridge connected — streaming\n")
                async for raw in ws:
                    msg = json.loads(raw)
                    now = time.time()
                    t = now - start_time

                    # Poll buzzer input
                    if buzzer_ok:
                        buzzer.poll()
                        knob_router.check_pending_press()

                    # --- HR + RR ---
                    if msg.get("type") == "hr":
                        hr_bpm = msg.get("bpm", 0)
                        for rr in msg.get("rr", []):
                            updated = hrv.add_rr(rr, now)
                            rr_batch.append((session_id, now, rr, hr_bpm))

                            if not updated:
                                continue

                            # Check stress
                            stress_event = hrv.check_stress(now)
                            if stress_event:
                                db.execute(
                                    "INSERT INTO stress_events (session_id, ts, duration_s, severity, rmssd_at_event, baseline_at_event, trigger_type) VALUES (?,?,?,?,?,?,?)",
                                    (session_id, stress_event["ts"], stress_event["duration_s"],
                                     stress_event["severity"], stress_event["rmssd_at_event"],
                                     stress_event["baseline_at_event"], "hrv_drop")
                                )
                                db.commit()
                                duration = stress_event["duration_s"]
                                sev = stress_event["severity"]
                                print(f"\n  ⚠ Stress event: {duration:.0f}s, severity {sev:.0%}")

                            # Check alerts (attention-grabbing events)
                            alert = alerts.update(hrv, now)
                            if alert:
                                light.fire_alert(alert)
                                print(f"\n  ⚡ ALERT: {alert}")
                                if buzzer_ok:
                                    # Triple flash for alerts
                                    ac = {"decline": (100,50,255), "spike": (255,80,0), "recovery": (0,255,100)}
                                    ar, ag, ab = ac.get(alert, (255,255,255))
                                    for _ in range(3):
                                        buzzer.set_rgb(ar, ag, ab)
                                        await asyncio.sleep(0.12)
                                        buzzer.set_rgb(0, 0, 0)
                                        await asyncio.sleep(0.08)

                            # Save HRV sample every 5s
                            if now - last_hrv_save >= 5:
                                last_hrv_save = now
                                db.execute(
                                    "INSERT INTO hrv_samples (session_id, ts, rmssd, sdnn, hr_mean, baseline_rmssd, relative_hrv, trend_slope) VALUES (?,?,?,?,?,?,?,?)",
                                    (session_id, now, hrv.current_rmssd, hrv.current_sdnn,
                                     hrv.hr_from_rr, hrv.baseline_rmssd, hrv.relative_hrv, hrv.trend)
                                )

                            # Advanced analysis every 30s
                            if advanced.update(hrv.rr_buffer, hrv.current_rmssd, now):
                                db.execute(
                                    "INSERT INTO hrv_advanced (session_id, ts, ln_rmssd, dfa_alpha1, sample_entropy, sd1, sd2, pnn50) VALUES (?,?,?,?,?,?,?,?)",
                                    (session_id, now, advanced.ln_rmssd, advanced.dfa_alpha1,
                                     advanced.sample_ent, advanced.sd1, advanced.sd2, advanced.pnn50)
                                )
                                db.commit()

                            # Update Hue lights (slow, 0.5s)
                            if now - last_light_update >= 0.5:
                                last_light_update = now
                                light.update(hrv, movement)
                                if hue_ok:
                                    if hue.lights:
                                        states = light.hue_gradient_states(t, hrv.drop_intensity, len(hue.lights))
                                        hue.set_gradient(states)
                                    else:
                                        hue.set_state(light.hue_api_state(t))

                            # Buzzer: flash on each heartbeat
                            if buzzer_ok and not buzzer.pressed:
                                last_beat_time = now

                                # Console
                                r, g, b = light.rgb
                                trend_arrow = "↗" if hrv.trend > 0.3 else "↘" if hrv.trend < -0.3 else "→"
                                stress_ind = "●" if hrv.stress_active else "○"
                                adv_str = advanced.log_line() if advanced.dfa_alpha1 else ""
                                print(
                                    f"\r  {stress_ind} HR {hrv.hr_from_rr:3.0f}  "
                                    f"RMSSD {hrv.current_rmssd:5.1f} {trend_arrow}  "
                                    f"drop {hrv.drop_intensity*100:2.0f}%  "
                                    f"{adv_str}  "
                                    f"[{light.mode}]",
                                    end="", flush=True
                                )

                        # Batch save RR intervals
                        if len(rr_batch) >= 20:
                            db.executemany(
                                "INSERT INTO rr_intervals (session_id, ts, rr_ms, hr_bpm) VALUES (?,?,?,?)",
                                rr_batch
                            )
                            db.commit()
                            rr_batch.clear()

                    # --- BUZZER HEARTBEAT FADE (smooth sine-shaped pulse) ---
                    if buzzer_ok and not buzzer.pressed and last_beat_time > 0:
                        if now - last_buzzer_fade >= 0.03:
                            last_buzzer_fade = now
                            expected_interval = 60.0 / max(40, hrv.hr_from_rr) if hrv.hr_from_rr > 0 else 1.0
                            elapsed = now - last_beat_time
                            # Smooth pulse: sine-shaped rise then gentle fall
                            phase = elapsed / expected_interval
                            if phase < 0.15:
                                # rise (soft onset)
                                fade = math.sin(phase / 0.15 * math.pi / 2)
                            elif phase < 0.5:
                                # smooth decay
                                fade = math.cos((phase - 0.15) / 0.35 * math.pi / 2)
                            else:
                                # dim rest between beats
                                fade = max(0.05, 0.15 * math.exp(-(phase - 0.5) * 3))
                            # Color: red (healthy) → blue (stressed)
                            drop = hrv.drop_intensity
                            base_r = int(255 * (1 - drop))
                            base_g = int(20 * (1 - drop))
                            base_b = int(255 * drop)
                            r = int(base_r * fade)
                            g = int(base_g * fade)
                            b = int(base_b * fade)
                            buzzer.set_rgb(r, g, b)

                    # --- ACC ---
                    elif msg.get("type") == "acc":
                        samples = msg.get("samples", [])
                        movement.add_acc_samples(samples, now)

                        # Save movement every 10s
                        if now - last_move_save >= 10:
                            last_move_save = now
                            db.execute(
                                "INSERT INTO movement_samples (session_id, ts, stillness, magnitude, breath_rate) VALUES (?,?,?,?,?)",
                                (session_id, now, movement.stillness, movement.magnitude, movement.breath_rate)
                            )
                            db.commit()

                            # Stillness alert
                            alert = movement.check_stillness_alert(now)
                            if alert:
                                db.execute(
                                    "INSERT INTO stillness_alerts (session_id, ts, still_duration_s) VALUES (?,?,?)",
                                    (session_id, alert["ts"], alert["still_duration_s"])
                                )
                                db.commit()
                                mins = alert["still_duration_s"] / 60
                                print(f"\n  🦶 Still for {mins:.0f} min — time to move!")
                                # flash buzzer orange + notification
                                if buzzer_ok:
                                    buzzer.set_rgb(255, 127, 0)
                                still_dur = (now - movement.still_since) if movement.still_since > 0 else 0
                                health.check_stillness(movement.stillness, still_dur, now)

                    # --- BATTERY ---
                    elif msg.get("type") == "battery":
                        health.update_battery(msg.get("pct", 100), now)

                    # --- HR monitoring ---
                    if hrv.hr_from_rr > 0:
                        health.check_hr(hrv.hr_from_rr, now)

        except ConnectionRefusedError:
            print("\r  Bridge not running. Retrying in 3s…", end="", flush=True)
            await asyncio.sleep(3)
        except Exception as e:
            print(f"\n  Connection lost ({e}). Reconnecting…")
            # flush remaining RR
            if rr_batch:
                db.executemany(
                    "INSERT INTO rr_intervals (session_id, ts, rr_ms, hr_bpm) VALUES (?,?,?,?)",
                    rr_batch
                )
                db.commit()
                rr_batch.clear()
            await asyncio.sleep(2)


def main():
    parser = argparse.ArgumentParser(description="HRV Daemon")
    parser.add_argument("--hue-group", type=int, default=0)
    parser.add_argument("--no-buzzer", action="store_true")
    parser.add_argument("--no-lights", action="store_true")
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--mode", choices=["training", "monitor", "focus"], default="training",
                        help="training=reactive, monitor=subtle+alerts, focus=minimal")
    parser.add_argument("--lights", default=None,
                        help="Comma-separated light IDs for dual mode (e.g. 2,3)")
    args = parser.parse_args()

    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\n\n  Session ended.")
        # close session
        db = sqlite3.connect(args.db)
        db.execute(
            "UPDATE sessions SET ended_at = ? WHERE ended_at IS NULL",
            (datetime.now(timezone.utc).isoformat(),)
        )
        db.commit()
        db.close()


if __name__ == "__main__":
    main()
