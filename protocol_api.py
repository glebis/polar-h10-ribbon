"""Protocol dashboard API — serves aggregated HRV data as JSON.

Usage:
    python protocol_api.py [--port 8090] [--db hrv_data.db]
"""
import argparse
import json
import math
import os
import sqlite3
import time
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

DB_PATH = Path(__file__).parent / "hrv_data.db"
JSONL_PATH = Path(__file__).parent / "hrv_log.jsonl"
TAG_PATH = Path(__file__).parent / "protocol_log.json"


def get_db(path=DB_PATH):
    conn = sqlite3.connect(str(path), timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def load_tags():
    if not TAG_PATH.exists():
        return []
    try:
        data = json.loads(TAG_PATH.read_text())
        return data.get("events", [])
    except Exception:
        return []


def save_tag(tag: dict):
    data = {"version": 1, "events": load_tags()}
    data["events"].append(tag)
    tmp = TAG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, TAG_PATH)


def load_jsonl_sessions(max_age_hours=48):
    if not JSONL_PATH.exists():
        return []
    cutoff = time.time() - max_age_hours * 3600
    rows = []
    for line in JSONL_PATH.read_text().splitlines():
        try:
            r = json.loads(line)
            if r.get("ts", 0) > cutoff:
                rows.append(r)
        except Exception:
            continue
    return rows


def api_daily(conn):
    today = datetime.now().strftime("%Y-%m-%d")

    # baseline from daily_baselines
    baseline = None
    row = conn.execute(
        "SELECT * FROM daily_baselines WHERE date = ? LIMIT 1", (today,)
    ).fetchone()
    if row:
        baseline = dict(row)

    # today's sessions
    sessions = []
    for s in conn.execute("""
        SELECT s.id, s.started_at, s.ended_at, s.notes,
               AVG(h.rmssd) as rmssd_mean, MIN(h.rmssd) as rmssd_min,
               MAX(h.rmssd) as rmssd_max, AVG(h.hr_mean) as hr_mean,
               COUNT(h.id) as sample_count
        FROM sessions s
        LEFT JOIN hrv_samples h ON h.session_id = s.id
        WHERE date(s.started_at) = ?
        GROUP BY s.id ORDER BY s.started_at
    """, (today,)).fetchall():
        sessions.append(dict(s))

    # today's stress events
    today_start = datetime.now().replace(hour=0, minute=0, second=0).timestamp()
    stress = [dict(r) for r in conn.execute("""
        SELECT ts, duration_s, severity, rmssd_at_event, trigger_type
        FROM stress_events WHERE ts > ?
        ORDER BY ts
    """, (today_start,)).fetchall()]

    # today's tags
    tags = [t for t in load_tags()
            if datetime.fromtimestamp(t.get("ts", 0)).strftime("%Y-%m-%d") == today]

    # live JSONL if no DB sessions today
    jsonl_summary = None
    if not sessions:
        rows = load_jsonl_sessions(24)
        if rows:
            rmssd = [r["rmssd"] for r in rows]
            hrs = [r["hr"] for r in rows]
            jsonl_summary = {
                "readings": len(rows),
                "duration_min": round((rows[-1]["ts"] - rows[0]["ts"]) / 60, 1),
                "rmssd_mean": round(sum(rmssd) / len(rmssd), 1),
                "hr_mean": round(sum(hrs) / len(hrs)),
            }

    return {
        "date": today,
        "baseline": baseline,
        "sessions": sessions,
        "stress_events": stress,
        "tags": tags,
        "live_summary": jsonl_summary,
    }


def api_weekly(conn):
    days = []
    for row in conn.execute("""
        SELECT date(ts, 'unixepoch', 'localtime') as day,
               AVG(rmssd) as rmssd_mean,
               AVG(sdnn) as sdnn_mean,
               AVG(hr_mean) as hr_mean,
               COUNT(*) as n
        FROM hrv_samples
        WHERE ts > strftime('%s', date('now', 'localtime', '-7 days'))
          AND rmssd > 1 AND rmssd < 200
        GROUP BY day ORDER BY day
    """).fetchall():
        days.append(dict(row))
    return days


