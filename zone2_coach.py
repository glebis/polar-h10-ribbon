"""Zone 2 voice coach — real-time HR zone + DFA α1 feedback via Polar H10.

Usage:
    python zone2_coach.py [--age 38] [--resting-hr 65]

Connects to bridge.py WebSocket, computes DFA α1 from RR intervals,
announces zone transitions and gives periodic voice updates.

Zone 2 is where DFA α1 ≈ 0.75 — the aerobic threshold.
"""
import argparse
import asyncio
import json
import math
import subprocess
import time
from collections import deque
from pathlib import Path

try:
    import websockets
except ImportError:
    import sys
    sys.exit("websockets required: pip install websockets")

WS_URL = "ws://localhost:8765"
COACH_LOG = Path(__file__).parent / "coach_log.jsonl"


# voice queue — only one utterance at a time, skip if busy
_say_proc = None

def say(text, rate=185):
    global _say_proc
    # if previous say is still talking, skip this one
    if _say_proc is not None and _say_proc.poll() is None:
        return
    _say_proc = subprocess.Popen(["say", "-v", "Samantha", "-r", str(rate), text],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def compute_dfa_alpha1(rr_list):
    """DFA α1 from RR intervals (ms). Needs ≥200 beats."""
    n = len(rr_list)
    if n < 200:
        return None

    mean_rr = sum(rr_list) / n
    integrated = []
    cumsum = 0
    for v in rr_list:
        cumsum += (v - mean_rr)
        integrated.append(cumsum)

    scales = [s for s in [4, 6, 8, 12, 16, 24, 32, 48, 64] if s <= n // 4]
    if len(scales) < 4:
        return None

    log_n, log_f = [], []
    for s in scales:
        num_segments = n // s
        fluctuations = []
        for seg in range(num_segments):
            start = seg * s
            segment = integrated[start:start + s]
            xs = list(range(s))
            mx = (s - 1) / 2
            my = sum(segment) / s
            num = sum((x - mx) * (y - my) for x, y in zip(xs, segment))
            den = sum((x - mx) ** 2 for x in xs)
            slope = num / den if den > 0 else 0
            intercept = my - slope * mx
            resid = [(segment[i] - (slope * i + intercept)) ** 2 for i in range(s)]
            fluctuations.append(math.sqrt(sum(resid) / s))
        if fluctuations:
            mean_f = sum(fluctuations) / len(fluctuations)
            if mean_f > 0:
                log_n.append(math.log(s))
                log_f.append(math.log(mean_f))

    if len(log_n) < 3:
        return None
    nl = len(log_n)
    mx = sum(log_n) / nl
    my = sum(log_f) / nl
    num = sum((x - mx) * (y - my) for x, y in zip(log_n, log_f))
    den = sum((x - mx) ** 2 for x in log_n)
    return num / den if den > 0 else None


def get_zones(age, resting_hr):
    """Karvonen HR zones + MAF number."""
    max_hr = 220 - age
    reserve = max_hr - resting_hr

    zones = {
        1: (resting_hr + reserve * 0.50, resting_hr + reserve * 0.60),
        2: (resting_hr + reserve * 0.60, resting_hr + reserve * 0.70),
        3: (resting_hr + reserve * 0.70, resting_hr + reserve * 0.80),
        4: (resting_hr + reserve * 0.80, resting_hr + reserve * 0.90),
        5: (resting_hr + reserve * 0.90, max_hr),
    }
    maf = 180 - age
    return zones, max_hr, maf


def hr_to_zone(hr, zones):
    for z in range(5, 0, -1):
        if hr >= zones[z][0]:
            return z
    return 0


async def run(args):
    zones, max_hr, maf = get_zones(args.age, args.resting_hr)

    print(f"Zone 2 coach · age {args.age} · resting HR {args.resting_hr}")
    print(f"Max HR: {max_hr} · MAF: {maf}")
    print(f"Zones (Karvonen):")
    for z, (lo, hi) in zones.items():
        marker = " ◀ TARGET" if z == 2 else ""
        print(f"  Zone {z}: {lo:.0f}–{hi:.0f} bpm{marker}")
    print(f"\nDFA α1 target: 0.75 (aerobic threshold)")
    print(f"Connecting to {WS_URL}…\n")

    # Workout structure
    warmup_min = args.warmup
    main_min = args.duration
    cooldown_min = args.cooldown
    total_s = (warmup_min + main_min + cooldown_min) * 60

    say(f"Kettlebell workout. {warmup_min} minutes warm up, {main_min} minutes zone 2, {cooldown_min} minutes cool down. Target heart rate {zones[2][0]:.0f} to {zones[2][1]:.0f}. Start with arm circles and hip hinges to warm up.")

    rr_buf = deque(maxlen=500)
    current_hr = 0
    current_zone = 0
    last_announce = 0
    last_dfa = 0
    last_dfa_value = None
    announce_interval = 20  # frequent for kettlebell work
    dfa_interval = 20
    session_start = time.time()
    zone_time = {0: 0, 1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
    last_tick = time.time()
    last_phase = None
    last_milestone = 0
    phase_transitions_announced = set()
    hr_history = deque(maxlen=30)  # last 30 HR readings for trend

    while True:
        try:
            async with websockets.connect(WS_URL) as ws:
                print("connected to polar bridge")

                async for raw in ws:
                    msg = json.loads(raw)
                    if msg.get("type") != "hr":
                        continue

                    current_hr = msg["bpm"]
                    hr_history.append(current_hr)
                    for rr in msg.get("rr", []):
                        if 250 < rr < 1800:
                            rr_buf.append(rr)

                    # HR trend: compare last 5 vs previous 5
                    hr_trend = ""
                    if len(hr_history) >= 10:
                        recent = sum(list(hr_history)[-5:]) / 5
                        older = sum(list(hr_history)[-10:-5]) / 5
                        diff = recent - older
                        if diff > 5:
                            hr_trend = "rising"
                        elif diff < -5:
                            hr_trend = "dropping"

                    now = time.time()
                    dt = now - last_tick
                    last_tick = now

                    new_zone = hr_to_zone(current_hr, zones)

                    # track time in zones
                    zone_time[current_zone] += dt

                    # zone transition — only announce if stable for 3+ seconds
                    if new_zone != current_zone:
                        if not hasattr(run, '_zone_change_time') or run._zone_pending != new_zone:
                            run._zone_change_time = now
                            run._zone_pending = new_zone
                        elif now - run._zone_change_time >= 3:
                            current_zone = new_zone
                            run._zone_pending = -1
                            if phase == "zone2":
                                if new_zone == 2:
                                    say(f"{current_hr}. In the zone. Keep going.")
                                elif new_zone < 2:
                                    say(f"{current_hr}. Pick up the bell. Next set.")
                                elif new_zone == 3:
                                    say(f"{current_hr}. Rest. Put the bell down.")
                                elif new_zone >= 4:
                                    say(f"{current_hr}. Rest now. Breathe.")

                    # periodic DFA α1 computation
                    if now - last_dfa >= dfa_interval and len(rr_buf) >= 200:
                        last_dfa = now
                        alpha = compute_dfa_alpha1(list(rr_buf))
                        if alpha is not None:
                            last_dfa_value = alpha

                    # workout phase tracking
                    elapsed = now - session_start
                    elapsed_min = elapsed / 60
                    warmup_end = warmup_min * 60
                    main_end = (warmup_min + main_min) * 60
                    total_end = total_s

                    if elapsed < warmup_end:
                        phase = "warmup"
                        phase_remaining = warmup_end - elapsed
                    elif elapsed < main_end:
                        phase = "zone2"
                        phase_remaining = main_end - elapsed
                    elif elapsed < total_end:
                        phase = "cooldown"
                        phase_remaining = total_end - elapsed
                    else:
                        phase = "done"
                        phase_remaining = 0

                    remaining_min = int(phase_remaining // 60)
                    remaining_sec = int(phase_remaining % 60)

                    # phase transition announcements
                    if phase != last_phase:
                        last_phase = phase
                        if phase == "zone2" and "zone2" not in phase_transitions_announced:
                            phase_transitions_announced.add("zone2")
                            say(f"Warm up complete. Main set: {main_min} minutes in zone 2. Push to {zones[2][0]:.0f} beats per minute.")
                        elif phase == "cooldown" and "cooldown" not in phase_transitions_announced:
                            phase_transitions_announced.add("cooldown")
                            z2_pct = zone_time[2] / max(1, sum(zone_time.values())) * 100
                            say(f"Main set complete. {z2_pct:.0f} percent in zone 2. Cool down for {cooldown_min} minutes. Slow it down.")
                        elif phase == "done" and "done" not in phase_transitions_announced:
                            phase_transitions_announced.add("done")
                            say("Workout complete. Great job. Stopping.")

                    if phase == "done":
                        break

                    # countdown at specific remaining times
                    for countdown in [10, 5, 3, 2, 1]:
                        mark = countdown * 60
                        if phase_remaining <= mark and phase_remaining > mark - 2 and mark not in phase_transitions_announced:
                            phase_transitions_announced.add(mark)
                            phase_name = {"warmup": "warm up", "zone2": "zone 2", "cooldown": "cool down"}[phase]
                            say(f"{countdown} minutes left in {phase_name}.")

                    # 30 second warning
                    if phase_remaining <= 30 and phase_remaining > 28 and f"{phase}_30s" not in phase_transitions_announced:
                        phase_transitions_announced.add(f"{phase}_30s")
                        phase_name = {"warmup": "warm up", "zone2": "zone 2", "cooldown": "cool down"}[phase]
                        say(f"30 seconds left in {phase_name}.")

                    # periodic voice update
                    if now - last_announce >= announce_interval:
                        last_announce = now

                        parts = []

                        if phase == "warmup":
                            warmup_cues = [
                                "Arm circles, loosen up the shoulders.",
                                "Hip hinges. Hinge at the hips, flat back, feel the hamstrings.",
                                "Halos. Swing the bell around your head, both directions.",
                                "Goblet squats. Slow and deep. Open the hips.",
                                "Light deadlifts. Practice the hip hinge with the bell.",
                                "A few easy swings to find your groove.",
                            ]
                            cue_idx = int(elapsed / max(1, warmup_min * 60) * len(warmup_cues)) % len(warmup_cues)
                            parts.append(f"{current_hr}. {warmup_cues[cue_idx]} {remaining_min} minutes.")
                        elif phase == "zone2":
                            # exercise suggestions rotate
                            exercises = [
                                "Two-hand swings.",
                                "Single-arm swings, switch halfway.",
                                "Goblet squats.",
                                "Clean and press, alternate arms.",
                                "Swings.",
                                "Turkish get-up, one each side.",
                                "Snatches if you're comfortable.",
                                "Farmer carries around the room.",
                            ]
                            ex_idx = int(elapsed / 40) % len(exercises)

                            if current_hr > zones[3][1]:
                                parts.append(f"{current_hr}. Put the bell down. Breathe. Walk it off.")
                                if hr_trend == "rising":
                                    parts.append("Still climbing. Keep resting.")
                            elif current_hr > zones[2][1]:
                                parts.append(f"{current_hr}. Rest. Shake out your arms.")
                                if hr_trend == "dropping":
                                    parts.append("Coming down. Almost ready.")
                            elif current_hr >= zones[2][0]:
                                parts.append(f"{current_hr}. Perfect zone. {exercises[ex_idx]}")
                                if hr_trend == "rising":
                                    parts.append("Trending up. Lighter reps or slow down.")
                                elif hr_trend == "dropping":
                                    parts.append("Trending down. Pick up the pace.")
                            elif current_hr >= zones[1][0]:
                                parts.append(f"{current_hr}. Recovered. {exercises[ex_idx]} Go.")
                            else:
                                parts.append(f"{current_hr}. Low. Start your next set. {exercises[ex_idx]}")
                            parts.append(f"{remaining_min} minutes left.")
                        elif phase == "cooldown":
                            parts.append(f"{current_hr}. Cool down. {remaining_min} minutes.")

                        if last_dfa_value is not None and phase == "zone2":
                            a = last_dfa_value
                            if 0.70 <= a <= 0.80:
                                parts.append("Aerobic zone perfect.")

                        say(" ".join(parts))

                    # log every reading
                    with open(COACH_LOG, "a") as lf:
                        lf.write(json.dumps({
                            "ts": now, "hr": current_hr, "zone": current_zone,
                            "phase": phase, "dfa": round(last_dfa_value, 3) if last_dfa_value else None,
                            "rr_count": len(rr_buf), "trend": hr_trend,
                            "elapsed": round(elapsed, 1),
                        }) + "\n")

                    # console output
                    dfa_str = f"α1={last_dfa_value:.2f}" if last_dfa_value else "α1=…"
                    el = int(elapsed)
                    z2_pct = zone_time[2] / max(1, sum(zone_time.values())) * 100
                    phase_short = {"warmup": "WARM", "zone2": "Z2", "cooldown": "COOL", "done": "DONE"}.get(phase, "?")
                    print(
                        f"\r  {phase_short} HR {current_hr:>3}  Zone {current_zone}  "
                        f"{dfa_str}  "
                        f"Z2={z2_pct:.0f}%  "
                        f"{el//60}:{el%60:02d}  "
                        f"-{remaining_min}:{remaining_sec:02d}  "
                        f"RR={len(rr_buf)}",
                        end="   \x1b[K", flush=True
                    )

        except (ConnectionRefusedError, OSError):
            print("\rbridge not running, retrying…", end="", flush=True)
            await asyncio.sleep(3)
        except websockets.ConnectionClosed:
            print("\ndisconnected, reconnecting…")
            await asyncio.sleep(1)
        except KeyboardInterrupt:
            break

    # session summary — from the log file for accurate per-second data
    elapsed = int(time.time() - session_start)
    total = max(1, sum(zone_time.values()))

    # read back the log for this session
    session_hrs = []
    session_z2_readings = 0
    peak_hr = 0
    try:
        for line in open(COACH_LOG):
            row = json.loads(line)
            if row.get("elapsed", 0) >= 0:
                session_hrs.append(row["hr"])
                if row.get("zone") == 2:
                    session_z2_readings += 1
                peak_hr = max(peak_hr, row["hr"])
    except Exception:
        pass

    z2_pct = zone_time[2] / total * 100
    above_z2 = (zone_time.get(3, 0) + zone_time.get(4, 0) + zone_time.get(5, 0)) / total * 100

    print(f"\n\n{'='*50}")
    print(f"SESSION SUMMARY: {elapsed//60}:{elapsed%60:02d}")
    print(f"{'='*50}")
    print(f"  Readings: {len(session_hrs)}")
    print(f"  HR: {min(session_hrs) if session_hrs else 0}–{peak_hr} bpm (avg {sum(session_hrs)/max(1,len(session_hrs)):.0f})")
    for z in range(5):
        pct = zone_time[z] / total * 100
        bar = "█" * int(pct / 2)
        label = ["rest", "warmup", "ZONE 2", "tempo", "threshold"][z]
        print(f"  Zone {z} ({label:>8}): {pct:5.1f}%  {bar}")
    print(f"  Peak HR: {peak_hr}")
    if last_dfa_value:
        print(f"  DFA α1: {last_dfa_value:.3f}")
    print(f"{'='*50}")

    # save session summary to JSON
    summary = {
        "ts": time.time(),
        "duration_s": elapsed,
        "avg_hr": round(sum(session_hrs) / max(1, len(session_hrs))),
        "peak_hr": peak_hr,
        "min_hr": min(session_hrs) if session_hrs else 0,
        "zone_pct": {str(z): round(zone_time[z] / total * 100, 1) for z in range(5)},
        "z2_pct": round(z2_pct, 1),
        "dfa_alpha1": round(last_dfa_value, 3) if last_dfa_value else None,
        "readings": len(session_hrs),
        "exercise": "kettlebell",
        "age": args.age,
    }
    summary_file = Path(__file__).parent / "coach_sessions.jsonl"
    with open(summary_file, "a") as f:
        f.write(json.dumps(summary) + "\n")

    # spoken summary
    parts = [f"Workout complete. {elapsed//60} minutes."]
    parts.append(f"Peak heart rate {peak_hr}.")
    parts.append(f"{z2_pct:.0f} percent in zone 2.")
    if z2_pct < 20:
        parts.append("Try longer sets with shorter rest to increase zone 2 time next session.")
    elif z2_pct < 40:
        parts.append("Good start. Push for 40 percent zone 2 next time.")
    else:
        parts.append("Solid zone 2 session. Great work.")
    if last_dfa_value:
        if last_dfa_value < 1.0:
            parts.append(f"DFA alpha {last_dfa_value:.2f}. Good aerobic recovery.")
        else:
            parts.append(f"DFA alpha {last_dfa_value:.2f}. Recovery still sympathetic.")
    say(" ".join(parts))


def main():
    parser = argparse.ArgumentParser(description="Zone 2 voice coach")
    parser.add_argument("--age", type=int, required=True, help="Your age")
    parser.add_argument("--resting-hr", type=int, default=65,
                        help="Resting heart rate (default: 65)")
    parser.add_argument("--warmup", type=int, default=5, help="Warm-up minutes (default: 5)")
    parser.add_argument("--duration", type=int, default=30, help="Zone 2 main set minutes (default: 30)")
    parser.add_argument("--cooldown", type=int, default=5, help="Cool-down minutes (default: 5)")
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
