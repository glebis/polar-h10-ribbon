#!/usr/bin/env python3
"""Outputs evening HRV summary for daily log prompt."""
import sqlite3, os
from datetime import datetime

DB = os.path.expanduser("~/ai_projects/polar-h10-ribbon/hrv_data.db")
db = sqlite3.connect(DB)
today = datetime.now().strftime("%Y-%m-%d")

# Day summary
day = db.execute("""
    SELECT avg(rmssd), avg(hr_mean), min(rmssd), max(rmssd), count(*)
    FROM hrv_samples WHERE date(ts,'unixepoch','localtime') = ? AND rmssd BETWEEN 1 AND 150
""", (today,)).fetchone()

# Experiments today
exps = db.execute("SELECT name, delta_pct FROM experiments WHERE date = ?", (today,)).fetchall()

# Stress events
stress = db.execute("""
    SELECT count(*), coalesce(sum(duration_s),0) FROM stress_events
    WHERE date(ts,'unixepoch','localtime') = ?
""", (today,)).fetchone()

# Check if log already exists
logged = db.execute("SELECT id FROM daily_log WHERE date = ?", (today,)).fetchone()

print("=== EVENING SUMMARY ===")
if day and day[4] > 0:
    print(f"Today: avg RMSSD={day[0]:.1f}ms, HR={day[1]:.0f}, range {day[2]:.0f}-{day[3]:.0f}ms, {day[4]} samples")
else:
    print("No data today")

if exps:
    print(f"Experiments: {', '.join(f'{e[0]} ({e[1]:+.0f}%)' for e in exps)}")
else:
    print("No experiments today")

if stress:
    print(f"Stress events: {stress[0]} ({stress[1]/60:.0f} min total)")

if logged:
    print("Daily log: already filled")
else:
    print("Daily log: NOT FILLED — please run: python experiment.py log")
