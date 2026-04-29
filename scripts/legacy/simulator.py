#!/usr/bin/env python3
"""
Polar H10 WebSocket Simulator
Generates realistic HR/RR/ACC/ECG data for testing without a chest strap.
"""

import asyncio
import argparse
import json
import math
import random
import time

import websockets

# --- State definitions ---
STATES = {
    "calm": {"hr_min": 62, "hr_max": 68, "rmssd_min": 50, "rmssd_max": 70},
    "focus": {"hr_min": 70, "hr_max": 78, "rmssd_min": 35, "rmssd_max": 50},
    "stress": {"hr_min": 80, "hr_max": 95, "rmssd_min": 20, "rmssd_max": 35},
    "recovery": {"hr_min": 68, "hr_max": 78, "rmssd_min": 35, "rmssd_max": 55},
}

STATE_ORDER = ["calm", "focus", "stress", "recovery"]


class PhysiologicalModel:
    """Generates realistic HR/RR data with autocorrelated RR intervals."""

    def __init__(self):
        self.state_idx = 0
        self.state = "calm"
        self.target_hr = 65.0
        self.target_rmssd = 55.0
        self.current_hr = 65.0
        self.current_rmssd = 55.0
        self.prev_rr = 60000.0 / 65.0  # ms
        self.breath_phase = 0.0  # radians
        self.breath_rate = 14.0  # breaths per minute
        self.start_time = time.time()
        self.last_state_change = time.time()
        self.state_cycle_secs = 120

    def set_cycle(self, secs: int):
        self.state_cycle_secs = secs

    def update_state(self):
        now = time.time()
        elapsed = now - self.last_state_change
        if elapsed >= self.state_cycle_secs:
            self.state_idx = (self.state_idx + 1) % len(STATE_ORDER)
            self.state = STATE_ORDER[self.state_idx]
            s = STATES[self.state]
            self.target_hr = random.uniform(s["hr_min"], s["hr_max"])
            self.target_rmssd = random.uniform(s["rmssd_min"], s["rmssd_max"])
            self.last_state_change = now
            elapsed_total = now - self.start_time
            mins = int(elapsed_total) // 60
            secs = int(elapsed_total) % 60
            print(
                f"[{mins:02d}:{secs:02d}] State: {self.state} "
                f"(HR ~{self.target_hr:.0f}, RMSSD ~{self.target_rmssd:.0f})"
            )

    def generate_rr(self) -> float:
        """Generate next RR interval with realistic autocorrelation."""
        # Smoothly approach target HR
        self.current_hr += (self.target_hr - self.current_hr) * 0.02
        self.current_rmssd += (self.target_rmssd - self.current_rmssd) * 0.02

        base_rr = 60000.0 / self.current_hr

        # Noise variance derived from RMSSD (RMSSD ≈ sqrt of mean squared successive diffs)
        noise_std = self.current_rmssd / math.sqrt(2)

        # Autocorrelated RR: next = base + 0.7*(prev - base) + noise
        noise = random.gauss(0, noise_std)
        next_rr = base_rr + 0.7 * (self.prev_rr - base_rr) + noise

        # Clamp to physiological range
        next_rr = max(600.0, min(1100.0, next_rr))

        # Add respiratory sinus arrhythmia (~20ms modulation)
        self.breath_phase += (2 * math.pi * self.breath_rate / 60) * (next_rr / 1000)
        rsa = 15.0 * math.sin(self.breath_phase)
        next_rr += rsa

        next_rr = max(600.0, min(1100.0, next_rr))
        self.prev_rr = next_rr
        return next_rr

    def generate_hr_message(self) -> dict:
        """Generate an HR message with 1-3 RR intervals."""
        self.update_state()
        # Typically 1-2 beats per second
        num_rr = random.choices([1, 2, 3], weights=[0.3, 0.6, 0.1])[0]
        rr_intervals = [round(self.generate_rr()) for _ in range(num_rr)]
        avg_rr = sum(rr_intervals) / len(rr_intervals)
        bpm = round(60000.0 / avg_rr)
        return {"type": "hr", "bpm": bpm, "rr": rr_intervals}

    def generate_acc_samples(self) -> list:
        """Generate 10 ACC samples (50Hz batch = 200ms)."""
        samples = []
        t_now = time.time()
        for i in range(10):
            t = t_now + i * 0.02  # 50Hz
            # Base: near 1g on Z axis (chest strap)
            z_base = 1020  # ~1g in mg
            # Breathing oscillation on Z
            breath_freq = self.breath_rate / 60.0
            z_breath = 20 * math.sin(2 * math.pi * breath_freq * t)
            # Movement noise depends on state
            if self.state == "calm":
                noise_scale = 3.0
            elif self.state == "stress":
                noise_scale = 8.0
            else:
                noise_scale = 5.0

            x = round(random.gauss(0, noise_scale))
            y = round(random.gauss(0, noise_scale))
            z = round(z_base + z_breath + random.gauss(0, noise_scale * 0.5))
            samples.append([x, y, z])
        return samples

    def generate_ecg_samples(self) -> list:
        """Generate 13 ECG samples (130Hz batch = 100ms). Rough simulation."""
        samples = []
        # Time between R-peaks based on current HR
        rr_sec = 60.0 / self.current_hr
        t_now = time.time()
        for i in range(13):
            t = t_now + i / 130.0
            # Position in current cardiac cycle
            cycle_pos = (t % rr_sec) / rr_sec
            # Simple spike near cycle start (R-wave)
            if 0.0 < cycle_pos < 0.03:
                val = 800 + random.randint(-50, 50)
            elif 0.03 < cycle_pos < 0.06:
                val = -200 + random.randint(-30, 30)
            else:
                val = random.randint(-20, 20)
            samples.append(val)
        return samples


