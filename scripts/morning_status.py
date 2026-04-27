#!/usr/bin/env python3
"""Outputs morning HRV status for Hermes cron injection."""
import sqlite3, math, os
from datetime import datetime

DB = os.path.expanduser("~/ai_projects/polar-h10-ribbon/hrv_data.db")
db = sqlite3.connect(DB)

# Current state
current = db.execute("SELECT rmssd, hr_mean, datetime(ts,'unixepoch','localtime') FROM hrv_samples ORDER BY ts DESC LIMIT 1").fetchone()

# Morning window (6-10am today)
today = datetime.now().strftime("%Y-%m-%d")
morning = db.execute("""
    SELECT avg(rmssd), avg(hr_mean), count(*) FROM hrv_samples
    WHERE date(ts,'unixepoch','localtime') = ?
      AND cast(strftime('%H',datetime(ts,'unixepoch','localtime')) as int) BETWEEN 6 AND 10
      AND rmssd BETWEEN 1 AND 150
""", (today,)).fetchone()

# Yesterday comparison
yesterday_morning = db.execute("""
    SELECT avg(rmssd), avg(hr_mean) FROM hrv_samples
    WHERE date(ts,'unixepoch','localtime') = date('now','-1 day')
      AND cast(strftime('%H',datetime(ts,'unixepoch','localtime')) as int) BETWEEN 6 AND 10
      AND rmssd BETWEEN 1 AND 150
""").fetchone()

# Last night sleep
overnight = db.execute("""
    SELECT avg(rmssd), avg(hr_mean), min(hr_mean) FROM hrv_samples
    WHERE ts > unixepoch() - 28800
      AND cast(strftime('%H',datetime(ts,'unixepoch','localtime')) as int) BETWEEN 0 AND 6
      AND rmssd BETWEEN 1 AND 150
""").fetchone()

# Experiments yesterday
exps = db.execute("""
    SELECT name, delta_pct FROM experiments
    WHERE date = date('now','-1 day')
""").fetchall()

# DFA
dfa = db.execute("SELECT dfa_alpha1 FROM hrv_advanced WHERE dfa_alpha1 > 0 ORDER BY ts DESC LIMIT 1").fetchone()

# Cannabis yesterday
cannabis = db.execute("SELECT cannabis FROM daily_log WHERE date = date('now','-1 day')").fetchone()

print("=== MORNING HRV STATUS ===")
if current and current[0]:
    data_age = db.execute("SELECT unixepoch()-max(ts) FROM hrv_samples").fetchone()[0]
    if data_age < 60:
        print(f"Live: RMSSD={current[0]:.1f}ms HR={current[1]:.0f} ({current[2]})")
    else:
        print(f"Last data: {current[2]} ({data_age//60}min ago) — strap may be off")

if morning and morning[2] > 0:
    print(f"Morning avg: RMSSD={morning[0]:.1f}ms HR={morning[1]:.0f} ({morning[2]} samples)")
else:
    print("No morning data yet — put on the strap")

if yesterday_morning and yesterday_morning[0]:
    delta = ((morning[0] / yesterday_morning[0] - 1) * 100) if morning and morning[0] else 0
    print(f"Yesterday morning: RMSSD={yesterday_morning[0]:.1f}ms ({delta:+.0f}% change)")

if overnight and overnight[0]:
    print(f"Overnight: avg RMSSD={overnight[0]:.1f}ms, lowest HR={overnight[2]:.0f}")

if dfa and dfa[0]:
    status = "healthy" if 0.75 <= dfa[0] <= 1.25 else "watch"
    print(f"DFA α1: {dfa[0]:.2f} ({status})")

if cannabis and cannabis[0]:
    print("Note: cannabis yesterday")

if exps:
    print(f"Yesterday's experiments: {', '.join(f'{e[0]} ({e[1]:+.0f}%)' for e in exps)}")

print("\n=== RECOMMENDED ===")
if morning and morning[0] and morning[0] < 20:
    print("RMSSD low — prioritize breathing session and gentle walk")
elif morning and morning[0] and morning[0] > 30:
    print("RMSSD good — can push harder today (exercise, HIIT)")
else:
    print("Put on strap, do 2min baseline, then 5min coherence breathing")