def api_monthly(conn):
    days = []
    for row in conn.execute("""
        SELECT date(ts, 'unixepoch', 'localtime') as day,
               AVG(rmssd) as rmssd_mean,
               AVG(sdnn) as sdnn_mean,
               AVG(hr_mean) as hr_mean,
               MIN(rmssd) as rmssd_min,
               MAX(rmssd) as rmssd_max,
               COUNT(*) as n
        FROM hrv_samples
        WHERE ts > strftime('%s', date('now', 'localtime', '-30 days'))
          AND rmssd > 1 AND rmssd < 200
        GROUP BY day ORDER BY day
    """).fetchall():
        days.append(dict(row))
    return days


def api_sessions(conn, limit=50, offset=0):
    total = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]

    sessions = []
    for row in conn.execute("""
        SELECT s.id, s.started_at, s.ended_at, s.notes,
               COUNT(r.id) as rr_count,
               AVG(h.rmssd) as rmssd_mean, MIN(h.rmssd) as rmssd_min,
               MAX(h.rmssd) as rmssd_max, AVG(h.sdnn) as sdnn_mean,
               AVG(h.hr_mean) as hr_mean,
               MIN(r.ts) as first_ts, MAX(r.ts) as last_ts
        FROM sessions s
        LEFT JOIN rr_intervals r ON r.session_id = s.id
        LEFT JOIN hrv_samples h ON h.session_id = s.id
        GROUP BY s.id
        ORDER BY s.started_at DESC
        LIMIT ? OFFSET ?
    """, (limit, offset)).fetchall():
        d = dict(row)
        if d["first_ts"] and d["last_ts"]:
            d["duration_s"] = round(d["last_ts"] - d["first_ts"])
        else:
            d["duration_s"] = 0
        # heuristic session type
        try:
            started = datetime.fromisoformat(d["started_at"])
            hour = started.hour
            dur = d["duration_s"]
            if dur > 6 * 3600:
                d["type"] = "overnight"
            elif 5 <= hour <= 9 and dur >= 240:
                d["type"] = "baseline"
            else:
                d["type"] = "practice"
        except Exception:
            d["type"] = "unknown"
        sessions.append(d)

    return {"total": total, "sessions": sessions}


def api_trend_signal(conn):
    """Compute 'is it working?' from 14+ days of morning-ish data."""
    rows = conn.execute("""
        SELECT date(ts, 'unixepoch', 'localtime') as day, AVG(rmssd) as rmssd_mean
        FROM hrv_samples
        WHERE ts > strftime('%s', date('now', 'localtime', '-30 days'))
          AND rmssd > 1 AND rmssd < 200
        GROUP BY day ORDER BY day
    """).fetchall()

    if len(rows) < 3:
        return {"signal": "insufficient", "slope": 0, "days": len(rows)}

    xs = list(range(len(rows)))
    ys = [r["rmssd_mean"] for r in rows]
    n = len(xs)
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    den = sum((x - mx) ** 2 for x in xs)
    slope = num / den if den > 0 else 0

    if slope > 0.1 and n >= 5:
        signal = "improving"
    elif slope < -0.1 and n >= 5:
        signal = "declining"
    else:
        signal = "stable"

    return {
        "signal": signal,
        "slope_per_day": round(slope, 3),
        "days": n,
        "first_day": rows[0]["day"],
        "last_day": rows[-1]["day"],
    }


