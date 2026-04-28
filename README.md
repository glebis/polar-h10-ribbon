# polar-h10-ribbon

Real-time HRV biofeedback system. Connects a Polar H10 chest strap to ambient lighting, sound, and browser visualizations — turning your nervous system state into something you can see, hear, and train.

## What it does

- **Streams** ECG (130 Hz), heart rate, RR intervals, and accelerometer (50 Hz) from a Polar H10 via BLE
- **Computes** RMSSD, SDNN, HRV trends, coherence, breathing rate, and stillness in real time
- **Drives ambient lighting** — 3 Philips Hue lights form a gradient that ripples with your heartbeat, color shifts with HRV state
- **timeBuzzer LED** — physical desk controller pulses with your HRV, minimum brightness floor so it's always visible
- **Harmonic audio engine** — heartbeat-synced chord progressions that shift from major (calm) to diminished (stressed), with physics-based particle collisions producing tuned notes
- **Breathing guide** — sine-eased coherence patterns overlaid on your real breath trace
- **Protocol tracking** — long-term dashboard with daily baselines, weekly/monthly trends, context tagging, and "is it working?" trend detection
- **Internal memory** — start recording on the H10, disconnect, download later

## Quick start

```sh
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

Grant Bluetooth permission to your terminal (System Settings → Privacy → Bluetooth).

## Run

Four services, each in its own terminal:

```sh
# 1. Polar H10 BLE → WebSocket bridge
python bridge.py

# 2. Serve web pages
python3 -m http.server 8080

# 3. HRV → lights daemon (Hue + timeBuzzer)
python hrv_lights.py --preset candle

# 4. Protocol API (for long-term tracking dashboard)
python protocol_api.py
```

Then open http://localhost:8080

## Pages

| Page | URL | Purpose |
|---|---|---|
| **ribbon** | `/index.html` | Three.js ECG ribbon — raw waveform drives 3D geometry |
| **pacer** | `/pacer.html` | Breathing pacer with multiple patterns |
| **coherence** | `/coherence.html` | Coherence score + breathing guide overlaid on breath chart. Space to pause, ↑↓ to switch patterns |
| **heartbeat** | `/heartbeat.html` | Particle physics + harmonic audio. Press A to cycle sound layers, S for visual style. Ring mode contains particles inside a sphere |
| **hrv-lights** | `/hrv-dashboard.html` | Live RMSSD trend chart, preset switcher, movement bars |
| **protocol** | `/protocol.html` | Long-term tracking: daily baseline, weekly/monthly trends, session log, context tags |

## Light presets

Switch via the dashboard preset buttons or `--preset NAME`:

| Preset | Calm (high HRV) | Stressed (low HRV) |
|---|---|---|
| **sleep** | dim red, barely visible | slightly brighter red |
| **candle** | deep red glow | amber/yellow, faster pulse |
| **sunset** | gold | rose-pink |
| **ocean** | teal | deep blue |
| **traffic** | green | red (unmistakable) |

Lights are controlled individually (not as a group) to create a gradient — each light gets a phase offset in the breathing pulse and a drifting hue shift.

## Audio engine (heartbeat.html)

Press **A** to cycle layers:

1. **bass** — sub-bass sine thump synced to heartbeat, louder when HRV is high
2. **bass+glass** — adds noise-burst shatter with resonant ringing, more shards when stressed
3. **bass+glass+chords** — full chord pad from the harmonic engine, strummed on each beat

HRV drives the chord progression:
- **Calm** (RMSSD > 23ms): I → V → vi → IV in major — warm, resolved
- **Mid** (10–23ms): sus4 → sus2 — floating, open
- **Stressed** (<10ms): m7 → half-dim → dim — dark, tense

Particle collisions produce notes from the current chord — small particles play high octaves, large particles play low.

## Protocol tracking

The protocol dashboard (`protocol.html` + `protocol_api.py`) tracks long-term biofeedback effectiveness:

- **Morning baseline** — 5 min resting RMSSD before coffee/cannabis, the primary outcome metric
- **Practice sessions** — coherence breathing, any time of day
- **Overnight** — passive sleep HRV recording
- **Context tags** — substance use, exercise, sleep quality, mood
- **Trend signal** — linear regression over 30 days, tells you if your protocol is working

## Internal memory

```sh
python polar_memory.py list                         # see stored exercises on H10
python polar_memory.py start --id "morning_walk"    # start internal recording
python polar_memory.py download                     # download all to SQLite
python polar_memory.py delete <exercise_id>         # free space on device
```

## Data

Primary store: `hrv_data.db` (SQLite)

| Table | Content |
|---|---|
| `sessions` | Recording sessions with start/end times |
| `rr_intervals` | Every heartbeat — timestamp, RR interval (ms), HR |
| `hrv_samples` | RMSSD, SDNN, HR mean, baseline, trend — every 5s |
| `stress_events` | Detected stress episodes with severity and duration |
| `movement_samples` | Stillness and breath rate |
| `daily_baselines` | Morning baseline measurements |

Live sessions also log to `hrv_log.jsonl` (append-only, every 5s).
Context tags stored in `protocol_log.json`.

## Files

| File | Purpose |
|---|---|
| `bridge.py` | Polar H10 BLE → WebSocket bridge (port 8765) |
| `hrv_lights.py` | HRV → lights daemon — computes RMSSD, drives Hue gradient + timeBuzzer, serves dashboard WebSocket (port 8082) |
| `protocol_api.py` | Protocol tracking JSON API (port 8090) — aggregates SQLite data for the protocol dashboard |
| `polar_memory.py` | H10 internal memory: start/stop recording, list/download/delete exercises via PSFTP protocol |
| `common.js` | Shared: WebSocket bridge, R-peak detection, breathing exercises, breath extraction from accelerometer |
| `index.html` | Three.js ECG ribbon visualization |
| `pacer.html` | Breathing pacer |
| `coherence.html` | Coherence score + guided breathing |
| `heartbeat.html` | Particle physics + harmonic audio biofeedback |
| `hrv-dashboard.html` | Live HRV dashboard with trend chart and light preset controls |
| `protocol.html` | Long-term protocol tracking dashboard |

## Hardware

- [Polar H10](https://www.polar.com/en/sensors/h10-heart-rate-sensor) chest strap
- [Philips Hue](https://www.philips-hue.com/) lights + bridge (lights 2, 3, 4 by default)
- [timeBuzzer](https://www.timebuzzer.com/) USB-C device (optional — `pip install python-rtmidi`)

## Dependencies

```
bleak           # BLE communication with Polar H10
websockets      # real-time data streaming
python-rtmidi   # timeBuzzer MIDI control (optional)
```

All other code uses Python stdlib and vanilla browser JS — no frameworks, no build step.
