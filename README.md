# polar-h10-ribbon

Real-time HRV biofeedback system. Connects a Polar H10 chest strap to ambient lighting, a physical controller, and browser visualizations — turning your nervous system state into something you can see, feel, and train.

## What it does

- **Streams** ECG (130 Hz), heart rate, RR intervals, and accelerometer (50 Hz) from a Polar H10 via BLE
- **Computes** RMSSD, SDNN, HRV trends, stress detection, breathing rate, and stillness in real time
- **Drives ambient lighting** — Philips Hue lights form a warm→cool gradient that shifts with your HRV state
- **Heartbeat on your desk** — timeBuzzer LED pulses with each heartbeat, color reflects HRV (red = healthy, blue = stressed)
- **Browser dashboard** — Poincaré plot, ECG strip, HR/RMSSD timeline, breathing wave, coherence score
- **Breathing pacer** — guided breathing at multiple patterns (coherence, box, 4-7-8, resonance frequency)
- **Logs everything** to SQLite — RR intervals, HRV samples, stress events, movement, markers
- **Generates reports** — overnight analysis, session summaries, before/after comparisons
- **Internal memory** — start recording on the H10, disconnect, come back and download later

## Architecture

```
Polar H10 ──BLE──▶ bridge.py ──WebSocket──▶ hrv_daemon.py ──▶ Hue lights
                        │                        │              timeBuzzer LED
                        │                        │              SQLite database
                        │                        │
                        ▼                        ▼
                   overlay.html            session_report.html
                   (live dashboard)        overnight_report.html
```

## Setup

```sh
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pip install python-rtmidi  # optional, for timeBuzzer
```

Grant Bluetooth permission to your terminal (System Settings → Privacy → Bluetooth).

## Run

```sh
# Terminal 1 — sensor bridge
python bridge.py

# Terminal 2 — daemon (tracks, stores, drives lights)
python hrv_daemon.py --lights 4,3,2 --mode monitor

# Terminal 3 — serve browser dashboard
python3 -m http.server 8080
# Open http://localhost:8080/overlay.html
```

## Daemon modes

| Mode | Reactivity | Use case |
|---|---|---|
| `--mode training` | Fast, wide brightness range | Active breath training |
| `--mode monitor` | Slow, subtle | Background during work |
| `--mode focus` | Minimal | Deep work, alerts only |

## Hue light gradient

With `--lights 4,3,2` (nearest to furthest), the lights form a spatial gradient:
- High HRV: all warm red
- Dropping HRV: blue creeps inward from the furthest light
- Low HRV: blue reaches the closest light

## timeBuzzer as controller

The timeBuzzer's rotation, touch, and press are context-aware:

| App | Rotate | Press | Double-tap |
|---|---|---|---|
| Browser | Exponential scroll | Mark moment | Dictation |
| Terminal | History cycle | Mark moment | Dictation |
| VS Code | Font size | Zen mode | Dictation |
| Spotify | Volume | Play/pause | Dictation |
| Zoom | Volume | Mute toggle | Dictation |

LED segments: heartbeat pulse (red→blue based on HRV).

## Alert system

| Alert | Trigger | Effect |
|---|---|---|
| Decline | RMSSD dropping for 45s | Triple purple pulse on buzzer |
| Spike | HR jumps 20+ above baseline | Triple orange pulse |
| Recovery | HRV recovers after stress | Triple green pulse |
| Stillness | No movement for 30 min | Orange flash |

## Internal memory (mobile mode)

```sh
python polar_memory.py start --id "morning_walk"  # start recording, disconnect
python polar_memory.py download                     # come back, download to SQLite
```

## Simulator (testing without the strap)

```sh
python simulator.py --stress-cycle 60
```

Generates realistic HR/RR/ACC data cycling through calm → focus → stress → recovery.

## Breathing patterns

Available in the overlay and morning session:

- **Coherence** — 5.5s in / 5.5s out (HRV-optimal)
- **Box 4** — 4-4-4-4
- **Box 6** — 6-6-6-6
- **4-7-8** — inhale 4, hold 7, exhale 8
- **Resonance** — 5s in / 5s out (6 breaths/min)

## Morning session

Automated daily protocol at 09:00 (via launchd):

1. Wake notification
2. 2 min resting baseline
3. Resonance frequency test (4.5–6.5 bpm)
4. Results comparison with previous sessions

```sh
python morning_session.py  # run manually
```

## Files

| File | Purpose |
|---|---|
| `bridge.py` | Polar H10 BLE → WebSocket bridge |
| `hrv_daemon.py` | Main daemon — HRV, lights, buzzer, SQLite |
| `overlay.html` | Multi-panel live dashboard |
| `simulator.py` | Fake sensor for testing |
| `polar_memory.py` | Internal memory start/stop/download |
| `morning_session.py` | Guided morning HRV protocol |
| `buzzer_probe.py` | timeBuzzer MIDI diagnostic |
| `session_report.html` | Generated session report |
| `overnight_report.html` | Generated overnight report |
| `hrv_lights.py` | Earlier standalone light driver (superseded by daemon) |
| `index.html` | Original three.js ribbon visualization |
| `pacer.html` | Standalone breathing pacer |
| `coherence.html` | Coherence visualization |
| `wave-match.html` | Wave matching visualization |

## Data

All stored in `hrv_data.db` (SQLite):

- `rr_intervals` — every heartbeat (ms)
- `hrv_samples` — RMSSD, SDNN, trend every 5s
- `stress_events` — detected stress episodes
- `movement_samples` — stillness + breath rate every 10s
- `markers` — manual timestamps (buzzer press)
- `sessions` — start/end times

## Hardware

- [Polar H10](https://www.polar.com/en/sensors/h10-heart-rate-sensor) chest strap
- [Philips Hue](https://www.philips-hue.com/) lights + bridge
- [timeBuzzer](https://www.timebuzzer.com/) USB-C device (optional)

## Legal

See [LEGAL.md](LEGAL.md) for details on hardware interoperability, protocol documentation methods, and disclaimers. All hardware communication uses standard interfaces (USB-MIDI, BLE GATT, HTTP). No proprietary code is included or redistributed.
