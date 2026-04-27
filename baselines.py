"""HRV baseline computation — rolling averages, Z-scores, alerts.

Usage:
    python baselines.py compute [--db hrv_data.db] [--preset quick|standard|research]
    python baselines.py status                      Show current baseline + Z-scores
    python baselines.py history                     Show baseline trend over time
    python baselines.py alert                       Check if any alerts active

Presets:
    quick     — morning readings only, 7d + 30d windows
    standard  — multi-read/day, 7d + 30d + 90d, includes DFA α1
    research  — continuous, all metrics, per-period baselines
"""
import argparse
import math
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta


DEFAULT_DB = "hrv_data.db"

PERIODS = {
    "morning": (5, 10),
    "afternoon": (12, 17),
    "evening": (17, 22),
    "night": (22, 5),
}

WINDOWS = [7, 30, 90]

# Z-score alert thresholds
Z_SPIKE = 1.5
Z_NORMAL_HI = 1.0
Z_NORMAL_LO = -1.0
Z_SUPPRESSED = -1.5
Z_CRITICAL = -2.0

# CV thresholds (Plews et al.)
CV_RECOVERED = 3.0
CV_NORMAL_HI = 10.0


@dataclass
class Baseline:
    window_days: int
    period: str
    date: str
    n_days: int
    ln_rmssd_median: float
    ln_rmssd_mean: float
    ln_rmssd_std: float
    ln_rmssd_cv: float
    hr_mean: float
    dfa_alpha1_mean: float | None
    sample_entropy_mean: float | None
    swc: float  # smallest worthwhile change


@dataclass
class Alert:
    level: str  # normal, suppressed, critical, spike
    z_score: float
    metric: str
    message: str


def get_daily_values(db: sqlite3.Connection, period: str = "morning") -> list[dict]:
    """Get one representative value per day, filtered by time-of-day period."""
    hour_start, hour_end = PERIODS.get(period, (0, 24))

    if hour_start < hour_end:
        hour_clause = f"CAST(strftime('%H', datetime(h.ts, 'unixepoch', 'localtime')) AS INT) BETWEEN {hour_start} AND {hour_end - 1}"
    else:
        hour_clause = f"(CAST(strftime('%H', datetime(h.ts, 'unixepoch', 'localtime')) AS INT) >= {hour_start} OR CAST(strftime('%H', datetime(h.ts, 'unixepoch', 'localtime')) AS INT) < {hour_end})"

    rows = db.execute(f"""
        SELECT
            date(h.ts, 'unixepoch', 'localtime') as day,
            AVG(h.rmssd) as rmssd,
            AVG(h.hr_mean) as hr,
            AVG(h.sdnn) as sdnn,
            COUNT(*) as n_samples
        FROM hrv_samples h
        WHERE {hour_clause}
          AND h.rmssd > 0
        GROUP BY day
        HAVING n_samples >= 3
        ORDER BY day
    """).fetchall()

    days = []
    for row in rows:
        ln_rmssd = math.log(max(1, row[1]))
        days.append({
            "date": row[0],
            "rmssd": row[1],
            "ln_rmssd": ln_rmssd,
            "hr": row[2],
            "sdnn": row[3],
            "n": row[4],
        })

    return days


def get_daily_advanced(db: sqlite3.Connection, period: str = "morning") -> dict[str, dict]:
    """Get daily average DFA alpha1 and sample entropy."""
    hour_start, hour_end = PERIODS.get(period, (0, 24))
    if hour_start < hour_end:
        hour_clause = f"CAST(strftime('%H', datetime(ts, 'unixepoch', 'localtime')) AS INT) BETWEEN {hour_start} AND {hour_end - 1}"
    else:
        hour_clause = f"(CAST(strftime('%H', datetime(ts, 'unixepoch', 'localtime')) AS INT) >= {hour_start} OR CAST(strftime('%H', datetime(ts, 'unixepoch', 'localtime')) AS INT) < {hour_end})"

    rows = db.execute(f"""
        SELECT date(ts, 'unixepoch', 'localtime') as day,
            AVG(dfa_alpha1) as alpha1,
            AVG(sample_entropy) as sampen
        FROM hrv_advanced
        WHERE {hour_clause}
          AND dfa_alpha1 IS NOT NULL AND dfa_alpha1 > 0
        GROUP BY day
    """).fetchall()

    return {row[0]: {"alpha1": row[1], "sampen": row[2]} for row in rows}


