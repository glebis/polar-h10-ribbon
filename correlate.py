#!/usr/bin/env python3
"""
Annotation Correlation Analysis for Polar H10 HRV data.

Cross-references timestamped annotations (markers) with HRV metrics
to analyse how activities, substances, and events affect heart rate
variability.

Examples:
    python correlate.py labels
    python correlate.py impact smoking
    python correlate.py impact breathing --window 10
    python correlate.py compare smoking breathing
    python correlate.py circadian
    python correlate.py report
    python correlate.py report --html > report.html
    python correlate.py dose exercise
"""

import argparse
import datetime
import math
import os
import sqlite3
import sys
import json
from collections import defaultdict

# ── Constants ──────────────────────────────────────────────────────────────

DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hrv_data.db")
BEFORE_WINDOW = 300       # 5 min
DURING_WINDOW = 300       # 5 min (from marker onward)
AFTER_WINDOWS = {         # name → (start_offset, end_offset) in seconds
    "5min_after":  (300,  600),
    "15min_after": (900,  1200),
    "30min_after": (1800, 2100),
}
METRIC_COLS_HRV = ["rmssd", "hr_mean"]
METRIC_COLS_ADV = ["ln_rmssd", "dfa_alpha1", "pnn50"]

# ── Helpers ────────────────────────────────────────────────────────────────

def connect(db_path):
    if not os.path.exists(db_path):
        print(f"Error: database not found at {db_path}", file=sys.stderr)
        sys.exit(1)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def ts_to_dt(ts):
    return datetime.datetime.fromtimestamp(ts)


def fmt_ts(ts):
    return ts_to_dt(ts).strftime("%Y-%m-%d %H:%M")


def mean(vals):
    if not vals:
        return None
    return sum(vals) / len(vals)


def stdev(vals):
    if len(vals) < 2:
        return None
    m = mean(vals)
    return math.sqrt(sum((v - m) ** 2 for v in vals) / (len(vals) - 1))


def paired_ttest(before, after):
    """Paired t-test. Returns (t_stat, p_approx, n)."""
    diffs = [a - b for a, b in zip(after, before)]
    n = len(diffs)
    if n < 2:
        return None, None, n
    m = mean(diffs)
    sd = stdev(diffs)
    if sd is None or sd == 0:
        return None, None, n
    t = m / (sd / math.sqrt(n))
    # Two-tailed p-value approximation using the normal distribution
    # (acceptable for n >= 5; for smaller n, just report t)
    df = n - 1
    p = _approx_p(t, df)
    return t, p, n


def _approx_p(t, df):
    """Rough two-tailed p from t and df via normal approx for df>=5."""
    # Welch-Satterthwaite or simple normal approx
    x = abs(t)
    # Use normal CDF approximation (Abramowitz & Stegun 26.2.17)
    if df < 3:
        return None
    # Adjust t for df
    a = 1.0 - 1.0 / (4.0 * df)
    b = t * a
    # Standard normal CDF via error function
    p2 = _norm_sf(abs(b)) * 2.0
    return min(p2, 1.0)


def _norm_sf(x):
    """Survival function of standard normal (1 - CDF)."""
    return 0.5 * math.erfc(x / math.sqrt(2))


def fmt_val(v, decimals=2):
    if v is None:
        return "—"
    return f"{v:+.{decimals}f}" if isinstance(v, float) else str(v)


def fmt_p(p):
    if p is None:
        return "—"
    if p < 0.001:
        return "<0.001"
    if p < 0.01:
        return f"{p:.3f}"
    return f"{p:.2f}"


def significance_marker(p):
    if p is None:
        return ""
    if p < 0.001:
        return " ***"
    if p < 0.01:
        return " **"
    if p < 0.05:
        return " *"
    return ""


def print_table(headers, rows, col_widths=None):
    """Print a nicely formatted table."""
    if not rows:
        print("  (no data)")
        return
    if col_widths is None:
        col_widths = []
        for i, h in enumerate(headers):
            w = len(str(h))
            for r in rows:
                if i < len(r):
                    w = max(w, len(str(r[i])))
            col_widths.append(w + 2)

    header_line = "".join(str(h).ljust(col_widths[i]) for i, h in enumerate(headers))
    print(f"  {header_line}")
    print(f"  {'─' * sum(col_widths)}")
    for r in rows:
        line = "".join(str(r[i] if i < len(r) else "").ljust(col_widths[i]) for i in range(len(headers)))
        print(f"  {line}")


