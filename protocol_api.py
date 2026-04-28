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