def api_analytics(conn):
    """Deep statistical analysis — uses hrv_metrics module, partitions by session.

    All metrics computed from cleaned NN intervals per session, then aggregated.
    Never computes across session boundaries.
    """
    import hrv_metrics as hm
    result = {}

    # Load RR intervals PER SESSION (never cross session boundaries)
    sessions = conn.execute("""
        SELECT DISTINCT session_id FROM rr_intervals ORDER BY session_id
    """).fetchall()

    all_metrics = []
    all_nn = []
    best_session = None
    best_rmssd = 0

    for row in sessions:
        sid = row["session_id"]
        rr_rows = conn.execute(
            "SELECT rr_ms FROM rr_intervals WHERE session_id = ? ORDER BY ts",
            (sid,)
        ).fetchall()
        rr = [r["rr_ms"] for r in rr_rows]
        if len(rr) < 30:
            continue

        nn = hm.clean_rr(rr)
        if len(nn) < 20:
            continue

        metrics = hm.compute_all(nn)
        metrics["session_id"] = sid
        all_metrics.append(metrics)
        all_nn.extend(nn)

        if metrics["rmssd"] and metrics["rmssd"] > best_rmssd:
            best_rmssd = metrics["rmssd"]
            best_session = metrics

    if not all_metrics:
        return {"error": "insufficient data"}

    # Aggregate: weighted average by sample count
    def wavg(key):
        vals = [(m[key], m["n"]) for m in all_metrics if m.get(key) is not None]
        if not vals:
            return None
        total_n = sum(n for _, n in vals)
        return round(sum(v * n for v, n in vals) / total_n, 1) if total_n > 0 else None

    # Per-session sparklines
    result["session_rmssd"] = [m["rmssd"] for m in all_metrics if m.get("rmssd")]
    result["session_sd1"] = [m["poincare"]["sd1"] for m in all_metrics if m.get("poincare")]

    # Aggregated metrics (weighted by session length)
    result["pnn50"] = {
        "value": wavg("pnn50"),
        "pnn20": wavg("pnn20"),
        "n": sum(m["n"] for m in all_metrics),
        "sessions": len(all_metrics),
        "interpretation": (
            "High parasympathetic activity" if (wavg("pnn50") or 0) > 20 else
            "Normal vagal modulation" if (wavg("pnn50") or 0) > 5 else
            "Low vagal modulation — may reflect anxiety or deconditioning"
        ),
        "note": "Computed per-session with artifact correction, then averaged.",
        "ref": "Bigger JT et al. Am J Cardiol. 1992;69(11):891-898"
    }

    # Poincaré from best (longest clean) session — not averaged
    if best_session and best_session.get("poincare"):
        p = best_session["poincare"]
        result["poincare"] = {
            **p,
            "interpretation": (
                "Vagal dominant (ratio > 0.5)" if p["ratio"] > 0.5 else
                "Sympathetic shift (ratio < 0.3)" if p["ratio"] < 0.3 else
                "Balanced autonomic modulation"
            ),
            "what": "SD1 = short-term vagal variability, SD2 = longer-term. Computed from cleaned NN intervals of best session.",
            "ref": "Brennan M et al. IEEE Trans Biomed Eng. 2001;48(11):1342-1347"
        }

    # RMSSD/SDNN ratio
    r = wavg("rmssd_sdnn_ratio")
    if r:
        result["rmssd_sdnn_ratio"] = {
            "value": r,
            "interpretation": (
                "Vagal dominant" if r > 0.5 else
                "Mixed autonomic" if r > 0.3 else
                "Sympathetic dominant"
            ),
            "what": "Values > 0.5 suggest parasympathetic predominance."
        }

    # Sample entropy — from longest session with ≥200 beats
    long_sessions = [m for m in all_metrics if m.get("sample_entropy") is not None]
    if long_sessions:
        se = max(long_sessions, key=lambda m: m["n"])
        result["sample_entropy"] = {
            "value": se["sample_entropy"],
            "window_size": se["n"],
            "interpretation": (
                "High complexity — healthy autonomic flexibility" if se["sample_entropy"] > 1.5 else
                "Moderate complexity" if se["sample_entropy"] > 1.0 else
                "Low complexity — reduced autonomic adaptability"
            ),
            "what": "Measures unpredictability of NN intervals. Higher = more complex = healthier. "
                    "Computed from artifact-corrected NN intervals of longest session.",
            "ref": "Richman JS & Moorman JR. Am J Physiol. 2000;278(6):H2039-H2049"
        }

    # DFA alpha1 — from longest session with ≥64 beats
    dfa_sessions = [m for m in all_metrics if m.get("dfa_alpha1") is not None]
    if dfa_sessions:
        dfa = max(dfa_sessions, key=lambda m: m["n"])
        result["dfa_alpha1"] = {
            "value": dfa["dfa_alpha1"],
            "window_size": dfa["n"],
            "interpretation": (
                "Healthy fractal correlation (0.75–1.0)" if 0.75 <= dfa["dfa_alpha1"] <= 1.0 else
                "Parasympathetic dominance (< 0.75)" if dfa["dfa_alpha1"] < 0.75 else
                "Loss of fractal complexity (> 1.0) — may reflect sympathetic rigidity"
            ),
            "what": "Fractal scaling of heartbeat intervals. Computed from cleaned NN intervals, scales 4-16.",
            "ref": "Peng CK et al. Chaos. 1995;5(1):82-87"
        }

    # Triangular index
    ti = wavg("triangular_index")
    if ti:
        result["triangular_index"] = {
            "value": ti,
            "interpretation": (
                "Normal variability" if ti > 20 else
                "Reduced variability" if ti > 10 else
                "Low variability"
            ),
            "what": "Total NN / max histogram bin. Robust to artifacts. "
                    "Note: clinical thresholds are for 24h recordings; short-term values will be lower.",
            "ref": "Task Force. Circulation. 1996;93(5):1043-1065"
        }

    # Stress-recovery from stress_events table
    stress_stats = conn.execute("""
        SELECT COUNT(*) as n, SUM(duration_s) as total_stress_s,
               AVG(severity) as avg_sev, AVG(rmssd_at_event) as avg_rmssd
        FROM stress_events
    """).fetchone()

    total_recording_s = conn.execute("""
        SELECT SUM(max_ts - min_ts) as total
        FROM (SELECT MIN(ts) as min_ts, MAX(ts) as max_ts FROM rr_intervals GROUP BY session_id)
    """).fetchone()["total"] or 1

    if stress_stats["n"] > 0 and stress_stats["total_stress_s"]:
        stress_pct = stress_stats["total_stress_s"] / total_recording_s * 100
        result["stress_recovery"] = {
            "stress_pct": round(stress_pct, 1),
            "recovery_pct": round(100 - stress_pct, 1),
            "total_stress_min": round(stress_stats["total_stress_s"] / 60, 1),
            "total_recording_min": round(total_recording_s / 60, 1),
            "avg_event_duration_s": round(stress_stats["total_stress_s"] / stress_stats["n"]),
            "interpretation": (
                "High stress load — more than 30% of recording time" if stress_pct > 30 else
                "Moderate stress load" if stress_pct > 15 else
                "Good recovery capacity — stress events are brief"
            )
        }

    return result