# ── Data queries ───────────────────────────────────────────────────────────

def get_markers(conn, label=None):
    if label:
        return conn.execute(
            "SELECT * FROM markers WHERE label = ? ORDER BY ts", (label,)
        ).fetchall()
    return conn.execute("SELECT * FROM markers ORDER BY ts").fetchall()


def get_hrv_window(conn, ts_start, ts_end):
    """Get HRV samples in a time window."""
    rows = conn.execute(
        "SELECT rmssd, hr_mean FROM hrv_samples WHERE ts >= ? AND ts < ?",
        (ts_start, ts_end)
    ).fetchall()
    return rows


def get_adv_window(conn, ts_start, ts_end):
    """Get advanced HRV in a time window."""
    rows = conn.execute(
        "SELECT ln_rmssd, dfa_alpha1, pnn50 FROM hrv_advanced WHERE ts >= ? AND ts < ?",
        (ts_start, ts_end)
    ).fetchall()
    return rows


def window_means(conn, ts_start, ts_end):
    """Return dict of metric → mean for a window."""
    hrv = get_hrv_window(conn, ts_start, ts_end)
    adv = get_adv_window(conn, ts_start, ts_end)
    result = {}
    for col in METRIC_COLS_HRV:
        vals = [r[col] for r in hrv if r[col] is not None]
        result[col] = mean(vals)
    for col in METRIC_COLS_ADV:
        vals = [r[col] for r in adv if r[col] is not None]
        result[col] = mean(vals)
    result["_n_hrv"] = len(hrv)
    result["_n_adv"] = len(adv)
    return result


def all_metrics():
    return METRIC_COLS_HRV + METRIC_COLS_ADV


# ── Commands ───────────────────────────────────────────────────────────────

def cmd_labels(conn, args):
    """List all unique annotation labels with counts."""
    rows = conn.execute(
        "SELECT label, COUNT(*) as cnt, MIN(ts) as first_ts, MAX(ts) as last_ts "
        "FROM markers GROUP BY label ORDER BY cnt DESC"
    ).fetchall()
    if not rows:
        print("No markers found in database.")
        return
    print(f"\n  Annotation Labels ({sum(r['cnt'] for r in rows)} total markers)\n")
    table_rows = []
    for r in rows:
        table_rows.append([
            r["label"] or "(unlabelled)",
            r["cnt"],
            fmt_ts(r["first_ts"]),
            fmt_ts(r["last_ts"]),
        ])
    print_table(["Label", "Count", "First", "Last"], table_rows)
    print()


def compute_event_windows(conn, marker_ts, before_s=BEFORE_WINDOW, during_s=DURING_WINDOW):
    """Compute metric means for each window around a marker event."""
    windows = {}
    windows["before"] = window_means(conn, marker_ts - before_s, marker_ts)
    windows["during"] = window_means(conn, marker_ts, marker_ts + during_s)
    for name, (start, end) in AFTER_WINDOWS.items():
        windows[name] = window_means(conn, marker_ts + start, marker_ts + end)
    return windows


def cmd_impact(conn, args):
    """Analyse impact of a specific annotation label."""
    label = args.label
    before_s = args.window * 60 if hasattr(args, "window") and args.window else BEFORE_WINDOW
    during_s = before_s  # symmetric

    markers = get_markers(conn, label)
    if not markers:
        print(f"\nNo markers found with label '{label}'.")
        all_labels = conn.execute("SELECT DISTINCT label FROM markers").fetchall()
        if all_labels:
            print(f"Available labels: {', '.join(r['label'] or '(unlabelled)' for r in all_labels)}")
        return

    print(f"\n  Impact Analysis: '{label}' ({len(markers)} events)\n")

    all_windows = []
    metrics = all_metrics()
    window_names = ["before", "during", "5min_after", "15min_after", "30min_after"]

    for m in markers:
        w = compute_event_windows(conn, m["ts"], before_s, during_s)
        all_windows.append(w)
        print(f"  Event at {fmt_ts(m['ts'])} (session {m['session_id']})")
        if m["hr_bpm"]:
            print(f"    Marker HR: {m['hr_bpm']} bpm, RMSSD: {m['rmssd']:.1f}" if m["rmssd"] else f"    Marker HR: {m['hr_bpm']} bpm")

        rows = []
        for metric in metrics:
            vals = []
            for wn in window_names:
                v = w[wn].get(metric)
                if v is not None:
                    vals.append(f"{v:.1f}")
                else:
                    vals.append("—")
            rows.append([metric] + vals)
        print_table(["Metric"] + window_names, rows)
        print()

    # Aggregate if multiple events
    if len(markers) >= 2:
        _print_aggregate(all_windows, window_names, metrics, len(markers))