def compute_baseline(values: list[float], min_count: int = 5) -> dict | None:
    if len(values) < min_count:
        return None
    n = len(values)
    mean = sum(values) / n
    sorted_vals = sorted(values)
    median = sorted_vals[n // 2] if n % 2 else (sorted_vals[n//2 - 1] + sorted_vals[n//2]) / 2
    variance = sum((v - mean) ** 2 for v in values) / n
    std = math.sqrt(variance)
    cv = (std / mean * 100) if mean > 0 else 0
    swc = 0.5 * std
    return {"mean": mean, "median": median, "std": std, "cv": cv, "swc": swc}


def compute_all_baselines(db: sqlite3.Connection, preset: str = "standard") -> list[Baseline]:
    periods = ["morning"] if preset == "quick" else ["morning", "afternoon", "evening"]
    windows = [7, 30] if preset == "quick" else WINDOWS

    results = []
    today = datetime.now().strftime("%Y-%m-%d")

    for period in periods:
        days = get_daily_values(db, period)
        adv = get_daily_advanced(db, period) if preset != "quick" else {}

        for window in windows:
            recent = [d for d in days if d["date"] >= (datetime.now() - timedelta(days=window)).strftime("%Y-%m-%d")]
            if not recent:
                continue

            ln_vals = [d["ln_rmssd"] for d in recent]
            hr_vals = [d["hr"] for d in recent]

            min_days = 5 if window == 7 else (20 if window == 30 else 40)
            stats = compute_baseline(ln_vals, min_count=min(min_days, len(ln_vals)))
            if not stats:
                continue

            hr_stats = compute_baseline(hr_vals, min_count=2)

            # Advanced metrics
            alpha1_vals = [adv[d["date"]]["alpha1"] for d in recent if d["date"] in adv and adv[d["date"]]["alpha1"]]
            sampen_vals = [adv[d["date"]]["sampen"] for d in recent if d["date"] in adv and adv[d["date"]]["sampen"]]

            results.append(Baseline(
                window_days=window,
                period=period,
                date=today,
                n_days=len(recent),
                ln_rmssd_median=stats["median"],
                ln_rmssd_mean=stats["mean"],
                ln_rmssd_std=stats["std"],
                ln_rmssd_cv=stats["cv"],
                hr_mean=hr_stats["mean"] if hr_stats else 0,
                dfa_alpha1_mean=sum(alpha1_vals) / len(alpha1_vals) if alpha1_vals else None,
                sample_entropy_mean=sum(sampen_vals) / len(sampen_vals) if sampen_vals else None,
                swc=stats["swc"],
            ))

    return results


def compute_z_score(current_ln_rmssd: float, baseline: Baseline) -> float:
    if baseline.ln_rmssd_std < 0.01:
        return 0
    return (current_ln_rmssd - baseline.ln_rmssd_median) / baseline.ln_rmssd_std


def check_alerts(current_ln_rmssd: float, baselines: list[Baseline]) -> list[Alert]:
    alerts = []
    for b in baselines:
        z = compute_z_score(current_ln_rmssd, b)

        if z < Z_CRITICAL:
            alerts.append(Alert("critical", z, f"lnRMSSD ({b.window_days}d {b.period})",
                                f"RMSSD critically low — Z={z:.1f} vs {b.window_days}d {b.period} baseline"))
        elif z < Z_SUPPRESSED:
            alerts.append(Alert("suppressed", z, f"lnRMSSD ({b.window_days}d {b.period})",
                                f"RMSSD suppressed — Z={z:.1f} vs {b.window_days}d {b.period} baseline"))
        elif z > Z_SPIKE:
            alerts.append(Alert("spike", z, f"lnRMSSD ({b.window_days}d {b.period})",
                                f"RMSSD unusually high — Z={z:.1f} (possible non-functional overreaching)"))

        if b.ln_rmssd_cv > CV_NORMAL_HI and b.window_days == 7:
            alerts.append(Alert("suppressed", 0, f"CV ({b.period})",
                                f"7-day CV = {b.ln_rmssd_cv:.1f}% — high variability suggests accumulated fatigue"))

    return alerts


def save_baselines(db: sqlite3.Connection, baselines: list[Baseline]):
    for b in baselines:
        db.execute("""
            INSERT OR REPLACE INTO daily_baselines (date, ln_rmssd_mean, ln_rmssd_cv, rmssd_mean, hr_mean,
                dfa_alpha1_mean, sample_entropy_mean, z_score, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            b.date, b.ln_rmssd_mean, b.ln_rmssd_cv,
            math.exp(b.ln_rmssd_median), b.hr_mean,
            b.dfa_alpha1_mean, b.sample_entropy_mean,
            None,  # z_score filled when checking current
            f"{b.window_days}d {b.period} n={b.n_days}"
        ))
    db.commit()


def cmd_compute(args):
    db = sqlite3.connect(args.db)
    baselines = compute_all_baselines(db, args.preset)

    if not baselines:
        print("  Not enough data yet. Need at least 5 days of readings.")
        return

    save_baselines(db, baselines)

    print(f"\n  Baselines computed ({args.preset} preset)")
    print(f"  {'─' * 60}")

    for b in baselines:
        cv_status = "recovered" if b.ln_rmssd_cv < CV_RECOVERED else "normal" if b.ln_rmssd_cv < CV_NORMAL_HI else "FATIGUED"
        alpha_str = f"  α1={b.dfa_alpha1_mean:.2f}" if b.dfa_alpha1_mean else ""
        print(f"  {b.window_days:2d}d {b.period:10s}  "
              f"lnRMSSD={b.ln_rmssd_median:.2f} ±{b.ln_rmssd_std:.2f}  "
              f"CV={b.ln_rmssd_cv:.1f}% ({cv_status})  "
              f"HR={b.hr_mean:.0f}  "
              f"SWC=±{b.swc:.2f}{alpha_str}  "
              f"[{b.n_days} days]")


def cmd_status(args):
    db = sqlite3.connect(args.db)
    baselines = compute_all_baselines(db, "standard")

    if not baselines:
        print("  Not enough data for baselines.")
        return

    # Get current values
    row = db.execute("""
        SELECT rmssd, hr_mean FROM hrv_samples ORDER BY ts DESC LIMIT 1
    """).fetchone()

    if not row:
        print("  No current data.")
        return

    current_rmssd = row[0]
    current_ln = math.log(max(1, current_rmssd))

    print(f"\n  Current: RMSSD={current_rmssd:.1f}ms  lnRMSSD={current_ln:.2f}  HR={row[1]:.0f}")
    print(f"  {'─' * 60}")

    for b in baselines:
        z = compute_z_score(current_ln, b)
        delta = current_ln - b.ln_rmssd_median
        delta_pct = (math.exp(current_ln) / math.exp(b.ln_rmssd_median) - 1) * 100
        swc_multiples = abs(delta) / b.swc if b.swc > 0 else 0

        status = "●" if abs(z) < 1 else "▲" if z > 1 else "▼"
        color_word = "normal" if abs(z) < 1 else ("HIGH" if z > 0 else "LOW")

        print(f"  {status} vs {b.window_days:2d}d {b.period:10s}  "
              f"Z={z:+.1f} ({color_word})  "
              f"Δ={delta_pct:+.0f}%  "
              f"{swc_multiples:.1f}×SWC")

    # Alerts
    alerts = check_alerts(current_ln, baselines)
    if alerts:
        print(f"\n  Alerts:")
        for a in alerts:
            icon = {"critical": "🔴", "suppressed": "🟡", "spike": "🔵", "normal": "🟢"}.get(a.level, "⚪")
            print(f"    {icon} {a.message}")
    else:
        print(f"\n  ✓ All metrics within normal range")


def cmd_history(args):
    db = sqlite3.connect(args.db)
    rows = db.execute("""
        SELECT date, ln_rmssd_mean, ln_rmssd_cv, rmssd_mean, hr_mean, dfa_alpha1_mean, notes
        FROM daily_baselines ORDER BY date DESC LIMIT 30
    """).fetchall()

    if not rows:
        print("  No baseline history. Run: python baselines.py compute")
        return

    print(f"\n  {'Date':12s} {'lnRMSSD':>8s} {'CV%':>6s} {'RMSSD':>7s} {'HR':>5s} {'α1':>6s} {'Notes'}")
    print(f"  {'─' * 60}")
    for r in rows:
        alpha_str = f"{r[5]:.2f}" if r[5] else "—"
        print(f"  {r[0]:12s} {r[1]:8.2f} {r[2]:5.1f}% {r[3]:6.1f}ms {r[4]:5.0f} {alpha_str:>6s} {r[6] or ''}")


def cmd_alert(args):
    db = sqlite3.connect(args.db)
    baselines = compute_all_baselines(db, "standard")
    row = db.execute("SELECT rmssd FROM hrv_samples ORDER BY ts DESC LIMIT 1").fetchone()

    if not row or not baselines:
        print("  Not enough data.")
        return

    current_ln = math.log(max(1, row[0]))
    alerts = check_alerts(current_ln, baselines)

    if alerts:
        for a in alerts:
            icon = {"critical": "🔴", "suppressed": "🟡", "spike": "🔵"}.get(a.level, "⚪")
            print(f"  {icon} [{a.level.upper()}] {a.message}")
    else:
        print("  ✓ No alerts. All metrics within normal range.")


def main():
    parser = argparse.ArgumentParser(description="HRV Baseline Computation")
    parser.add_argument("--db", default=DEFAULT_DB)
    sub = parser.add_subparsers(dest="command")

    p_compute = sub.add_parser("compute", help="Compute baselines")
    p_compute.add_argument("--preset", choices=["quick", "standard", "research"], default="standard")

    sub.add_parser("status", help="Show current status vs baselines")
    sub.add_parser("history", help="Show baseline trend")
    sub.add_parser("alert", help="Check for active alerts")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return

    {"compute": cmd_compute, "status": cmd_status, "history": cmd_history, "alert": cmd_alert}[args.command](args)


if __name__ == "__main__":
    main()
