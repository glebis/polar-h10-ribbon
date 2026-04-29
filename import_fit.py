"""Import FIT/TCX/CSV files from Polar Beat/Flow into hrv_data.db.

Usage:
    python import_fit.py <file.FIT|file.TCX|file.CSV> [--db hrv_data.db]

Supports:
  - FIT: Garmin FIT format (per-second HR, RR if available)
  - TCX: Garmin TCX format (per-second HR)
  - CSV: Polar Flow CSV export

Automatically detects format from extension.
Creates session + device entries, imports HR and RR data.
"""
import argparse
import csv
import json
import sqlite3
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent / "hrv_data.db"


def init_db(db_path):
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""CREATE TABLE IF NOT EXISTS devices (
        id INTEGER PRIMARY KEY, serial TEXT UNIQUE NOT NULL,
        name TEXT, manufacturer TEXT, model TEXT, firmware TEXT,
        hardware TEXT, address TEXT, first_seen REAL, last_seen REAL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS sessions (
        id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, ended_at TEXT, notes TEXT
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS rr_intervals (
        id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL,
        ts REAL NOT NULL, rr_ms INTEGER NOT NULL, hr_bpm INTEGER,
        device_id INTEGER,
        FOREIGN KEY (session_id) REFERENCES sessions(id)
    )""")
    try:
        conn.execute("SELECT device_id FROM rr_intervals LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE rr_intervals ADD COLUMN device_id INTEGER")
    conn.commit()
    return conn


def get_or_create_device(conn, name="Polar Beat Import", serial="polar-beat"):
    row = conn.execute("SELECT id FROM devices WHERE serial = ?", (serial,)).fetchone()
    now = time.time()
    if row:
        conn.execute("UPDATE devices SET last_seen = ? WHERE id = ?", (now, row[0]))
        conn.commit()
        return row[0]
    conn.execute("""INSERT INTO devices (serial, name, manufacturer, first_seen, last_seen)
                    VALUES (?, ?, 'Polar', ?, ?)""", (serial, name, now, now))
    conn.commit()
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def import_fit(filepath, conn, device_id):
    try:
        import fitdecode
    except ImportError:
        sys.exit("pip install fitdecode")

    fit = fitdecode.FitReader(str(filepath))
    records = []
    rr_intervals = []
    first_ts = None
    last_ts = None

    for frame in fit:
        if isinstance(frame, fitdecode.FitDataMessage):
            if frame.name == 'record':
                ts = hr = None
                for field in frame.fields:
                    if field.name == 'timestamp' and field.value:
                        ts = field.value
                        if not first_ts:
                            first_ts = ts
                        last_ts = ts
                    if field.name == 'heart_rate' and field.value:
                        hr = field.value
                if ts and hr:
                    records.append((ts, hr))

            if frame.name == 'hrv':
                for field in frame.fields:
                    if field.name == 'time' and field.value:
                        vals = field.value if isinstance(field.value, (list, tuple)) else [field.value]
                        for v in vals:
                            if v and v > 0:
                                rr_intervals.append(int(v * 1000))

    if not records:
        print("no HR data found in FIT file")
        return

    started = first_ts.isoformat() if first_ts else datetime.now(timezone.utc).isoformat()
    ended = last_ts.isoformat() if last_ts else None
    duration_min = (last_ts - first_ts).total_seconds() / 60 if first_ts and last_ts else 0

    conn.execute("INSERT INTO sessions (started_at, ended_at, notes) VALUES (?, ?, ?)",
                 (started, ended, f"FIT import · {filepath.name} · {len(records)} HR · {len(rr_intervals)} RR"))
    session_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    if rr_intervals:
        # use RR intervals with timestamps derived from cumulative RR
        base_ts = first_ts.timestamp()
        cumulative = 0
        for rr in rr_intervals:
            cumulative += rr / 1000
            hr_est = int(60000 / rr) if rr > 0 else 0
            conn.execute("INSERT INTO rr_intervals (session_id, ts, rr_ms, hr_bpm, device_id) VALUES (?,?,?,?,?)",
                         (session_id, base_ts + cumulative, rr, hr_est, device_id))
    else:
        # no RR — use per-second HR, estimate RR
        for ts, hr in records:
            rr = int(60000 / hr) if hr > 0 else 0
            conn.execute("INSERT INTO rr_intervals (session_id, ts, rr_ms, hr_bpm, device_id) VALUES (?,?,?,?,?)",
                         (session_id, ts.timestamp(), rr, hr, device_id))

    conn.commit()
    print(f"imported session #{session_id}: {len(records)} HR records, {len(rr_intervals)} RR intervals, {duration_min:.0f} min")
    return session_id


def import_tcx(filepath, conn, device_id):
    ns = {'tcx': 'http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2'}
    tree = ET.parse(str(filepath))
    trackpoints = tree.findall('.//tcx:Trackpoint', ns)

    if not trackpoints:
        print("no trackpoints in TCX file")
        return

    records = []
    for tp in trackpoints:
        t = tp.find('tcx:Time', ns)
        hr_el = tp.find('tcx:HeartRateBpm/tcx:Value', ns)
        if t is not None and hr_el is not None:
            ts = datetime.fromisoformat(t.text.replace('Z', '+00:00'))
            records.append((ts, int(hr_el.text)))

    if not records:
        print("no HR data in TCX")
        return

    started = records[0][0].isoformat()
    ended = records[-1][0].isoformat()
    duration_min = (records[-1][0] - records[0][0]).total_seconds() / 60

    conn.execute("INSERT INTO sessions (started_at, ended_at, notes) VALUES (?, ?, ?)",
                 (started, ended, f"TCX import · {filepath.name} · {len(records)} points"))
    session_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    for ts, hr in records:
        rr = int(60000 / hr) if hr > 0 else 0
        conn.execute("INSERT INTO rr_intervals (session_id, ts, rr_ms, hr_bpm, device_id) VALUES (?,?,?,?,?)",
                     (session_id, ts.timestamp(), rr, hr, device_id))

    conn.commit()
    print(f"imported session #{session_id}: {len(records)} points, {duration_min:.0f} min")
    return session_id


def main():
    parser = argparse.ArgumentParser(description="Import FIT/TCX/CSV into hrv_data.db")
    parser.add_argument("file", help="FIT, TCX, or CSV file to import")
    parser.add_argument("--db", default=str(DB_PATH), help="SQLite database path")
    parser.add_argument("--device", default="polar-beat", help="Device serial for tagging")
    args = parser.parse_args()

    filepath = Path(args.file)
    if not filepath.exists():
        sys.exit(f"file not found: {filepath}")

    conn = init_db(Path(args.db))
    device_id = get_or_create_device(conn, filepath.stem, args.device)

    ext = filepath.suffix.lower()
    if ext == '.fit':
        import_fit(filepath, conn, device_id)
    elif ext == '.tcx':
        import_tcx(filepath, conn, device_id)
    else:
        sys.exit(f"unsupported format: {ext} (use .fit or .tcx)")

    conn.close()


if __name__ == "__main__":
    main()