def _print_aggregate(all_windows, window_names, metrics, n):
    """Print aggregate statistics across events."""
    print(f"  ── Aggregate ({n} events) ──\n")

    # Deltas from before baseline
    print("  Mean delta from pre-event baseline:\n")
    header = ["Metric"] + [f"Δ {wn}" for wn in window_names[1:]]
    if n >= 5:
        header.append("p-value")
    rows = []

    for metric in metrics:
        row = [metric]
        before_vals = [w["before"].get(metric) for w in all_windows]

        for wn in window_names[1:]:
            after_vals = [w[wn].get(metric) for w in all_windows]
            pairs = [(b, a) for b, a in zip(before_vals, after_vals) if b is not None and a is not None]
            if pairs:
                deltas = [a - b for b, a in pairs]
                m_delta = mean(deltas)
                row.append(fmt_val(m_delta))
            else:
                row.append("—")

        # p-value for "during" window vs before
        if n >= 5:
            during_vals = [w["during"].get(metric) for w in all_windows]
            pairs = [(b, a) for b, a in zip(before_vals, during_vals) if b is not None and a is not None]
            if len(pairs) >= 5:
                bv = [p[0] for p in pairs]
                av = [p[1] for p in pairs]
                t, p, _ = paired_ttest(bv, av)
                row.append(f"{fmt_p(p)}{significance_marker(p)}")
            else:
                row.append("—")

        rows.append(row)

    print_table(header, rows)
    print()
    if n >= 5:
        print("  Significance: * p<0.05  ** p<0.01  *** p<0.001\n")


def cmd_compare(conn, args):
    """Compare two annotation labels side by side."""
    label_a = args.label_a
    label_b = args.label_b

    window_names = ["before", "during", "5min_after", "15min_after", "30min_after"]
    metrics = all_metrics()

    results = {}
    for label in [label_a, label_b]:
        markers = get_markers(conn, label)
        if not markers:
            print(f"\nNo markers found with label '{label}'.")
            return
        windows_list = [compute_event_windows(conn, m["ts"]) for m in markers]
        # Compute mean per window per metric
        agg = {}
        for wn in window_names:
            agg[wn] = {}
            for metric in metrics:
                vals = [w[wn].get(metric) for w in windows_list if w[wn].get(metric) is not None]
                agg[wn][metric] = mean(vals)
        results[label] = {"agg": agg, "n": len(markers)}

    print(f"\n  Comparison: '{label_a}' (n={results[label_a]['n']}) vs '{label_b}' (n={results[label_b]['n']})\n")

    for metric in metrics:
        print(f"  {metric}:")
        header = ["Window", label_a, label_b, "Difference"]
        rows = []
        for wn in window_names:
            va = results[label_a]["agg"][wn].get(metric)
            vb = results[label_b]["agg"][wn].get(metric)
            diff = None
            if va is not None and vb is not None:
                diff = vb - va
            rows.append([
                wn,
                f"{va:.1f}" if va is not None else "—",
                f"{vb:.1f}" if vb is not None else "—",
                fmt_val(diff) if diff is not None else "—",
            ])
        print_table(header, rows)

        # Delta comparison
        ba = results[label_a]["agg"]["before"].get(metric)
        bb = results[label_b]["agg"]["before"].get(metric)
        da = results[label_a]["agg"]["during"].get(metric)
        db = results[label_b]["agg"]["during"].get(metric)
        if all(v is not None for v in [ba, bb, da, db]):
            delta_a = da - ba
            delta_b = db - bb
            print(f"    Δ during (from baseline):  {label_a}: {fmt_val(delta_a)}  |  {label_b}: {fmt_val(delta_b)}")
        print()