def api_insights(conn):
    """Research-driven HRV insights from all available data."""
    insights = []

    # sparkline data: hourly averages for each metric
    sparklines = {}
    for metric, col in [("rmssd", "rmssd"), ("sdnn", "sdnn"), ("hr", "hr_mean")]:
        rows = conn.execute(f"""
            SELECT AVG({col}) as v
            FROM hrv_samples
            WHERE {col} IS NOT NULL AND rmssd > 1 AND rmssd < 150
            GROUP BY cast(ts / 3600 as int)
            ORDER BY cast(ts / 3600 as int)
        """).fetchall()
        sparklines[metric] = [round(r["v"], 1) for r in rows if r["v"]]

    # daily sparkline for trend
    daily_rmssd = conn.execute("""
        SELECT date(ts, 'unixepoch', 'localtime') as day, AVG(rmssd) as v
        FROM hrv_samples WHERE rmssd > 1 AND rmssd < 150
        GROUP BY day ORDER BY day
    """).fetchall()
    sparklines["daily_rmssd"] = [round(r["v"], 1) for r in daily_rmssd]

    # per-session sparkline
    session_rmssd = conn.execute("""
        SELECT session_id, AVG(rmssd) as v
        FROM hrv_samples WHERE rmssd > 1 AND rmssd < 150
        GROUP BY session_id ORDER BY session_id
    """).fetchall()
    sparklines["session_rmssd"] = [round(r["v"], 1) for r in session_rmssd]

    # stress events over time (hourly count)
    stress_hourly = conn.execute("""
        SELECT COUNT(*) as n FROM stress_events
        GROUP BY cast(ts / 3600 as int) ORDER BY cast(ts / 3600 as int)
    """).fetchall()
    sparklines["stress"] = [r["n"] for r in stress_hourly]

    # overall stats
    stats = conn.execute("""
        SELECT COUNT(*) as n, AVG(rmssd) as rmssd, AVG(sdnn) as sdnn, AVG(hr_mean) as hr,
               MIN(rmssd) as rmssd_min, MAX(rmssd) as rmssd_max
        FROM hrv_samples WHERE rmssd > 1 AND rmssd < 150
    """).fetchone()

    if not stats or stats["n"] < 10:
        return {"insights": [{"type": "info", "title": "Not enough data",
                              "body": "Need at least 10 HRV samples. Keep recording."}]}

    avg_rmssd = stats["rmssd"]
    avg_sdnn = stats["sdnn"]
    avg_hr = stats["hr"]
    total_n = stats["n"]

    # 1. SDNN risk stratification (Kleiger et al., 1987; Task Force, 1996)
    if avg_sdnn and avg_sdnn > 0:
        if avg_sdnn < 50:
            insights.append({
                "type": "warning", "title": f"SDNN {avg_sdnn:.0f}ms — below clinical threshold",
                "body": "SDNN < 50ms is associated with 5.3× increased cardiac mortality risk "
                        "(Kleiger et al., 1987). This is the single strongest HRV predictor of health outcomes. "
                        "Your biofeedback protocol should prioritize raising this number.",
                "metric": "sdnn", "value": round(avg_sdnn, 1), "threshold": 50,
                "ref": "Kleiger RE et al. Am J Cardiol. 1987;59(4):256-262"
            })
        elif avg_sdnn < 100:
            insights.append({
                "type": "info", "title": f"SDNN {avg_sdnn:.0f}ms — moderate range",
                "body": "Normal 24h SDNN is 100–180ms. Your shorter recording periods will naturally show lower values. "
                        "Track the trend over weeks — a rising SDNN means your autonomic flexibility is improving.",
                "metric": "sdnn", "value": round(avg_sdnn, 1), "threshold": 100,
                "ref": "Task Force of ESC/NASPE. Circulation. 1996;93(5):1043-1065"
            })

    # 2. RMSSD and parasympathetic tone (Shaffer & Ginsberg, 2017)
    if avg_rmssd < 20:
        insights.append({
            "type": "warning", "title": f"RMSSD {avg_rmssd:.0f}ms — low parasympathetic tone",
            "body": "RMSSD reflects vagal (parasympathetic) activity. Values below 20ms indicate "
                    "reduced vagal modulation, common in GAD. Meta-analysis shows GAD reduces resting "
                    "HRV by 15–30% vs healthy controls. Coherence breathing at resonant frequency "
                    "(~6 breaths/min) is the most evidence-based intervention.",
            "metric": "rmssd", "value": round(avg_rmssd, 1),
            "ref": "Chalmers JA et al. Biol Psychol. 2014;98:12-26"
        })
    elif avg_rmssd < 35:
        insights.append({
            "type": "info", "title": f"RMSSD {avg_rmssd:.0f}ms — low-normal range",
            "body": "Your RMSSD is in the lower normal range. For reference, healthy adults aged 30–40 "
                    "average 27–45ms (Nunan et al., 2010). With consistent biofeedback practice, "
                    "expect 10–20% improvement over 6–10 weeks.",
            "metric": "rmssd", "value": round(avg_rmssd, 1),
            "ref": "Nunan D et al. Scand J Med Sci Sports. 2010;20(4):e289-e300"
        })

    # 3. Resting HR and autonomic balance
    if avg_hr and avg_hr > 80:
        insights.append({
            "type": "caution", "title": f"Resting HR {avg_hr:.0f} bpm — elevated",
            "body": "Resting HR above 80 suggests sympathetic dominance. This correlates with your low RMSSD. "
                    "As vagal tone improves through biofeedback, resting HR typically drops 3–8 bpm "
                    "over 8–12 weeks (Lehrer et al., 2003).",
            "metric": "hr", "value": round(avg_hr),
            "ref": "Lehrer PM et al. Appl Psychophysiol Biofeedback. 2003;28(1):1-10"
        })

    # 4. Time-of-day analysis — morning vs evening
    morning = conn.execute("""
        SELECT AVG(rmssd) as rmssd, AVG(hr_mean) as hr, COUNT(*) as n
        FROM hrv_samples
        WHERE cast(strftime('%H', ts, 'unixepoch', 'localtime') as int) BETWEEN 6 AND 10
          AND rmssd > 1 AND rmssd < 150
    """).fetchone()
    evening = conn.execute("""
        SELECT AVG(rmssd) as rmssd, AVG(hr_mean) as hr, COUNT(*) as n
        FROM hrv_samples
        WHERE cast(strftime('%H', ts, 'unixepoch', 'localtime') as int) BETWEEN 20 AND 23
          AND rmssd > 1 AND rmssd < 150
    """).fetchone()

    if morning["n"] > 5 and evening["n"] > 5 and morning["rmssd"] and evening["rmssd"]:
        delta = morning["rmssd"] - evening["rmssd"]
        pct = delta / evening["rmssd"] * 100 if evening["rmssd"] > 0 else 0
        insights.append({
            "type": "data", "title": f"Morning RMSSD {morning['rmssd']:.0f}ms vs evening {evening['rmssd']:.0f}ms",
            "body": f"Your morning HRV is {abs(pct):.0f}% {'higher' if delta > 0 else 'lower'} than evening. "
                    f"{'Morning elevation is normal — sleep restores vagal tone. ' if delta > 0 else 'Evening suppression is likely cannabis-related. '}"
                    f"Morning readings are your cleanest baseline for tracking protocol effectiveness.",
            "metric": "circadian", "morning": round(morning["rmssd"], 1), "evening": round(evening["rmssd"], 1)
        })

    # 5. Stress event analysis
    stress = conn.execute("""
        SELECT COUNT(*) as n, AVG(severity) as sev, AVG(duration_s) as dur, AVG(rmssd_at_event) as rmssd
        FROM stress_events
    """).fetchone()

    if stress["n"] > 0:
        events_per_hour = stress["n"] / max(1, total_n * 5 / 3600)  # ~5s per sample
        insights.append({
            "type": "data", "title": f"{stress['n']} stress events detected",
            "body": f"Average severity {stress['sev']:.1f}/1.0, duration {stress['dur']:.0f}s, "
                    f"RMSSD at event {stress['rmssd']:.0f}ms. "
                    f"That's ~{events_per_hour:.1f} events/hour. "
                    f"{'Frequent but mild — consistent with GAD pattern (chronic low-grade activation). ' if events_per_hour > 1 else ''}"
                    f"Track whether event frequency decreases with practice — that's a key outcome measure.",
            "metric": "stress", "count": stress["n"], "per_hour": round(events_per_hour, 1)
        })

    # 6. Overnight recovery capacity
    overnight = conn.execute("""
        SELECT AVG(rmssd) as rmssd, MAX(rmssd) as peak, AVG(hr_mean) as hr
        FROM hrv_samples h
        JOIN sessions s ON h.session_id = s.id
        WHERE (julianday(s.ended_at) - julianday(s.started_at)) * 24 > 4
          AND rmssd > 1 AND rmssd < 200
    """).fetchone()

    if overnight["rmssd"] and overnight["peak"]:
        insights.append({
            "type": "positive" if overnight["peak"] > 60 else "info",
            "title": f"Overnight peak RMSSD: {overnight['peak']:.0f}ms",
            "body": f"Your nervous system reached {overnight['peak']:.0f}ms during sleep "
                    f"(avg {overnight['rmssd']:.0f}ms). "
                    f"{'This shows strong vagal recovery capacity — your baseline suppression is situational, not structural. ' if overnight['peak'] > 60 else ''}"
                    f"Overnight HRV is considered the most reliable measure of autonomic health "
                    f"(Shaffer & Ginsberg, 2017).",
            "metric": "overnight", "peak": round(overnight["peak"], 1), "avg": round(overnight["rmssd"], 1),
            "ref": "Shaffer F & Ginsberg JP. Front Public Health. 2017;5:258"
        })

    # 7. Biofeedback protocol recommendations
    practice_count = conn.execute("""
        SELECT COUNT(DISTINCT date(started_at)) as days
        FROM sessions
        WHERE julianday(started_at) > julianday('now', '-14 days')
    """).fetchone()["days"]

    insights.append({
        "type": "protocol", "title": f"Protocol adherence: {practice_count} days in last 14",
        "body": f"Research shows optimal HRV biofeedback requires 4–5 sessions/week, 15–20 min each "
                f"(Lehrer & Gevirtz, 2014). Most studies see significant results at week 4–6. "
                f"{'You\'re on track — maintain this frequency.' if practice_count >= 8 else 'Try to increase to at least 4 sessions per week.'} "
                f"Morning baseline measurement (5 min resting, before substances) is essential for tracking progress.",
        "metric": "adherence", "days_active": practice_count,
        "ref": "Lehrer PM & Gevirtz R. Biofeedback. 2014;42(1):26-31"
    })

    return {
        "insights": insights,
        "summary": {
            "rmssd_avg": round(avg_rmssd, 1),
            "sdnn_avg": round(avg_sdnn, 1) if avg_sdnn else None,
            "hr_avg": round(avg_hr) if avg_hr else None,
            "total_samples": total_n,
            "total_hours": round(total_n * 5 / 3600, 1),
        },
        "sparklines": sparklines,
    }