async def handle_client(websocket, model: PhysiologicalModel):
    """Handle a connected WebSocket client."""
    print(f"Client connected: {websocket.remote_address}")

    # Send device info
    await websocket.send(json.dumps({
        "type": "device",
        "manufacturer": "Polar",
        "model": "H10 (SIM)",
        "name": "Polar H10 SIM",
        "address": "00:00:00:00:00:00",
    }))
    await websocket.send(json.dumps({"type": "battery", "pct": 85}))

    # Timers for different message types
    last_hr = 0.0
    last_acc = 0.0
    last_ecg = 0.0

    try:
        while True:
            now = time.time()

            # HR every ~1s
            if now - last_hr >= 1.0:
                msg = model.generate_hr_message()
                await websocket.send(json.dumps(msg))
                last_hr = now

            # ACC every ~200ms
            if now - last_acc >= 0.2:
                samples = model.generate_acc_samples()
                await websocket.send(json.dumps({"type": "acc", "samples": samples}))
                last_acc = now

            # ECG every ~100ms
            if now - last_ecg >= 0.1:
                samples = model.generate_ecg_samples()
                await websocket.send(json.dumps({"type": "ecg", "samples": samples}))
                last_ecg = now

            await asyncio.sleep(0.05)
    except websockets.exceptions.ConnectionClosed:
        print(f"Client disconnected: {websocket.remote_address}")


async def main(port: int, stress_cycle: int):
    model = PhysiologicalModel()
    model.set_cycle(stress_cycle)

    # Print initial state
    print(f"Polar H10 Simulator starting on ws://localhost:{port}")
    print(f"State cycle: {stress_cycle}s per state")
    print(f"[00:00] State: {model.state} (HR ~{model.target_hr:.0f}, RMSSD ~{model.target_rmssd:.0f})")
    print()

    async with websockets.serve(
        lambda ws: handle_client(ws, model),
        "localhost",
        port,
    ):
        await asyncio.Future()  # run forever


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Polar H10 WebSocket Simulator")
    parser.add_argument("--port", type=int, default=8765, help="WebSocket port (default: 8765)")
    parser.add_argument("--stress-cycle", type=int, default=120, help="Seconds per state (default: 120)")
    args = parser.parse_args()

    try:
        asyncio.run(main(args.port, args.stress_cycle))
    except KeyboardInterrupt:
        print("\nSimulator stopped.")