def cmd_circadian(conn, args):
    """Show average metrics by hour of day."""
    print("\n  Circadian HRV Profile\n")

    metrics = ["rmssd", "hr_mean"]
    adv_metrics = ["ln_rmssd", "dfa_alpha1"]

    hourly = defaultdict(lambda: defaultdict(list))

    # HRV samples
    rows = conn.execute("SELECT ts, rmssd, hr_mean FROM hrv_samples WHERE rmssd IS NOT NULL").fetchall()
    for r in rows:
        h = ts_to_dt(r["ts"]).hour
        for m in metrics:
            if r[m] is not None:
                hourly[h][m].append(r[m])

    # Advanced
    rows = conn.execute("SELECT ts, ln_rmssd, dfa_alpha1 FROM hrv_advanced WHERE ln_rmssd IS NOT NULL").fetchall()
    for r in rows:
        h = ts_to_dt(r["ts"]).hour
        for m in adv_metrics:
            if r[m] is not None:
                hourly[h][m].append(r[m])

    all_m = metrics + adv_metrics
    header = ["Hour", "n"] + all_m
    table_rows = []
    for h in range(24):
        if not hourly[h]:
            continue
        n = len(hourly[h].get("rmssd", []))
        vals = []
        for m in all_m:
            v = mean(hourly[h].get(m, []))
            vals.append(f"{v:.2f}" if v is not None else "—")
        table_rows.append([f"{h:02d}:00", n] + vals)

    print_table(header, table_rows)
    print()

    # Sparkline-style bar chart for RMSSD
    rmssd_by_hour = {}
    for h in range(24):
        v = mean(hourly[h].get("rmssd", []))
        if v is not None:
            rmssd_by_hour[h] = v
    if rmssd_by_hour:
        max_v = max(rmssd_by_hour.values())
        min_v = min(rmssd_by_hour.values())
        rng = max_v - min_v if max_v != min_v else 1
        print("  RMSSD by Hour:")
        for h in range(24):
            if h in rmssd_by_hour:
                bar_len = int((rmssd_by_hour[h] - min_v) / rng * 30)
                print(f"    {h:02d}:00 {'█' * bar_len}{'░' * (30 - bar_len)} {rmssd_by_hour[h]:.1f}")
        print()


def cmd_dose(conn, args):
    """Dose-response analysis: frequency/timing correlations."""
    label = args.label
    markers = get_markers(conn, label)
    if not markers:
        print(f"\nNo markers found with label '{label}'.")
        return

    print(f"\n  Dose-Response Analysis: '{label}' ({len(markers)} events)\n")

    if len(markers) < 2:
        print("  Need at least 2 events for dose-response analysis.")
        return

    # Analyse by inter-event interval
    intervals = []
    for i in range(1, len(markers)):
        gap = markers[i]["ts"] - markers[i - 1]["ts"]
        intervals.append(gap / 3600.0)  # hours

    print(f"  Inter-event intervals:")
    print(f"    Mean: {mean(intervals):.1f} hours")
    print(f"    Min:  {min(intervals):.1f} hours")
    print(f"    Max:  {max(intervals):.1f} hours")
    if stdev(intervals) is not None:
        print(f"    SD:   {stdev(intervals):.1f} hours")
    print()

    # Event number vs RMSSD delta (does effect change with repetition?)
    print("  Sequential event analysis (does effect change over time?):\n")
    metrics = all_metrics()
    header = ["Event #", "Time"] + [f"Δ {m}" for m in metrics]
    rows = []
    for i, m in enumerate(markers):
        w = compute_event_windows(conn, m["ts"])
        row = [i + 1, fmt_ts(m["ts"])]
        for metric in metrics:
            b = w["before"].get(metric)
            d = w["during"].get(metric)
            if b is not None and d is not None:
                row.append(fmt_val(d - b))
            else:
                row.append("—")
        rows.append(row)
    print_table(header, rows)

    # Simple trend: correlation of event index with delta
    if len(markers) >= 3:
        print("\n  Trend (Pearson r of event# vs RMSSD delta):")
        rmssd_deltas = []
        indices = []
        for i, m in enumerate(markers):
            w = compute_event_windows(conn, m["ts"])
            b = w["before"].get("rmssd")
            d = w["during"].get("rmssd")
            if b is not None and d is not None:
                rmssd_deltas.append(d - b)
                indices.append(float(i))
        if len(indices) >= 3:
            r = _pearson(indices, rmssd_deltas)
            print(f"    r = {r:.3f}  ({'positive' if r > 0 else 'negative'} trend)")
            if abs(r) > 0.5:
                print(f"    {'Effect appears to increase' if r > 0 else 'Effect appears to decrease'} with repeated events.")
            else:
                print(f"    No strong trend detected.")
    print()


