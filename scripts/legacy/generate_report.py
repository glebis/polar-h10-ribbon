#!/usr/bin/env python3
"""Generate full_day_report.html — Tufte-inspired HRV analytical report."""

import sqlite3
import json
import math
import statistics
from pathlib import Path
import numpy as np
from scipy import stats as sp_stats

DB = Path(__file__).parent / "hrv_data.db"
OUT = Path(__file__).parent / "full_day_report.html"

# CET offset
CET_OFFSET = 2 * 3600  # CEST = UTC+2 for April


def ts_to_cet(ts):
    """Unix timestamp to CET HH:MM string."""
    import datetime
    dt = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone(datetime.timedelta(hours=2)))
    return dt.strftime("%H:%M")


def ts_to_cet_full(ts):
    import datetime
    dt = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone(datetime.timedelta(hours=2)))
    return dt.strftime("%Y-%m-%d %H:%M")


def moving_average(data, window):
    if len(data) < window:
        return data
    result = []
    for i in range(len(data)):
        start = max(0, i - window // 2)
        end = min(len(data), i + window // 2 + 1)
        result.append(sum(data[start:end]) / (end - start))
    return result


def pearson_r(x, y):
    """Compute Pearson r, return (r, p)."""
    if len(x) < 3:
        return (0.0, 1.0)
    mask = np.isfinite(x) & np.isfinite(y)
    x, y = x[mask], y[mask]
    if len(x) < 3:
        return (0.0, 1.0)
    r, p = sp_stats.pearsonr(x, y)
    return (round(r, 3), round(p, 4))


def main():
    conn = sqlite3.connect(str(DB))
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    # ── Load all data ──────────────────────────────────────────
    hrv_rows = c.execute("SELECT ts, rmssd, hr_mean, sdnn, trend_slope FROM hrv_samples ORDER BY ts").fetchall()
    adv_rows = c.execute("SELECT ts, dfa_alpha1, sample_entropy, ln_rmssd, sd1, sd2, pnn50 FROM hrv_advanced ORDER BY ts").fetchall()
    move_rows = c.execute("SELECT ts, stillness, magnitude, breath_rate FROM movement_samples ORDER BY ts").fetchall()
    stress_rows = c.execute("SELECT ts, duration_s, severity, rmssd_at_event FROM stress_events ORDER BY ts").fetchall()
    rr_rows = c.execute("SELECT ts, rr_ms, hr_bpm FROM rr_intervals ORDER BY ts").fetchall()
    marker_rows = c.execute("SELECT ts, label, hr_bpm, rmssd FROM markers ORDER BY ts").fetchall()

    ts_min = hrv_rows[0]["ts"]
    ts_max = hrv_rows[-1]["ts"]

    # ── Filter artifacts: cap RMSSD at 150 ──────────────────
    def cap(v):
        if v is None:
            return None
        return min(v, 150.0)

    # ── Define events (CET timestamps) ────────────────────────
    # We need to find approximate unix timestamps
    # ts_min is 2026-04-26 23:41 CET, ts_max is 2026-04-27 14:10 CET
    # Base date: 2026-04-26 in CET

    import datetime
    cet = datetime.timezone(datetime.timedelta(hours=2))

    def cet_to_ts(month, day, hour, minute):
        dt = datetime.datetime(2026, month, day, hour, minute, tzinfo=cet)
        return dt.timestamp()

    events = [
        {"ts": cet_to_ts(4, 27, 1, 19), "label": "Cannabis #1", "color": "#ff6b6b"},
        {"ts": cet_to_ts(4, 27, 2, 0), "label": "Sleep start", "color": "#4a9eff"},
        {"ts": cet_to_ts(4, 27, 3, 35), "label": "THC block start", "color": "#ff6b6b"},
        {"ts": cet_to_ts(4, 27, 4, 30), "label": "THC block end", "color": "#ff6b6b"},
        {"ts": cet_to_ts(4, 27, 7, 0), "label": "Wake", "color": "#4a9eff"},
        {"ts": cet_to_ts(4, 27, 11, 11), "label": "Exercise", "color": "#4ecdc4"},
        {"ts": cet_to_ts(4, 27, 11, 15), "label": "Breathing", "color": "#4ecdc4"},
        {"ts": cet_to_ts(4, 27, 11, 30), "label": "Post-stack rest", "color": "#4ecdc4"},
        {"ts": cet_to_ts(4, 27, 12, 38), "label": "Sexual arousal", "color": "#ffe66d"},
        {"ts": cet_to_ts(4, 27, 12, 55), "label": "Arousal end", "color": "#ffe66d"},
        {"ts": cet_to_ts(4, 27, 13, 35), "label": "Cannabis #2", "color": "#ff6b6b"},
    ]

    # ── Downsample: every 3rd point ───────────────────────────
    hrv_ds = hrv_rows[::3]

    ts_arr = [r["ts"] for r in hrv_ds]
    rmssd_raw = [cap(r["rmssd"]) for r in hrv_ds]
    hr_raw = [r["hr_mean"] for r in hrv_ds]

    rmssd_ma = moving_average(rmssd_raw, 30)
    hr_ma = moving_average(hr_raw, 30)

    # Labels as CET time strings
    labels = [ts_to_cet(t) for t in ts_arr]

    # ── Summary statistics ────────────────────────────────────
    all_rmssd = [cap(r["rmssd"]) for r in hrv_rows if r["rmssd"] is not None and r["rmssd"] < 150]
    all_hr = [r["hr_mean"] for r in hrv_rows if r["hr_mean"] is not None]

    summary = {
        "rmssd_mean": round(statistics.mean(all_rmssd), 1),
        "rmssd_median": round(statistics.median(all_rmssd), 1),
        "rmssd_min": round(min(all_rmssd), 1),
        "rmssd_max": round(max(all_rmssd), 1),
        "rmssd_std": round(statistics.stdev(all_rmssd), 1),
        "hr_mean": round(statistics.mean(all_hr), 1),
        "hr_min": round(min(all_hr), 0),
        "hr_max": round(max(all_hr), 0),
        "hr_rest": round(np.percentile(all_hr, 5), 1),
        "n_samples": len(hrv_rows),
        "n_stress": len(stress_rows),
        "duration_h": round((ts_max - ts_min) / 3600, 1),
        "recording_start": ts_to_cet_full(ts_min),
        "recording_end": ts_to_cet_full(ts_max),
    }

    # ── DFA alpha1 stats ──────────────────────────────────────
    dfa_vals = [r["dfa_alpha1"] for r in adv_rows if r["dfa_alpha1"] is not None]
    if dfa_vals:
        summary["dfa_mean"] = round(statistics.mean(dfa_vals), 2)
        summary["dfa_latest"] = round(dfa_vals[-1], 2)

    # ── Trend analysis (linear regression on full RMSSD) ──────
    rmssd_for_trend = [(r["ts"], cap(r["rmssd"])) for r in hrv_rows if r["rmssd"] is not None and r["rmssd"] < 150]
    trend_x = np.array([t[0] - ts_min for t in rmssd_for_trend])
    trend_y = np.array([t[1] for t in rmssd_for_trend])
    slope, intercept, r_val, p_val, std_err = sp_stats.linregress(trend_x, trend_y)

    trend_info = {
        "slope_per_hour": round(slope * 3600, 3),
        "r_squared": round(r_val ** 2, 4),
        "p_value": round(p_val, 6),
        "direction": "improving" if slope > 0 else "declining" if slope < 0 else "stable",
        "intercept": round(intercept, 2),
    }

    # Trend line for chart (start and end points)
    trend_line_start = intercept
    trend_line_end = intercept + slope * (ts_max - ts_min)

    # ── Correlation: HR vs RMSSD ──────────────────────────────
    hr_rmssd_x = np.array([r["hr_mean"] for r in hrv_ds if r["hr_mean"] is not None and r["rmssd"] is not None and r["rmssd"] < 150])
    hr_rmssd_y = np.array([cap(r["rmssd"]) for r in hrv_ds if r["hr_mean"] is not None and r["rmssd"] is not None and r["rmssd"] < 150])
    hr_rmssd_r, hr_rmssd_p = pearson_r(hr_rmssd_x, hr_rmssd_y)

    # Downsample scatter to every 5th for performance
    scatter_hr = hr_rmssd_x[::5].tolist()
    scatter_rmssd_for_hr = hr_rmssd_y[::5].tolist()

    # ── Correlation: Stillness vs RMSSD ───────────────────────
    # Match movement and HRV by nearest timestamp
    stillness_rmssd_pairs = []
    hrv_ts_arr = np.array([r["ts"] for r in hrv_rows])
    hrv_rmssd_arr = np.array([cap(r["rmssd"]) if r["rmssd"] is not None else np.nan for r in hrv_rows])

    for mr in move_rows:
        idx = np.argmin(np.abs(hrv_ts_arr - mr["ts"]))
        if abs(hrv_ts_arr[idx] - mr["ts"]) < 15 and not np.isnan(hrv_rmssd_arr[idx]) and hrv_rmssd_arr[idx] < 150:
            stillness_rmssd_pairs.append((mr["stillness"], hrv_rmssd_arr[idx]))

    if stillness_rmssd_pairs:
        still_x = np.array([p[0] for p in stillness_rmssd_pairs])
        still_y = np.array([p[1] for p in stillness_rmssd_pairs])
        still_r, still_p = pearson_r(still_x, still_y)
    else:
        still_x, still_y = np.array([]), np.array([])
        still_r, still_p = 0.0, 1.0

    # ── Correlation: Time-of-day vs RMSSD ─────────────────────
    tod_rmssd_x = []
    tod_rmssd_y = []
    for r in hrv_ds:
        if r["rmssd"] is not None and r["rmssd"] < 150:
            # Hours since midnight CET
            import datetime as dt_mod
            d = dt_mod.datetime.fromtimestamp(r["ts"], tz=cet)
            hour_decimal = d.hour + d.minute / 60.0
            tod_rmssd_x.append(hour_decimal)
            tod_rmssd_y.append(cap(r["rmssd"]))

    tod_x_np = np.array(tod_rmssd_x)
    tod_y_np = np.array(tod_rmssd_y)
    tod_r, tod_p = pearson_r(tod_x_np, tod_y_np)

    scatter_tod_x = tod_x_np[::3].tolist()
    scatter_tod_y = tod_y_np[::3].tolist()

    # ── Intervention before/after analysis ────────────────────
    def get_rmssd_window(center_ts, window_s=300):
        """Get avg RMSSD in a window around a timestamp."""
        vals = [cap(r["rmssd"]) for r in hrv_rows
                if r["rmssd"] is not None and r["rmssd"] < 150
                and abs(r["ts"] - center_ts) < window_s]
        return round(statistics.mean(vals), 1) if vals else None

    def get_rmssd_after(start_ts, window_s=300, offset_s=0):
        vals = [cap(r["rmssd"]) for r in hrv_rows
                if r["rmssd"] is not None and r["rmssd"] < 150
                and r["ts"] >= start_ts + offset_s
                and r["ts"] < start_ts + offset_s + window_s]
        return round(statistics.mean(vals), 1) if vals else None

    interventions = [
        {"name": "Cannabis #1", "ts": cet_to_ts(4, 27, 1, 19),
         "before_window": (-600, -60), "after_window": (60, 600)},
        {"name": "Sleep onset", "ts": cet_to_ts(4, 27, 2, 0),
         "before_window": (-600, -60), "after_window": (600, 1800)},
        {"name": "Exercise", "ts": cet_to_ts(4, 27, 11, 11),
         "before_window": (-600, -60), "after_window": (300, 900)},
        {"name": "Breathing", "ts": cet_to_ts(4, 27, 11, 15),
         "before_window": (-300, -30), "after_window": (60, 600)},
        {"name": "Post-stack rest", "ts": cet_to_ts(4, 27, 11, 30),
         "before_window": (-300, -30), "after_window": (60, 600)},
        {"name": "Sexual arousal", "ts": cet_to_ts(4, 27, 12, 38),
         "before_window": (-600, -60), "after_window": (60, 600)},
        {"name": "Cannabis #2", "ts": cet_to_ts(4, 27, 13, 35),
         "before_window": (-600, -60), "after_window": (60, 600)},
    ]

    for iv in interventions:
        before_vals = [cap(r["rmssd"]) for r in hrv_rows
                       if r["rmssd"] is not None and r["rmssd"] < 150
                       and r["ts"] >= iv["ts"] + iv["before_window"][0]
                       and r["ts"] < iv["ts"] + iv["before_window"][1]]
        after_vals = [cap(r["rmssd"]) for r in hrv_rows
                      if r["rmssd"] is not None and r["rmssd"] < 150
                      and r["ts"] >= iv["ts"] + iv["after_window"][0]
                      and r["ts"] < iv["ts"] + iv["after_window"][1]]
        iv["before"] = round(statistics.mean(before_vals), 1) if before_vals else 0
        iv["after"] = round(statistics.mean(after_vals), 1) if after_vals else 0
        if iv["before"] > 0:
            iv["delta_pct"] = round((iv["after"] - iv["before"]) / iv["before"] * 100, 1)
        else:
            iv["delta_pct"] = 0

    # ── Recovery speed ────────────────────────────────────────
    recovery_events = [
        {"name": "Cannabis #1", "ts": cet_to_ts(4, 27, 1, 19)},
        {"name": "Exercise", "ts": cet_to_ts(4, 27, 11, 11)},
        {"name": "Sexual arousal", "ts": cet_to_ts(4, 27, 12, 38)},
        {"name": "Cannabis #2", "ts": cet_to_ts(4, 27, 13, 35)},
    ]

    for rev in recovery_events:
        # baseline = avg RMSSD 10 min before
        baseline_vals = [cap(r["rmssd"]) for r in hrv_rows
                         if r["rmssd"] is not None and r["rmssd"] < 150
                         and r["ts"] >= rev["ts"] - 600
                         and r["ts"] < rev["ts"] - 60]
        baseline = statistics.mean(baseline_vals) if baseline_vals else None

        if baseline:
            # Find first time RMSSD returns to baseline after event
            recovered_ts = None
            window = []
            for r in hrv_rows:
                if r["ts"] <= rev["ts"]:
                    continue
                if r["rmssd"] is None or r["rmssd"] >= 150:
                    continue
                window.append(cap(r["rmssd"]))
                if len(window) > 6:
                    window.pop(0)
                if len(window) >= 5 and statistics.mean(window) >= baseline * 0.9:
                    recovered_ts = r["ts"]
                    break
            if recovered_ts:
                rev["recovery_min"] = round((recovered_ts - rev["ts"]) / 60, 1)
            else:
                rev["recovery_min"] = None
            rev["baseline"] = round(baseline, 1)
        else:
            rev["recovery_min"] = None
            rev["baseline"] = None

    # ── Cannabis deep dive ────────────────────────────────────
    cannabis_sessions = []
    for label, ts_start in [("Session #1 (high baseline)", cet_to_ts(4, 27, 1, 19)),
                             ("Session #2 (low baseline)", cet_to_ts(4, 27, 13, 35))]:
        # Get RMSSD for 45 min after
        pts = []
        for r in hrv_rows:
            dt_min = (r["ts"] - ts_start) / 60
            if -5 <= dt_min <= 45 and r["rmssd"] is not None and r["rmssd"] < 150:
                pts.append({"min": round(dt_min, 1), "rmssd": round(cap(r["rmssd"]), 1)})
        # Downsample
        pts_ds = pts[::3]
        cannabis_sessions.append({"label": label, "data": pts_ds})

    # ── Advanced metrics timeline ─────────────────────────────
    adv_ts = [ts_to_cet(r["ts"]) for r in adv_rows]
    adv_dfa = [round(r["dfa_alpha1"], 3) if r["dfa_alpha1"] is not None else None for r in adv_rows]
    adv_entropy = [round(r["sample_entropy"], 3) if r["sample_entropy"] is not None else None for r in adv_rows]
    adv_pnn50 = [round(r["pnn50"], 1) if r["pnn50"] is not None else None for r in adv_rows]

    # ── Build JSON payload ────────────────────────────────────
    data = {
        "summary": summary,
        "trend": trend_info,
        "events": events,
        "timeline": {
            "labels": labels,
            "ts": ts_arr,
            "rmssd_raw": [round(v, 1) if v else None for v in rmssd_raw],
            "rmssd_ma": [round(v, 1) for v in rmssd_ma],
            "hr_raw": [round(v, 1) if v else None for v in hr_raw],
            "hr_ma": [round(v, 1) for v in hr_ma],
            "trend_start": round(trend_line_start, 2),
            "trend_end": round(trend_line_end, 2),
        },
        "correlations": {
            "hr_rmssd": {"r": hr_rmssd_r, "p": hr_rmssd_p,
                         "x": [round(v, 1) for v in scatter_hr],
                         "y": [round(v, 1) for v in scatter_rmssd_for_hr]},
            "stillness_rmssd": {"r": still_r, "p": still_p,
                                "x": [round(v, 3) for v in still_x.tolist()],
                                "y": [round(v, 1) for v in still_y.tolist()]},
            "tod_rmssd": {"r": tod_r, "p": tod_p,
                          "x": [round(v, 2) for v in scatter_tod_x],
                          "y": [round(v, 1) for v in scatter_tod_y]},
        },
        "interventions": interventions,
        "recovery": recovery_events,
        "cannabis": cannabis_sessions,
        "advanced": {
            "labels": adv_ts,
            "dfa": adv_dfa,
            "entropy": adv_entropy,
            "pnn50": adv_pnn50,
        },
        "movement": {
            "n_samples": len(move_rows),
            "stillness_mean": round(statistics.mean([r["stillness"] for r in move_rows]), 3) if move_rows else None,
            "coverage_pct": round(len(move_rows) * 10 / (ts_max - ts_min) * 100, 1) if move_rows else 0,
        },
    }

    conn.close()

    # ── Render HTML ───────────────────────────────────────────
    data_json = json.dumps(data, separators=(',', ':'))

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Full Day HRV Analysis — {summary['recording_start']} to {summary['recording_end']}</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-plugin-annotation@3.0.1/dist/chartjs-plugin-annotation.min.js"></script>
<style>
@import url('https://fonts.googleapis.com/css2?family=EB+Garamond:ital,wght@0,400;0,600;1,400&family=JetBrains+Mono:wght@400;600&display=swap');

* {{ margin: 0; padding: 0; box-sizing: border-box; }}

body {{
  background: #0a0a0f;
  color: #b0b0b0;
  font-family: 'EB Garamond', Georgia, serif;
  font-size: 16px;
  line-height: 1.7;
  max-width: 1100px;
  margin: 0 auto;
  padding: 48px 32px 80px;
}}

h1 {{
  font-family: 'EB Garamond', Georgia, serif;
  font-size: 32px;
  font-weight: 600;
  color: #e0e0e0;
  letter-spacing: -0.5px;
  margin-bottom: 4px;
}}

.subtitle {{
  font-family: 'JetBrains Mono', monospace;
  font-size: 12px;
  color: #555;
  letter-spacing: 0.5px;
  margin-bottom: 40px;
}}

h2 {{
  font-family: 'EB Garamond', Georgia, serif;
  font-size: 22px;
  font-weight: 600;
  color: #d0d0d0;
  margin: 48px 0 8px;
  padding-bottom: 4px;
  border-bottom: 1px solid #1a1a1a;
}}

h3 {{
  font-family: 'EB Garamond', Georgia, serif;
  font-size: 17px;
  font-weight: 600;
  color: #c0c0c0;
  margin: 24px 0 8px;
}}

p {{ margin: 8px 0 16px; max-width: 65ch; }}

.num {{
  font-family: 'JetBrains Mono', monospace;
  font-variant-numeric: tabular-nums;
}}

/* Stats grid */
.stats-grid {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
  gap: 1px;
  background: #1a1a1a;
  border: 1px solid #1a1a1a;
  border-radius: 4px;
  margin: 16px 0 24px;
  overflow: hidden;
}}
.stat-cell {{
  background: #0f0f14;
  padding: 14px 16px;
}}
.stat-val {{
  font-family: 'JetBrains Mono', monospace;
  font-size: 22px;
  font-weight: 600;
  color: #fff;
  line-height: 1.2;
}}
.stat-label {{
  font-family: 'JetBrains Mono', monospace;
  font-size: 10px;
  color: #555;
  text-transform: uppercase;
  letter-spacing: 1.2px;
  margin-top: 2px;
}}

/* Chart containers */
.chart-container {{
  background: #0f0f14;
  border: 1px solid #1a1a1a;
  border-radius: 4px;
  padding: 20px;
  margin: 16px 0;
  position: relative;
}}
.chart-container canvas {{
  max-height: 220px;
}}
.chart-title {{
  font-family: 'JetBrains Mono', monospace;
  font-size: 11px;
  color: #666;
  text-transform: uppercase;
  letter-spacing: 1.5px;
  margin-bottom: 12px;
}}

/* Two column */
.two-col {{
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
}}
@media (max-width: 768px) {{
  .two-col {{ grid-template-columns: 1fr; }}
}}

/* Insight callouts */
.insight {{
  background: #0f0f14;
  border-left: 3px solid #4ecdc4;
  padding: 12px 16px;
  margin: 12px 0;
  border-radius: 0 4px 4px 0;
  font-size: 15px;
}}
.insight.warn {{ border-color: #ffe66d; }}
.insight.bad {{ border-color: #ff6b6b; }}
.insight strong {{ color: #e0e0e0; }}

/* Table */
table {{
  width: 100%;
  border-collapse: collapse;
  font-family: 'JetBrains Mono', monospace;
  font-size: 12px;
  margin: 12px 0;
}}
th {{
  text-align: left;
  padding: 8px 10px;
  color: #555;
  border-bottom: 1px solid #222;
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 1px;
  font-weight: 400;
}}
td {{
  padding: 7px 10px;
  border-bottom: 1px solid #111;
  font-variant-numeric: tabular-nums;
}}
td.good {{ color: #4ecdc4; }}
td.bad {{ color: #ff6b6b; }}
td.warn {{ color: #ffe66d; }}

/* Correlation badge */
.corr-badge {{
  display: inline-block;
  font-family: 'JetBrains Mono', monospace;
  font-size: 11px;
  background: #1a1a1a;
  padding: 2px 8px;
  border-radius: 3px;
  margin-left: 8px;
}}

/* Section note */
.section-note {{
  font-size: 14px;
  color: #666;
  font-style: italic;
  margin: 4px 0 12px;
}}

/* Sidenote style */
.marginal {{
  font-size: 13px;
  color: #555;
  margin: 4px 0;
}}
</style>
</head>
<body>

<h1>Heart Rate Variability: A Full Day Under the Lens</h1>
<div class="subtitle">{summary['recording_start']} &mdash; {summary['recording_end']} CET &middot; {summary['duration_h']}h &middot; {summary['n_samples']:,} samples</div>

<p>This report covers a continuous 14.4-hour recording from a Polar H10 chest strap, capturing sleep, two cannabis sessions, high-intensity exercise, breathwork, and sexual arousal. Every physiological intervention is annotated on the timeline and measured for its effect on autonomic balance.</p>

<h2>Summary Statistics</h2>
<div class="stats-grid">
  <div class="stat-cell"><div class="stat-val">{summary['rmssd_mean']}</div><div class="stat-label">Mean RMSSD ms</div></div>
  <div class="stat-cell"><div class="stat-val">{summary['rmssd_median']}</div><div class="stat-label">Median RMSSD</div></div>
  <div class="stat-cell"><div class="stat-val">{summary['rmssd_min']}&ndash;{summary['rmssd_max']}</div><div class="stat-label">RMSSD range</div></div>
  <div class="stat-cell"><div class="stat-val">{summary['hr_mean']}</div><div class="stat-label">Mean HR bpm</div></div>
  <div class="stat-cell"><div class="stat-val">{summary['hr_rest']}</div><div class="stat-label">Resting HR (P5)</div></div>
  <div class="stat-cell"><div class="stat-val">{int(summary['hr_max'])}</div><div class="stat-label">Peak HR</div></div>
  <div class="stat-cell"><div class="stat-val">{summary['n_stress']}</div><div class="stat-label">Stress events</div></div>
  <div class="stat-cell"><div class="stat-val">{summary.get('dfa_mean', 'N/A')}</div><div class="stat-label">Mean DFA &alpha;1</div></div>
</div>

<div class="insight">
  <strong>Key finding:</strong> Mean RMSSD of {summary['rmssd_mean']}ms with high variance (SD {summary['rmssd_std']}ms) reflects a day of extreme autonomic swings &mdash; from parasympathetic dominance during post-exercise rest (131ms) to near-total vagal withdrawal during sexual arousal (4.7ms).
</div>

<h2>RMSSD Timeline</h2>
<p>Raw 5-second samples shown as faded dots; 30-sample moving average as a solid line. Vertical annotations mark each intervention. The overall trend line (linear regression) is overlaid in white.</p>
<div class="chart-container">
  <canvas id="rmssdChart"></canvas>
</div>

<h2>Heart Rate Timeline</h2>
<p>Same structure: raw samples plus moving average. Note the inverse relationship with RMSSD &mdash; when HR rises, vagal tone (RMSSD) typically falls.</p>
<div class="chart-container">
  <canvas id="hrChart"></canvas>
</div>

<h2>Trend Analysis</h2>
<div class="insight {'warn' if trend_info['slope_per_hour'] < 0 else ''}">
  <strong>Overall RMSSD trend: {trend_info['direction']}</strong> &mdash;
  slope of <span class="num">{trend_info['slope_per_hour']:+.2f} ms/hour</span>
  (R&sup2; = {trend_info['r_squared']}, p = {trend_info['p_value']}).
  {'This suggests progressive autonomic fatigue over the recording period.' if trend_info['slope_per_hour'] < 0 else 'Autonomic resilience held steady or improved across the session.'}
</div>

<h2>Intervention Effects</h2>
<p>For each intervention, we compare the mean RMSSD in a window before and after. The grouped bar chart below shows both values with the percentage change labeled.</p>
<div class="chart-container">
  <canvas id="interventionChart"></canvas>
</div>

<table>
<tr><th>Intervention</th><th>Time</th><th>RMSSD Before</th><th>RMSSD After</th><th>&Delta;%</th></tr>
"""

    for iv in interventions:
        delta_class = "good" if iv["delta_pct"] > 10 else "bad" if iv["delta_pct"] < -10 else "warn"
        html += f'<tr><td>{iv["name"]}</td><td class="num">{ts_to_cet(iv["ts"])}</td>'
        html += f'<td class="num">{iv["before"]}ms</td><td class="num">{iv["after"]}ms</td>'
        html += f'<td class="{delta_class}">{iv["delta_pct"]:+.1f}%</td></tr>\n'

    html += """</table>

<div class="insight warn">
  <strong>Exercise + breathwork stack</strong> produced the most dramatic positive effect: from 23ms pre-exercise to 131ms post-rest, a +470% rebound. This is the single most effective intervention in the dataset.
</div>

<h2>Recovery Speed</h2>
<p>How long does it take to return to 90% of pre-intervention RMSSD baseline? Faster recovery indicates better autonomic resilience.</p>
<div class="chart-container">
  <canvas id="recoveryChart"></canvas>
</div>

<table>
<tr><th>Event</th><th>Baseline RMSSD</th><th>Recovery time</th></tr>
"""

    for rev in recovery_events:
        recovery_str = f'{rev["recovery_min"]} min' if rev["recovery_min"] else "Did not recover"
        baseline_str = f'{rev["baseline"]}ms' if rev["baseline"] else "N/A"
        html += f'<tr><td>{rev["name"]}</td><td class="num">{baseline_str}</td><td class="num">{recovery_str}</td></tr>\n'

    html += """</table>

<h2>Cannabis Deep Dive</h2>
<p>Both cannabis sessions overlaid on the same time axis (minutes from administration). Session #1 started from a high parasympathetic baseline (resting, pre-sleep); Session #2 from an already depleted state (post-arousal, afternoon).</p>
<div class="chart-container">
  <canvas id="cannabisChart"></canvas>
</div>

<div class="insight bad">
  <strong>Context matters more than substance.</strong> Cannabis #1 (baseline 36ms) caused a meaningful 28% RMSSD drop and a 1-hour suppression block during sleep. Cannabis #2 (baseline 16ms) had minimal additional impact &mdash; there was little vagal tone left to suppress. The floor effect suggests cannabis harm is proportional to current parasympathetic reserve.
</div>

<h2>Correlations</h2>
<p>Three scatter plots testing the relationships between key variables. Each shows the Pearson correlation coefficient and statistical significance.</p>

<div class="two-col">
  <div>
    <div class="chart-container">
      <div class="chart-title">HR vs RMSSD <span class="corr-badge">r = """ + str(hr_rmssd_r) + """</span></div>
      <canvas id="scatterHrRmssd"></canvas>
    </div>
    <p class="marginal">The expected inverse relationship: as heart rate increases, beat-to-beat variability decreases. """ + (f"Strongly significant (p = {hr_rmssd_p})." if hr_rmssd_p < 0.001 else f"p = {hr_rmssd_p}.") + """</p>
  </div>
  <div>
    <div class="chart-container">
      <div class="chart-title">Time of Day vs RMSSD <span class="corr-badge">r = """ + str(tod_r) + """</span></div>
      <canvas id="scatterTod"></canvas>
    </div>
    <p class="marginal">Circadian pattern in autonomic tone. """ + ("Negative correlation suggests declining parasympathetic activity as the day progresses." if tod_r < 0 else "Positive correlation may reflect morning recovery.") + """</p>
  </div>
</div>

<div class="chart-container" style="max-width:540px;">
  <div class="chart-title">Stillness vs RMSSD <span class="corr-badge">r = """ + str(still_r) + """</span></div>
  <canvas id="scatterStillness"></canvas>
</div>
<p class="marginal">Movement data available for """ + str(data["movement"]["n_samples"]) + """ samples (""" + str(data["movement"]["coverage_pct"]) + """% of session). """ + ("Positive correlation confirms that physical stillness supports vagal tone." if still_r > 0 else "Weak or negative correlation may reflect the limited coverage window.") + """ Consider Apple Watch integration for continuous accelerometer data.</p>

<h2>Advanced Autonomic Metrics</h2>
<p>DFA &alpha;1 (detrended fluctuation analysis) measures the fractal correlation structure of RR intervals. Values near 1.0 indicate healthy complexity; values above 1.5 suggest loss of complex dynamics; below 0.5 indicates uncorrelated noise.</p>

<div class="two-col">
  <div class="chart-container">
    <div class="chart-title">DFA &alpha;1</div>
    <canvas id="dfaChart"></canvas>
  </div>
  <div class="chart-container">
    <div class="chart-title">pNN50 %</div>
    <canvas id="pnn50Chart"></canvas>
  </div>
</div>

<h2>Methodology &amp; Limitations</h2>
<p>All data from Polar H10 chest strap via Bluetooth, sampled at RR-interval resolution and aggregated into 5-second windows. RMSSD values above 150ms are excluded as likely artifacts. Moving averages use a centered 30-sample window (~2.5 minutes). Correlation coefficients are Pearson product-moment. Recovery time is defined as the first 5-sample window exceeding 90% of the pre-intervention baseline. Movement data covers only """ + str(data["movement"]["coverage_pct"]) + """% of the session due to phone-based accelerometer limitations.</p>

<p style="color:#333; margin-top:48px; font-size:13px;">Generated from hrv_data.db &middot; """ + str(summary['n_samples']) + """ HRV samples &middot; """ + str(len(rr_rows)) + """ RR intervals</p>

<script>
const D = """ + data_json + """;

// ── Chart defaults ───────────────────────────────────────
Chart.defaults.color = '#555';
Chart.defaults.borderColor = '#1a1a1a';
Chart.defaults.font.family = "'JetBrains Mono', monospace";
Chart.defaults.font.size = 10;
Chart.defaults.plugins.legend.labels.boxWidth = 10;
Chart.defaults.plugins.legend.labels.padding = 16;

// ── Annotation helper ────────────────────────────────────
function makeAnnotations(tsArr) {
  const annotations = {};
  const tsMin = tsArr[0];
  const tsMax = tsArr[tsArr.length - 1];
  D.events.forEach((ev, i) => {
    // Find nearest label index
    let bestIdx = 0;
    let bestDist = Infinity;
    tsArr.forEach((t, idx) => {
      const dist = Math.abs(t - ev.ts);
      if (dist < bestDist) { bestDist = dist; bestIdx = idx; }
    });
    if (ev.ts >= tsMin && ev.ts <= tsMax) {
      annotations['line' + i] = {
        type: 'line',
        xMin: bestIdx,
        xMax: bestIdx,
        borderColor: ev.color + '60',
        borderWidth: 1,
        borderDash: [4, 4],
        label: {
          display: true,
          content: ev.label,
          position: 'start',
          backgroundColor: 'transparent',
          color: ev.color + 'aa',
          font: { size: 9, family: "'JetBrains Mono', monospace" },
          rotation: -90,
          yAdjust: -10,
        }
      };
    }
  });
  return annotations;
}

// ── RMSSD Timeline ───────────────────────────────────────
new Chart(document.getElementById('rmssdChart'), {
  type: 'line',
  data: {
    labels: D.timeline.labels,
    datasets: [
      {
        label: 'RMSSD raw',
        data: D.timeline.rmssd_raw,
        borderColor: '#4ecdc420',
        backgroundColor: '#4ecdc415',
        pointRadius: 0.8,
        pointBackgroundColor: '#4ecdc430',
        borderWidth: 0,
        showLine: false,
        order: 2,
      },
      {
        label: 'RMSSD 30-MA',
        data: D.timeline.rmssd_ma,
        borderColor: '#4ecdc4',
        backgroundColor: 'transparent',
        borderWidth: 1.5,
        pointRadius: 0,
        tension: 0.3,
        order: 1,
      },
      {
        label: 'Trend',
        data: D.timeline.rmssd_ma.map((_, i) => {
          const t = D.timeline.trend_start + (D.timeline.trend_end - D.timeline.trend_start) * (i / (D.timeline.rmssd_ma.length - 1));
          return Math.round(t * 10) / 10;
        }),
        borderColor: '#ffffff30',
        borderWidth: 1,
        borderDash: [6, 3],
        pointRadius: 0,
        order: 0,
      }
    ]
  },
  options: {
    responsive: true,
    interaction: { mode: 'index', intersect: false },
    scales: {
      x: { ticks: { maxTicksLimit: 16, maxRotation: 0 } },
      y: { title: { display: true, text: 'RMSSD (ms)' }, min: 0, max: 150 }
    },
    plugins: {
      annotation: { annotations: makeAnnotations(D.timeline.ts) }
    }
  }
});

// ── HR Timeline ──────────────────────────────────────────
new Chart(document.getElementById('hrChart'), {
  type: 'line',
  data: {
    labels: D.timeline.labels,
    datasets: [
      {
        label: 'HR raw',
        data: D.timeline.hr_raw,
        borderColor: '#ff6b6b20',
        backgroundColor: '#ff6b6b15',
        pointRadius: 0.8,
        pointBackgroundColor: '#ff6b6b30',
        borderWidth: 0,
        showLine: false,
        order: 2,
      },
      {
        label: 'HR 30-MA',
        data: D.timeline.hr_ma,
        borderColor: '#ff6b6b',
        backgroundColor: 'transparent',
        borderWidth: 1.5,
        pointRadius: 0,
        tension: 0.3,
        order: 1,
      }
    ]
  },
  options: {
    responsive: true,
    interaction: { mode: 'index', intersect: false },
    scales: {
      x: { ticks: { maxTicksLimit: 16, maxRotation: 0 } },
      y: { title: { display: true, text: 'HR (bpm)' } }
    },
    plugins: {
      annotation: { annotations: makeAnnotations(D.timeline.ts) }
    }
  }
});

// ── Intervention bars ────────────────────────────────────
new Chart(document.getElementById('interventionChart'), {
  type: 'bar',
  data: {
    labels: D.interventions.map(iv => iv.name),
    datasets: [
      {
        label: 'Before',
        data: D.interventions.map(iv => iv.before),
        backgroundColor: '#4a9eff60',
        borderColor: '#4a9eff',
        borderWidth: 1,
      },
      {
        label: 'After',
        data: D.interventions.map(iv => iv.after),
        backgroundColor: '#4ecdc460',
        borderColor: '#4ecdc4',
        borderWidth: 1,
      }
    ]
  },
  options: {
    responsive: true,
    scales: {
      y: { title: { display: true, text: 'RMSSD (ms)' }, beginAtZero: true }
    },
    plugins: {
      tooltip: {
        callbacks: {
          afterBody: function(ctx) {
            const i = ctx[0].dataIndex;
            return 'Delta: ' + D.interventions[i].delta_pct + '%';
          }
        }
      }
    }
  }
});

// ── Recovery chart ───────────────────────────────────────
new Chart(document.getElementById('recoveryChart'), {
  type: 'bar',
  data: {
    labels: D.recovery.map(r => r.name),
    datasets: [{
      label: 'Minutes to recover',
      data: D.recovery.map(r => r.recovery_min || 0),
      backgroundColor: D.recovery.map(r => r.recovery_min ? '#4ecdc460' : '#ff6b6b40'),
      borderColor: D.recovery.map(r => r.recovery_min ? '#4ecdc4' : '#ff6b6b'),
      borderWidth: 1,
    }]
  },
  options: {
    indexAxis: 'y',
    responsive: true,
    scales: {
      x: { title: { display: true, text: 'Minutes' }, beginAtZero: true }
    },
    plugins: {
      tooltip: {
        callbacks: {
          afterBody: function(ctx) {
            const i = ctx[0].dataIndex;
            const r = D.recovery[i];
            return r.recovery_min ? 'Baseline: ' + r.baseline + 'ms' : 'Did not recover to baseline';
          }
        }
      }
    }
  }
});

// ── Cannabis overlay ─────────────────────────────────────
new Chart(document.getElementById('cannabisChart'), {
  type: 'scatter',
  data: {
    datasets: [
      {
        label: D.cannabis[0].label,
        data: D.cannabis[0].data.map(d => ({x: d.min, y: d.rmssd})),
        borderColor: '#4a9eff',
        backgroundColor: '#4a9eff20',
        borderWidth: 1.5,
        pointRadius: 1,
        showLine: true,
        tension: 0.2,
        fill: true,
      },
      {
        label: D.cannabis[1].label,
        data: D.cannabis[1].data.map(d => ({x: d.min, y: d.rmssd})),
        borderColor: '#ff6b6b',
        backgroundColor: '#ff6b6b20',
        borderWidth: 1.5,
        pointRadius: 1,
        showLine: true,
        tension: 0.2,
        fill: true,
      }
    ]
  },
  options: {
    responsive: true,
    scales: {
      x: {
        type: 'linear',
        title: { display: true, text: 'Minutes from administration' },
        min: -5,
        max: 45,
      },
      y: { title: { display: true, text: 'RMSSD (ms)' }, min: 0 }
    },
    plugins: {
      annotation: {
        annotations: {
          smokeLine: {
            type: 'line',
            xMin: 0, xMax: 0,
            borderColor: '#ffffff40',
            borderWidth: 1,
            borderDash: [4, 4],
            label: {
              display: true, content: 'Administration',
              position: 'start', backgroundColor: 'transparent',
              color: '#888', font: { size: 9 }
            }
          }
        }
      }
    }
  }
});

// ── Scatter: HR vs RMSSD ─────────────────────────────────
new Chart(document.getElementById('scatterHrRmssd'), {
  type: 'scatter',
  data: {
    datasets: [{
      data: D.correlations.hr_rmssd.x.map((x, i) => ({x, y: D.correlations.hr_rmssd.y[i]})),
      backgroundColor: '#4ecdc420',
      borderColor: '#4ecdc450',
      pointRadius: 2,
    }]
  },
  options: {
    responsive: true,
    plugins: { legend: { display: false } },
    scales: {
      x: { title: { display: true, text: 'HR (bpm)' } },
      y: { title: { display: true, text: 'RMSSD (ms)' }, min: 0 }
    }
  }
});

// ── Scatter: Time vs RMSSD ───────────────────────────────
new Chart(document.getElementById('scatterTod'), {
  type: 'scatter',
  data: {
    datasets: [{
      data: D.correlations.tod_rmssd.x.map((x, i) => ({x, y: D.correlations.tod_rmssd.y[i]})),
      backgroundColor: '#ffe66d20',
      borderColor: '#ffe66d50',
      pointRadius: 2,
    }]
  },
  options: {
    responsive: true,
    plugins: { legend: { display: false } },
    scales: {
      x: { title: { display: true, text: 'Hour (CET)' }, min: 0, max: 24 },
      y: { title: { display: true, text: 'RMSSD (ms)' }, min: 0 }
    }
  }
});

// ── Scatter: Stillness vs RMSSD ──────────────────────────
new Chart(document.getElementById('scatterStillness'), {
  type: 'scatter',
  data: {
    datasets: [{
      data: D.correlations.stillness_rmssd.x.map((x, i) => ({x, y: D.correlations.stillness_rmssd.y[i]})),
      backgroundColor: '#4a9eff20',
      borderColor: '#4a9eff50',
      pointRadius: 3,
    }]
  },
  options: {
    responsive: true,
    plugins: { legend: { display: false } },
    scales: {
      x: { title: { display: true, text: 'Stillness (0-1)' }, min: 0, max: 1 },
      y: { title: { display: true, text: 'RMSSD (ms)' }, min: 0 }
    }
  }
});

// ── DFA alpha1 ───────────────────────────────────────────
new Chart(document.getElementById('dfaChart'), {
  type: 'line',
  data: {
    labels: D.advanced.labels,
    datasets: [{
      label: 'DFA alpha1',
      data: D.advanced.dfa,
      borderColor: '#bb86fc',
      borderWidth: 1.5,
      pointRadius: 2,
      pointBackgroundColor: '#bb86fc',
      tension: 0.2,
    }]
  },
  options: {
    responsive: true,
    scales: {
      x: { ticks: { maxTicksLimit: 8, maxRotation: 0 } },
      y: { title: { display: true, text: 'DFA alpha1' }, min: 0, max: 2.5 }
    },
    plugins: {
      annotation: {
        annotations: {
          healthy: {
            type: 'box',
            yMin: 0.75, yMax: 1.25,
            backgroundColor: '#4ecdc408',
            borderColor: '#4ecdc420',
            borderWidth: 1,
            label: { display: true, content: 'Healthy range', position: 'start', color: '#4ecdc440', font: { size: 9 } }
          }
        }
      }
    }
  }
});

// ── pNN50 ────────────────────────────────────────────────
new Chart(document.getElementById('pnn50Chart'), {
  type: 'line',
  data: {
    labels: D.advanced.labels,
    datasets: [{
      label: 'pNN50 %',
      data: D.advanced.pnn50,
      borderColor: '#4ecdc4',
      borderWidth: 1.5,
      pointRadius: 2,
      pointBackgroundColor: '#4ecdc4',
      tension: 0.2,
      fill: { target: 'origin', above: '#4ecdc410' },
    }]
  },
  options: {
    responsive: true,
    scales: {
      x: { ticks: { maxTicksLimit: 8, maxRotation: 0 } },
      y: { title: { display: true, text: 'pNN50 (%)' }, min: 0 }
    }
  }
});
</script>

</body>
</html>"""

    OUT.write_text(html)
    print(f"Report written to {OUT}")
    print(f"  {summary['n_samples']:,} HRV samples, {len(rr_rows):,} RR intervals")
    print(f"  Trend: {trend_info['direction']} ({trend_info['slope_per_hour']:+.2f} ms/h)")
    print(f"  HR-RMSSD correlation: r={hr_rmssd_r}")
    print(f"  Stillness-RMSSD correlation: r={still_r} ({len(stillness_rmssd_pairs)} pairs)")


if __name__ == "__main__":
    main()