class Handler(BaseHTTPRequestHandler):
    db_path = DB_PATH

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)

        conn = get_db(self.db_path)
        try:
            if path == "/api/daily":
                self._json(api_daily(conn))
            elif path == "/api/weekly":
                self._json(api_weekly(conn))
            elif path == "/api/monthly":
                self._json(api_monthly(conn))
            elif path == "/api/sessions":
                limit = int(params.get("limit", [50])[0])
                offset = int(params.get("offset", [0])[0])
                self._json(api_sessions(conn, limit, offset))
            elif path == "/api/trend":
                self._json(api_trend_signal(conn))
            elif path == "/api/analytics":
                self._json(api_analytics(conn))
            elif path == "/api/insights":
                self._json(api_insights(conn))
            elif path == "/api/tags":
                date_filter = params.get("date", [None])[0]
                tags = load_tags()
                if date_filter:
                    tags = [t for t in tags
                            if datetime.fromtimestamp(t.get("ts", 0)).strftime("%Y-%m-%d") == date_filter]
                self._json(tags)
            else:
                self.send_error(404)
        finally:
            conn.close()

    def do_POST(self):
        if self.path != "/api/tag":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length))
        if "ts" not in body:
            body["ts"] = int(time.time())
        save_tag(body)
        self._json({"ok": True})

    def _json(self, data):
        body = json.dumps(data, default=str).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self._cors()
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def log_message(self, fmt, *args):
        pass


def main():
    parser = argparse.ArgumentParser(description="Protocol dashboard API")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--db", default=str(DB_PATH))
    args = parser.parse_args()

    Handler.db_path = Path(args.db)
    server = HTTPServer(("localhost", args.port), Handler)
    print(f"protocol API on http://localhost:{args.port}/api/")
    print(f"  db: {args.db}")
    print(f"  tags: {TAG_PATH}")
    server.serve_forever()


if __name__ == "__main__":
    main()