def _pearson(x, y):
    """Pearson correlation coefficient."""
    n = len(x)
    mx, my = mean(x), mean(y)
    num = sum((xi - mx) * (yi - my) for xi, yi in zip(x, y))
    dx = math.sqrt(sum((xi - mx) ** 2 for xi in x))
    dy = math.sqrt(sum((yi - my) ** 2 for yi in y))
    if dx == 0 or dy == 0:
        return 0.0
    return num / (dx * dy)


def cmd_report(conn, args):
    """Generate full correlation report."""
    if args.html:
        _report_html(conn, args)
    else:
        _report_text(conn, args)


def _report_text(conn, args):
    """Text-based full report."""
    labels = conn.execute(
        "SELECT label, COUNT(*) as cnt FROM markers GROUP BY label ORDER BY cnt DESC"
    ).fetchall()

    total_hrv = conn.execute("SELECT COUNT(*) as c FROM hrv_samples").fetchone()["c"]
    total_adv = conn.execute("SELECT COUNT(*) as c FROM hrv_advanced").fetchone()["c"]
    total_markers = conn.execute("SELECT COUNT(*) as c FROM markers").fetchone()["c"]

    print("=" * 60)
    print("  HRV Annotation Correlation Report")
    print(f"  Generated: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 60)
    print(f"\n  Database summary:")
    print(f"    HRV samples:      {total_hrv:,}")
    print(f"    Advanced samples:  {total_adv:,}")
    print(f"    Markers:           {total_markers:,}")
    print(f"    Unique labels:     {len(labels)}")
    print()

    # Circadian
    print("─" * 60)
    print("  CIRCADIAN PROFILE")
    print("─" * 60)
    cmd_circadian(conn, args)

    # Per-label analysis
    for row in labels:
        label = row["label"]
        print("─" * 60)
        print(f"  LABEL: '{label}' ({row['cnt']} events)")
        print("─" * 60)

        class FakeArgs:
            pass
        fa = FakeArgs()
        fa.label = label
        fa.window = None
        cmd_impact(conn, fa)

    print("=" * 60)
    print("  End of Report")
    print("=" * 60)


def _report_html(conn, args):
    """HTML report with Chart.js."""
    labels = conn.execute(
        "SELECT label, COUNT(*) as cnt FROM markers GROUP BY label ORDER BY cnt DESC"
    ).fetchall()

    total_hrv = conn.execute("SELECT COUNT(*) as c FROM hrv_samples").fetchone()["c"]
    total_markers = conn.execute("SELECT COUNT(*) as c FROM markers").fetchone()["c"]

    # Circadian data
    hourly_rmssd = {}
    hourly_hr = {}
    rows = conn.execute("SELECT ts, rmssd, hr_mean FROM hrv_samples WHERE rmssd IS NOT NULL").fetchall()
    hourly_data = defaultdict(lambda: defaultdict(list))
    for r in rows:
        h = ts_to_dt(r["ts"]).hour
        if r["rmssd"] is not None:
            hourly_data[h]["rmssd"].append(r["rmssd"])
        if r["hr_mean"] is not None:
            hourly_data[h]["hr_mean"].append(r["hr_mean"])

    hours_list = list(range(24))
    rmssd_vals = [mean(hourly_data[h].get("rmssd", [])) or 0 for h in hours_list]
    hr_vals = [mean(hourly_data[h].get("hr_mean", [])) or 0 for h in hours_list]

    # Per-label impact data
    label_charts = []
    metrics = all_metrics()
    window_names = ["before", "during", "5min_after", "15min_after", "30min_after"]

    for row in labels:
        label = row["label"]
        markers_list = get_markers(conn, label)
        all_w = [compute_event_windows(conn, m["ts"]) for m in markers_list]

        chart_data = {}
        for metric in metrics:
            vals = []
            for wn in window_names:
                mv = [w[wn].get(metric) for w in all_w if w[wn].get(metric) is not None]
                vals.append(round(mean(mv), 2) if mv else 0)
            chart_data[metric] = vals

        # Deltas
        delta_data = {}
        for metric in metrics:
            before_m = chart_data[metric][0]
            if before_m:
                delta_data[metric] = [round(v - before_m, 2) for v in chart_data[metric][1:]]
            else:
                delta_data[metric] = [0] * (len(window_names) - 1)

        label_charts.append({
            "label": label,
            "n": row["cnt"],
            "chart_data": chart_data,
            "delta_data": delta_data,
        })

    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>HRV Annotation Correlation Report</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<style>
  :root {{
    --bg: #0f1117;
    --card: #1a1d27;
    --border: #2a2d3a;
    --text: #e0e0e0;
    --muted: #888;
    --accent: #6c8cff;
    --green: #4caf50;
    --red: #ef5350;
  }}
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, monospace;
    background: var(--bg);
    color: var(--text);
    padding: 2rem;
    line-height: 1.6;
  }}
  h1 {{ font-size: 1.8rem; margin-bottom: 0.5rem; color: var(--accent); }}
  h2 {{ font-size: 1.3rem; margin: 2rem 0 1rem; color: var(--accent); border-bottom: 1px solid var(--border); padding-bottom: 0.5rem; }}
  h3 {{ font-size: 1.1rem; margin: 1rem 0 0.5rem; color: var(--muted); }}
  .meta {{ color: var(--muted); margin-bottom: 2rem; }}
  .stats {{ display: flex; gap: 1rem; flex-wrap: wrap; margin-bottom: 2rem; }}
  .stat-card {{
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 1rem 1.5rem;
    min-width: 150px;
  }}
  .stat-card .value {{ font-size: 1.8rem; font-weight: bold; color: var(--accent); }}
  .stat-card .label {{ color: var(--muted); font-size: 0.85rem; }}
  .chart-container {{
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 1.5rem;
    margin-bottom: 1.5rem;
  }}
  .chart-container canvas {{ max-height: 300px; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(450px, 1fr)); gap: 1rem; }}
  table {{
    width: 100%;
    border-collapse: collapse;
    background: var(--card);
    border-radius: 8px;
    overflow: hidden;
    margin-bottom: 1rem;
  }}
  th, td {{ padding: 0.5rem 1rem; text-align: right; border-bottom: 1px solid var(--border); }}
  th {{ background: var(--border); color: var(--accent); font-size: 0.85rem; text-transform: uppercase; }}
  td:first-child, th:first-child {{ text-align: left; }}
  .positive {{ color: var(--green); }}
  .negative {{ color: var(--red); }}
