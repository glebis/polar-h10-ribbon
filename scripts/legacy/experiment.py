"""Experiment runner — structured before/after measurements for HRV interventions.

Usage:
    python experiment.py run "coherence breathing 1min"
    python experiment.py run "cold face immersion" --baseline 120 --recovery 300
    python experiment.py list
    python experiment.py compare breathing exercise
    python experiment.py weekly
    python experiment.py log                           # daily log entry
"""
import argparse
import asyncio
import json
import math
import os
import sqlite3
import subprocess
import time
from datetime import datetime, timezone

DB = os.path.expanduser("~/ai_projects/polar-h10-ribbon/hrv_data.db")


def init_experiment_tables(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS experiments (
            id INTEGER PRIMARY KEY,
            date TEXT NOT NULL,
            ts REAL NOT NULL,
            name TEXT NOT NULL,
            hypothesis TEXT,
            baseline_rmssd REAL,
            baseline_hr REAL,
            baseline_dfa REAL,
            post_rmssd REAL,
            post_hr REAL,
            post_dfa REAL,
            delta_pct REAL,
            recovery_min REAL,
            subjective_before INTEGER,
            subjective_after INTEGER,
            notes TEXT
        );

        CREATE TABLE IF NOT EXISTS daily_log (
            id INTEGER PRIMARY KEY,
            date TEXT NOT NULL UNIQUE,
            morning_rmssd REAL,
            morning_hr REAL,
            evening_rmssd REAL,
            evening_hr REAL,
            experiment_names TEXT,
            cannabis INTEGER DEFAULT 0,
            coffee_time TEXT,
            sleep_time TEXT,
            wake_time TEXT,
            exercise_min REAL DEFAULT 0,
            breathing_min REAL DEFAULT 0,
            subjective_wellbeing INTEGER,
            notes TEXT
        );
    """)
    db.commit()


def get_current_hrv(db, window_s=60):
    row = db.execute("""
        SELECT avg(rmssd), avg(hr_mean) FROM hrv_samples
        WHERE ts > unixepoch() - ? AND rmssd BETWEEN 1 AND 150
    """, (window_s,)).fetchone()
    return (row[0] or 0, row[1] or 0)


def get_current_dfa(db):
    row = db.execute("""
        SELECT dfa_alpha1 FROM hrv_advanced
        WHERE dfa_alpha1 > 0 ORDER BY ts DESC LIMIT 1
    """).fetchone()
    return row[0] if row else None


def say(text, rate=140):
    subprocess.Popen(["say", "-r", str(rate), text],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def wait_with_countdown(seconds, label=""):
    for remaining in range(seconds, 0, -1):
        if remaining % 30 == 0 and remaining != seconds:
            say(f"{remaining} seconds")
        elif remaining == 10:
            say("ten seconds")
        time.sleep(1)


def cmd_run(args):
    db = sqlite3.connect(args.db)
    init_experiment_tables(db)

    name = args.name
    baseline_s = args.baseline
    recovery_s = args.recovery

    print(f"\n  Experiment: {name}")
    print(f"  Baseline: {baseline_s}s → Intervention → Recovery: {recovery_s}s")
    print(f"  {'─' * 50}")

    # Phase 1: Baseline
    say(f"Starting experiment: {name}. Sit still for baseline measurement.")
    print(f"\n  Collecting baseline ({baseline_s}s)...")
    time.sleep(baseline_s)
    baseline_rmssd, baseline_hr = get_current_hrv(db, baseline_s)
    baseline_dfa = get_current_dfa(db)
    print(f"  Baseline: RMSSD={baseline_rmssd:.1f}  HR={baseline_hr:.0f}  DFA={baseline_dfa or '—'}")

    # Subjective before
    say("Rate how you feel, one to five. One is terrible, five is great.")
    subj_before = None
    try:
        subj_before = int(input("  How do you feel? (1-5): ") or 0)
    except (ValueError, EOFError):
        pass

    # Phase 2: Intervention
    say(f"Begin: {name}. Go.")
    print(f"\n  >>> DO THE INTERVENTION NOW <<<")
    print(f"  Press Enter when done...")
    input()
    say("Done. Sit still for recovery measurement.")

    # Phase 3: Recovery
    print(f"  Collecting recovery ({recovery_s}s)...")
    wait_with_countdown(recovery_s)
    post_rmssd, post_hr = get_current_hrv(db, min(recovery_s, 120))
    post_dfa = get_current_dfa(db)

    # Subjective after
    say("Rate how you feel now, one to five.")
    subj_after = None
    try:
        subj_after = int(input("  How do you feel now? (1-5): ") or 0)
    except (ValueError, EOFError):
        pass

    # Calculate
    delta_pct = ((post_rmssd / baseline_rmssd - 1) * 100) if baseline_rmssd > 0 else 0

    # Find recovery time (when RMSSD returned to 90% of baseline)
    recovery_min = None
    if post_rmssd < baseline_rmssd * 0.9:
        recovery_min = recovery_s / 60  # didn't fully recover yet

    # Notes
    notes = input("  Notes (optional): ").strip() or None

    # Save
    now = time.time()
    db.execute("""
        INSERT INTO experiments (date, ts, name, baseline_rmssd, baseline_hr, baseline_dfa,
            post_rmssd, post_hr, post_dfa, delta_pct, recovery_min,
            subjective_before, subjective_after, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (datetime.now().strftime("%Y-%m-%d"), now, name,
          baseline_rmssd, baseline_hr, baseline_dfa,
          post_rmssd, post_hr, post_dfa, delta_pct, recovery_min,
          subj_before, subj_after, notes))
    db.commit()

    # Report
    print(f"\n  {'─' * 50}")
    print(f"  Result: RMSSD {baseline_rmssd:.1f} → {post_rmssd:.1f}ms ({delta_pct:+.0f}%)")
    print(f"          HR    {baseline_hr:.0f} → {post_hr:.0f}")
    if baseline_dfa and post_dfa:
        print(f"          DFA   {baseline_dfa:.2f} → {post_dfa:.2f}")
    if subj_before and subj_after:
        print(f"          Feel  {subj_before} → {subj_after}")
    print(f"  Saved as experiment #{db.execute('SELECT last_insert_rowid()').fetchone()[0]}")
    say(f"Result: RMSSD changed {delta_pct:+.0f} percent")


def cmd_list(args):
    db = sqlite3.connect(args.db)
    init_experiment_tables(db)
    rows = db.execute("""
        SELECT date, name, baseline_rmssd, post_rmssd, delta_pct,
               subjective_before, subjective_after, notes
        FROM experiments ORDER BY ts DESC LIMIT 20
    """).fetchall()

    if not rows:
        print("  No experiments yet. Run: python experiment.py run \"breathing 1min\"")
        return

    print(f"\n  {'Date':12s} {'Experiment':30s} {'Before':>7s} {'After':>7s} {'Δ':>6s} {'Feel':>6s}")
    print(f"  {'─' * 72}")
    for r in rows:
        feel = f"{r[5]}→{r[6]}" if r[5] and r[6] else "—"
        print(f"  {r[0]:12s} {r[1]:30s} {r[2]:6.1f}ms {r[3]:6.1f}ms {r[4]:+5.0f}% {feel:>6s}")


def cmd_compare(args):
    db = sqlite3.connect(args.db)
    init_experiment_tables(db)

    for name in args.names:
        rows = db.execute("""
            SELECT baseline_rmssd, post_rmssd, delta_pct, subjective_before, subjective_after
            FROM experiments WHERE name LIKE ? ORDER BY ts
        """, (f"%{name}%",)).fetchall()

        if not rows:
            print(f"  No experiments matching '{name}'")
            continue

        deltas = [r[2] for r in rows]
        mean_d = sum(deltas) / len(deltas)

        print(f"\n  {name} (n={len(rows)}):")
        for r in rows:
            print(f"    {r[0]:.1f} → {r[1]:.1f}ms ({r[2]:+.0f}%)")
        print(f"    Mean Δ = {mean_d:+.1f}%")

        if len(rows) >= 3:
            sd = math.sqrt(sum((d - mean_d)**2 for d in deltas) / (len(deltas)-1))
            se = sd / math.sqrt(len(deltas))
            t = mean_d / se if se > 0 else 0
            z = abs(t)
            p = 2 * (1 - 0.5 * (1 + math.erf(z / math.sqrt(2))))
            sig = "***" if p < 0.001 else "**" if p < 0.01 else "*" if p < 0.05 else "ns"
            print(f"    t = {t:.2f}, p ≈ {p:.3f} {sig}")


def cmd_weekly(args):
    db = sqlite3.connect(args.db)
    init_experiment_tables(db)

    # This week's experiments
    rows = db.execute("""
        SELECT name, count(*), avg(delta_pct), min(delta_pct), max(delta_pct)
        FROM experiments
        WHERE date >= date('now', '-7 days')
        GROUP BY name
        ORDER BY avg(delta_pct) DESC
    """).fetchall()

    print(f"\n  ── THIS WEEK'S EXPERIMENTS ──\n")
    if not rows:
        print("  No experiments this week.")
    else:
        print(f"  {'Intervention':30s} {'n':>3s} {'Mean Δ':>8s} {'Range':>15s}")
        print(f"  {'─' * 60}")
        for r in rows:
            print(f"  {r[0]:30s} {r[1]:3d} {r[2]:+7.0f}%  {r[3]:+.0f}% to {r[4]:+.0f}%")

    # Daily log
    logs = db.execute("""
        SELECT date, morning_rmssd, morning_hr, cannabis, exercise_min, breathing_min, subjective_wellbeing
        FROM daily_log WHERE date >= date('now', '-7 days') ORDER BY date
    """).fetchall()

    if logs:
        print(f"\n  ── DAILY LOG ──\n")
        print(f"  {'Date':12s} {'RMSSD':>6s} {'HR':>4s} {'🌿':>3s} {'🏃':>5s} {'🫁':>5s} {'😊':>3s}")
        print(f"  {'─' * 45}")
        for l in logs:
            print(f"  {l[0]:12s} {l[1] or 0:5.1f}  {l[2] or 0:3.0f}  {'y' if l[3] else 'n':>3s} {l[4] or 0:4.0f}m {l[5] or 0:4.0f}m  {l[6] or '-':>3}")


def cmd_log(args):
    db = sqlite3.connect(args.db)
    init_experiment_tables(db)

    today = datetime.now().strftime("%Y-%m-%d")
    print(f"\n  Daily log for {today}")
    print(f"  {'─' * 40}")

    # Auto-fill from HRV data
    morning = db.execute("""
        SELECT avg(rmssd), avg(hr_mean) FROM hrv_samples
        WHERE date(ts, 'unixepoch', 'localtime') = ?
          AND cast(strftime('%H', datetime(ts, 'unixepoch', 'localtime')) as int) BETWEEN 6 AND 10
          AND rmssd BETWEEN 1 AND 150
    """, (today,)).fetchone()

    evening = db.execute("""
        SELECT avg(rmssd), avg(hr_mean) FROM hrv_samples
        WHERE date(ts, 'unixepoch', 'localtime') = ?
          AND cast(strftime('%H', datetime(ts, 'unixepoch', 'localtime')) as int) BETWEEN 18 AND 23
          AND rmssd BETWEEN 1 AND 150
    """, (today,)).fetchone()

    experiments_today = db.execute("""
        SELECT group_concat(name, ', ') FROM experiments WHERE date = ?
    """, (today,)).fetchone()[0] or ""

    print(f"  Morning RMSSD: {morning[0]:.1f}" if morning[0] else "  Morning RMSSD: —")
    print(f"  Evening RMSSD: {evening[0]:.1f}" if evening[0] else "  Evening RMSSD: —")
    print(f"  Experiments: {experiments_today or 'none'}")

    cannabis = input("  Cannabis today? (y/n): ").strip().lower() == 'y'
    exercise_min = float(input("  Exercise minutes: ") or 0)
    breathing_min = float(input("  Breathing minutes: ") or 0)
    sleep_time = input("  Bedtime last night (HH:MM): ").strip() or None
    wake_time = input("  Wake time (HH:MM): ").strip() or None
    wellbeing = int(input("  Overall wellbeing (1-10): ") or 0) or None
    notes = input("  Notes: ").strip() or None

    db.execute("""
        INSERT OR REPLACE INTO daily_log
        (date, morning_rmssd, morning_hr, evening_rmssd, evening_hr,
         experiment_names, cannabis, exercise_min, breathing_min,
         sleep_time, wake_time, subjective_wellbeing, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (today, morning[0] if morning else None, morning[1] if morning else None,
          evening[0] if evening else None, evening[1] if evening else None,
          experiments_today, 1 if cannabis else 0, exercise_min, breathing_min,
          sleep_time, wake_time, wellbeing, notes))
    db.commit()
    print(f"\n  ✓ Logged for {today}")


def main():
    parser = argparse.ArgumentParser(description="HRV Experiment Runner")
    parser.add_argument("--db", default=DB)
    sub = parser.add_subparsers(dest="command")

    p_run = sub.add_parser("run", help="Run an experiment")
    p_run.add_argument("name", help="Experiment name (e.g. 'coherence breathing 1min')")
    p_run.add_argument("--baseline", type=int, default=120, help="Baseline seconds (default 120)")
    p_run.add_argument("--recovery", type=int, default=300, help="Recovery seconds (default 300)")

    sub.add_parser("list", help="List past experiments")

    p_cmp = sub.add_parser("compare", help="Compare experiment types")
    p_cmp.add_argument("names", nargs="+")

    sub.add_parser("weekly", help="Weekly experiment summary")
    sub.add_parser("log", help="Fill in daily log")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return

    {"run": cmd_run, "list": cmd_list, "compare": cmd_compare,
     "weekly": cmd_weekly, "log": cmd_log}[args.command](args)


if __name__ == "__main__":
    main()
