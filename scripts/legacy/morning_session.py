"""Morning HRV session — resonance frequency test with voice guidance.
Triggered by launchd or run manually:
    python morning_session.py
"""
import os
import subprocess
import sqlite3
import time
import math

DB = os.path.expanduser("~/ai_projects/polar-h10-ribbon/hrv_data.db")

def say(text, rate=160):
    subprocess.run(["say", "-r", str(rate), text])

def notify(title, msg):
    subprocess.run(["osascript", "-e",
        f'display notification "{msg}" with title "{title}" sound name "Glass"'])

def check_bridge():
    try:
        import websockets, asyncio
        async def _check():
            async with websockets.connect("ws://localhost:8765") as ws:
                return True
        return asyncio.run(_check())
    except Exception:
        return False

def get_last_night_baseline():
    db = sqlite3.connect(DB)
    row = db.execute("""
        SELECT avg(rmssd), avg(hr_mean) FROM hrv_samples
        WHERE ts > unixepoch() - 86400 AND ts < unixepoch() - 28800
    """).fetchone()
    db.close()
    return row if row[0] else (None, None)

def run_rf_test():
    """Test resonance frequency at 5 rates, 2 min each."""
    import asyncio
    import websockets
    import json

    rates = [6.5, 6.0, 5.5, 5.0, 4.5]  # breaths per minute
    results = {}

    async def collect_at_rate(bpm, duration_s=120):
        rr_buf = []
        async with websockets.connect("ws://localhost:8765") as ws:
            start = time.time()
            cycle_s = 60.0 / bpm
            inhale_s = cycle_s / 2
            exhale_s = cycle_s / 2

            say(f"Breathing at {bpm} breaths per minute. {inhale_s:.1f} seconds in, {exhale_s:.1f} seconds out.")
            time.sleep(1)

            breath_start = time.time()
            phase = "in"
            next_switch = breath_start + inhale_s

            while time.time() - start < duration_s:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=0.3)
                    msg = json.loads(raw)
                    if msg.get("type") == "hr":
                        for rr in msg.get("rr", []):
                            if 200 < rr < 2000:
                                rr_buf.append(rr)
                except asyncio.TimeoutError:
                    pass

                now = time.time()
                if now >= next_switch:
                    if phase == "in":
                        phase = "out"
                        say("out", rate=130)
                        next_switch = now + exhale_s
                    else:
                        phase = "in"
                        say("in", rate=130)
                        next_switch = now + inhale_s

        # Compute RMSSD
        if len(rr_buf) >= 10:
            diffs_sq = [(rr_buf[i+1]-rr_buf[i])**2 for i in range(len(rr_buf)-1)]
            rmssd = math.sqrt(sum(diffs_sq) / len(diffs_sq))
            mean_rr = sum(rr_buf) / len(rr_buf)
            hr = 60000 / mean_rr
            # HR oscillation amplitude (proxy for resonance)
            hrs = [60000/rr for rr in rr_buf if rr > 0]
            if len(hrs) > 10:
                hr_range = max(hrs) - min(hrs)
            else:
                hr_range = 0
            return {"rmssd": rmssd, "hr": hr, "hr_range": hr_range, "beats": len(rr_buf)}
        return None

    say("Starting resonance frequency test. Five breathing rates, two minutes each. Sit comfortably.")
    time.sleep(2)

    for bpm in rates:
        say(f"Next rate: {bpm} breaths per minute. Get ready.")
        time.sleep(3)
        result = asyncio.run(collect_at_rate(bpm))
        if result:
            results[bpm] = result
            print(f"  {bpm} bpm: RMSSD={result['rmssd']:.1f}  HR={result['hr']:.0f}  HR_range={result['hr_range']:.0f}")
        say("Rest for ten seconds.")
        time.sleep(10)

    return results

def main():
    # Step 1: Notification
    notify("HRV Morning Session", "Put on Polar H10, sit down, no coffee yet")
    say("Good morning. Time for your HRV session. Put on the Polar H10 chest strap and sit down. No coffee yet.")

    # Step 2: Wait for bridge
    say("I'll check for the sensor in two minutes.")
    time.sleep(120)

    for attempt in range(6):
        if check_bridge():
            say("Sensor connected. Starting in thirty seconds. Sit still and relax.")
            time.sleep(30)
            break
        say("Sensor not found. Start bridge dot py. Checking again in one minute.")
        time.sleep(60)
    else:
        say("Could not connect to sensor. Skipping session.")
        return

    # Step 3: Resting baseline (2 min quiet)
    say("First, two minutes of quiet rest. Just breathe normally.")
    import asyncio, websockets, json
    rr_baseline = []
    async def collect_baseline():
        async with websockets.connect("ws://localhost:8765") as ws:
            start = time.time()
            while time.time() - start < 120:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=1)
                    msg = json.loads(raw)
                    if msg.get("type") == "hr":
                        for rr in msg.get("rr", []):
                            if 200 < rr < 2000:
                                rr_baseline.append(rr)
                except asyncio.TimeoutError:
                    pass
    asyncio.run(collect_baseline())

    if len(rr_baseline) >= 10:
        diffs = [(rr_baseline[i+1]-rr_baseline[i])**2 for i in range(len(rr_baseline)-1)]
        rest_rmssd = math.sqrt(sum(diffs)/len(diffs))
        rest_hr = 60000 / (sum(rr_baseline)/len(rr_baseline))
        print(f"\n  Resting: RMSSD={rest_rmssd:.1f}  HR={rest_hr:.0f}")
        say(f"Resting baseline: RMSSD {rest_rmssd:.0f} milliseconds, heart rate {rest_hr:.0f}.")
    else:
        rest_rmssd = 0
        rest_hr = 0

    # Step 4: RF test
    results = run_rf_test()

    # Step 5: Find best rate
    if results:
        best_rate = max(results, key=lambda r: results[r]["hr_range"])
        best = results[best_rate]
        say(f"Your resonance frequency appears to be {best_rate} breaths per minute. "
            f"Heart rate swing was {best['hr_range']:.0f} beats. "
            f"RMSSD was {best['rmssd']:.0f} milliseconds.")

        # Compare with last night
        last_rmssd, last_hr = get_last_night_baseline()
        if last_rmssd:
            change = ((rest_rmssd - last_rmssd) / last_rmssd * 100) if last_rmssd > 0 else 0
            say(f"Compared to last night: resting RMSSD was {last_rmssd:.0f}, "
                f"this morning {rest_rmssd:.0f}. That's {change:+.0f} percent.")

        # Log to DB
        db = sqlite3.connect(DB)
        session_id = db.execute(
            "INSERT INTO sessions (started_at, notes) VALUES (?, ?)",
            (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
             f"Morning RF test. Best rate: {best_rate} bpm. Resting RMSSD: {rest_rmssd:.1f}")
        ).lastrowid
        db.commit()
        db.close()

        print(f"\n  Best rate: {best_rate} bpm (HR range {best['hr_range']:.0f})")
        print(f"  Results: {results}")

    say("Session complete. Have a great morning.")

if __name__ == "__main__":
    main()