</style>
</head>
<body>
<h1>HRV Annotation Correlation Report</h1>
<p class="meta">Generated {now}</p>

<div class="stats">
  <div class="stat-card"><div class="value">{total_hrv:,}</div><div class="label">HRV Samples</div></div>
  <div class="stat-card"><div class="value">{total_markers:,}</div><div class="label">Markers</div></div>
  <div class="stat-card"><div class="value">{len(labels)}</div><div class="label">Unique Labels</div></div>
</div>

<h2>Circadian Profile</h2>
<div class="grid">
  <div class="chart-container">
    <canvas id="circadian-rmssd"></canvas>
  </div>
  <div class="chart-container">
    <canvas id="circadian-hr"></canvas>
  </div>
</div>
"""

    # Per-label sections
    for i, lc in enumerate(label_charts):
        safe_id = f"label_{i}"
        html += f"""
<h2>Label: '{lc['label']}' (n={lc['n']})</h2>
<div class="grid">
  <div class="chart-container">
    <h3>Absolute Values</h3>
    <canvas id="{safe_id}-abs"></canvas>
  </div>
  <div class="chart-container">
    <h3>Delta from Baseline</h3>
    <canvas id="{safe_id}-delta"></canvas>
  </div>
</div>
"""

    # Scripts
    hours_json = json.dumps([f"{h:02d}:00" for h in hours_list])
    rmssd_json = json.dumps([round(v, 2) for v in rmssd_vals])
    hr_json = json.dumps([round(v, 2) for v in hr_vals])

    html += f"""
<script>
const chartDefaults = {{
  color: '#e0e0e0',
  borderColor: '#2a2d3a',
}};
Chart.defaults.color = '#e0e0e0';
Chart.defaults.borderColor = '#2a2d3a';

// Circadian RMSSD
new Chart(document.getElementById('circadian-rmssd'), {{
  type: 'bar',
  data: {{
    labels: {hours_json},
    datasets: [{{ label: 'RMSSD', data: {rmssd_json}, backgroundColor: 'rgba(108,140,255,0.6)', borderColor: '#6c8cff', borderWidth: 1 }}]
  }},
  options: {{ plugins: {{ title: {{ display: true, text: 'RMSSD by Hour of Day' }} }} }}
}});

// Circadian HR
new Chart(document.getElementById('circadian-hr'), {{
  type: 'bar',
  data: {{
    labels: {hours_json},
    datasets: [{{ label: 'HR (bpm)', data: {hr_json}, backgroundColor: 'rgba(239,83,80,0.6)', borderColor: '#ef5350', borderWidth: 1 }}]
  }},
  options: {{ plugins: {{ title: {{ display: true, text: 'Heart Rate by Hour of Day' }} }} }}
}});
"""

    colors = [
        ("rgba(108,140,255,0.8)", "#6c8cff"),
        ("rgba(76,175,80,0.8)", "#4caf50"),
        ("rgba(255,167,38,0.8)", "#ffa726"),
        ("rgba(239,83,80,0.8)", "#ef5350"),
        ("rgba(171,71,188,0.8)", "#ab47bc"),
    ]

    for i, lc in enumerate(label_charts):
        safe_id = f"label_{i}"
        wn_json = json.dumps(window_names)
        delta_wn_json = json.dumps(window_names[1:])

        datasets_abs = []
        datasets_delta = []
        for j, metric in enumerate(metrics):
            c_bg, c_border = colors[j % len(colors)]
            datasets_abs.append({
                "label": metric,
                "data": lc["chart_data"][metric],
                "backgroundColor": c_bg,
                "borderColor": c_border,
                "borderWidth": 1,
            })
            datasets_delta.append({
                "label": metric,
                "data": lc["delta_data"][metric],
                "backgroundColor": c_bg,
                "borderColor": c_border,
                "borderWidth": 1,
            })

        html += f"""
new Chart(document.getElementById('{safe_id}-abs'), {{
  type: 'bar',
  data: {{ labels: {wn_json}, datasets: {json.dumps(datasets_abs)} }},
  options: {{ plugins: {{ title: {{ display: true, text: 'Metric Values Across Windows' }} }} }}
}});
new Chart(document.getElementById('{safe_id}-delta'), {{
  type: 'bar',
  data: {{ labels: {delta_wn_json}, datasets: {json.dumps(datasets_delta)} }},
  options: {{ plugins: {{ title: {{ display: true, text: 'Change from Pre-Event Baseline' }} }} }}
}});
"""

    html += """
</script>
</body>
</html>"""

    print(html)


# ── CLI ────────────────────────────────────────────────────────────────────

def build_parser():
    parser = argparse.ArgumentParser(
        description="Analyse how annotations correlate with HRV metrics.",
        epilog="""Examples:
  %(prog)s labels                         List all annotation labels
  %(prog)s impact smoking                 Analyse impact of 'smoking' events
  %(prog)s impact breathing --window 10   Use 10-min window (default 5)
  %(prog)s compare smoking breathing      Side-by-side comparison
  %(prog)s circadian                      RMSSD/HR by hour of day
  %(prog)s report                         Full text report
  %(prog)s report --html > report.html    HTML report with charts
  %(prog)s dose exercise                  Dose-response analysis
""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--db", default=DEFAULT_DB, help="Path to SQLite database (default: hrv_data.db)")

    sub = parser.add_subparsers(dest="command", help="Command to run")

    sub.add_parser("labels", help="List all unique annotation labels with counts")

    p_impact = sub.add_parser("impact", help="Analyse impact of a specific label")
    p_impact.add_argument("label", help="Annotation label to analyse")
    p_impact.add_argument("--window", type=int, default=None, help="Window size in minutes (default: 5)")

    p_compare = sub.add_parser("compare", help="Compare two annotation labels")
    p_compare.add_argument("label_a", help="First label")
    p_compare.add_argument("label_b", help="Second label")

    sub.add_parser("circadian", help="Show average metrics by hour of day")

    p_report = sub.add_parser("report", help="Generate full correlation report")
    p_report.add_argument("--html", action="store_true", help="Output HTML with Chart.js charts")

    p_dose = sub.add_parser("dose", help="Dose-response analysis for a label")
    p_dose.add_argument("label", help="Annotation label to analyse")

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(0)

    conn = connect(args.db)

    commands = {
        "labels": cmd_labels,
        "impact": cmd_impact,
        "compare": cmd_compare,
        "circadian": cmd_circadian,
        "report": cmd_report,
        "dose": cmd_dose,
    }

    try:
        commands[args.command](conn, args)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
