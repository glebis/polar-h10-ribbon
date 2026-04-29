"""Polar H10 -> WebSocket bridge. Streams HR, ECG (130 Hz), and ACC (50 Hz).
Also writes RR intervals to SQLite with device ID tracking."""
import asyncio
import json
import os
import sqlite3
import struct
import time
from pathlib import Path
from bleak import BleakClient, BleakScanner
import websockets

DB_PATH = Path(__file__).parent / "hrv_data.db"


# ── SQLite live logging ─────────────────────────────────────────────────

def init_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""CREATE TABLE IF NOT EXISTS devices (
        id INTEGER PRIMARY KEY,
        serial TEXT UNIQUE NOT NULL,
        name TEXT,
        manufacturer TEXT,
        model TEXT,
        firmware TEXT,
        hardware TEXT,
        address TEXT,
        first_seen REAL,
        last_seen REAL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS sessions (
        id INTEGER PRIMARY KEY,
        started_at TEXT NOT NULL,
        ended_at TEXT,
        notes TEXT
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS rr_intervals (
        id INTEGER PRIMARY KEY,
        session_id INTEGER NOT NULL,
        ts REAL NOT NULL,
        rr_ms INTEGER NOT NULL,
        hr_bpm INTEGER,
        device_id INTEGER,
        FOREIGN KEY (session_id) REFERENCES sessions(id),
        FOREIGN KEY (device_id) REFERENCES devices(id)
    )""")
    # add device_id column if missing (existing databases)
    try:
        conn.execute("SELECT device_id FROM rr_intervals LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE rr_intervals ADD COLUMN device_id INTEGER")
    conn.commit()
    return conn


def get_or_create_device(conn, info, address):
    serial = info.get("serial") or address
    row = conn.execute("SELECT id FROM devices WHERE serial = ?", (serial,)).fetchone()
    now = time.time()
    if row:
        conn.execute("UPDATE devices SET last_seen = ?, address = ? WHERE id = ?",
                     (now, address, row[0]))
        conn.commit()
        return row[0]
    conn.execute("""INSERT INTO devices (serial, name, manufacturer, model, firmware, hardware, address, first_seen, last_seen)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                 (serial, info.get("name"), info.get("manufacturer"), info.get("model"),
                  info.get("firmware"), info.get("hardware"), address, now, now))
    conn.commit()
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def create_session(conn, device_id, device_name):
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("INSERT INTO sessions (started_at, notes) VALUES (?, ?)",
                 (now, f"Live bridge · device {device_name}"))
    conn.commit()
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


class RRLogger:
    """Buffers RR intervals and flushes to SQLite in batches."""
    def __init__(self, conn, session_id, device_id):
        self.conn = conn
        self.session_id = session_id
        self.device_id = device_id
        self.buffer = []
        self.flush_interval = 5.0
        self.last_flush = time.time()

    def add(self, hr, rr_list):
        now = time.time()
        for rr in rr_list:
            if 200 < rr < 2000:
                self.buffer.append((self.session_id, now, rr, hr, self.device_id))
        if now - self.last_flush >= self.flush_interval:
            self.flush()

    def flush(self):
        if not self.buffer:
            return
        self.conn.executemany(
            "INSERT INTO rr_intervals (session_id, ts, rr_ms, hr_bpm, device_id) VALUES (?,?,?,?,?)",
            self.buffer)
        self.conn.commit()
        self.buffer.clear()
        self.last_flush = time.time()

HR_UUID = "00002a37-0000-1000-8000-00805f9b34fb"
PMD_CONTROL = "fb005c81-02e7-f387-1cad-8acd2d8df0c8"
PMD_DATA = "fb005c82-02e7-f387-1cad-8acd2d8df0c8"
BATTERY_UUID = "00002a19-0000-1000-8000-00805f9b34fb"
DIS_MANUFACTURER = "00002a29-0000-1000-8000-00805f9b34fb"
DIS_MODEL = "00002a24-0000-1000-8000-00805f9b34fb"
DIS_SERIAL = "00002a25-0000-1000-8000-00805f9b34fb"
DIS_FIRMWARE = "00002a26-0000-1000-8000-00805f9b34fb"
DIS_HARDWARE = "00002a27-0000-1000-8000-00805f9b34fb"

# start ECG: type=ECG, sample_rate=130, resolution=14
START_ECG = bytearray([0x02, 0x00, 0x00, 0x01, 0x82, 0x00, 0x01, 0x01, 0x0E, 0x00])

# start ACC: type=ACC, sample_rate=50, resolution=16, range=8g
START_ACC = bytearray([
    0x02, 0x02,
    0x00, 0x01, 0x32, 0x00,
    0x01, 0x01, 0x10, 0x00,
    0x02, 0x01, 0x08, 0x00,
])

clients: set = set()
# sticky state to replay to any newly connecting client
sticky: dict[str, dict] = {}


async def broadcast(msg: dict) -> None:
    t = msg.get("type")
    if t in ("battery", "device"):
        sticky[t] = msg
    if not clients:
        return
    payload = json.dumps(msg)
    dead = []
    for ws in clients:
        try:
            await ws.send(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        clients.discard(ws)


async def ws_handler(ws) -> None:
    clients.add(ws)
    try:
        for m in sticky.values():
            try:
                await ws.send(json.dumps(m))
            except Exception:
                break
        await ws.wait_closed()
    finally:
        clients.discard(ws)


def parse_hr(data: bytes) -> tuple[int, list[int]]:
    flags = data[0]
    if flags & 0x01:
        hr = struct.unpack_from("<H", data, 1)[0]
        idx = 3
    else:
        hr = data[1]
        idx = 2
    if flags & 0x08:
        idx += 2
    rr = []
    if flags & 0x10:
        while idx + 1 < len(data):
            rr_raw = struct.unpack_from("<H", data, idx)[0]
            rr.append(round(rr_raw / 1024 * 1000))
            idx += 2
    return hr, rr


def parse_ecg(data: bytes) -> list[int]:
    if len(data) < 10 or data[0] != 0x00 or data[9] != 0x00:
        return []
    samples = []
    payload = data[10:]
    for i in range(0, len(payload) - 2, 3):
        val = payload[i] | (payload[i + 1] << 8) | (payload[i + 2] << 16)
        if val & 0x800000:
            val -= 0x1000000
        samples.append(val)
    return samples


def _read_bits(data: bytes, bit_pos: int, n_bits: int) -> tuple[int, int]:
    val = 0
    for b in range(n_bits):
        bp = bit_pos + b
        byte_idx = bp >> 3
        if byte_idx >= len(data):
            break
        val |= ((data[byte_idx] >> (bp & 7)) & 1) << b
    return val, bit_pos + n_bits


def parse_acc(data: bytes) -> list[tuple[int, int, int]]:
    """Parse PMD ACC frame.

    frame_type semantics (bit 7 = compression flag):
      - bit 7 cleared (0x00, 0x01, ...): raw int16 samples end-to-end
      - bit 7 set (0x80+): delta-encoded frame (reference + bit-packed deltas)
    Polar H10 firmware 5.x emits frame_type 0x01 for raw samples.
    """
    if len(data) < 10 or data[0] != 0x02:
        return []
    frame_type = data[9]
    is_delta = bool(frame_type & 0x80)
    payload = data[10:]
    out: list[tuple[int, int, int]] = []

    if not is_delta:
        for i in range(0, len(payload) - 5, 6):
            x = struct.unpack_from("<h", payload, i)[0]
            y = struct.unpack_from("<h", payload, i + 2)[0]
            z = struct.unpack_from("<h", payload, i + 4)[0]
            out.append((x, y, z))
        return out

    # delta-encoded (kept for forward compatibility; untested on H10 5.x)
    if len(payload) < 8:
        return []
    ref_x, ref_y, ref_z = struct.unpack_from("<hhh", payload, 0)
    out.append((ref_x, ref_y, ref_z))
    delta_bits = payload[6]
    count = payload[7]
    deltas = payload[8:]
    sign_bit = 1 << (delta_bits - 1)
    full = 1 << delta_bits
    bit_pos = 0
    for _ in range(count):
        dx, bit_pos = _read_bits(deltas, bit_pos, delta_bits)
        dy, bit_pos = _read_bits(deltas, bit_pos, delta_bits)
        dz, bit_pos = _read_bits(deltas, bit_pos, delta_bits)
        if dx & sign_bit: dx -= full
        if dy & sign_bit: dy -= full
        if dz & sign_bit: dz -= full
        ref_x += dx; ref_y += dy; ref_z += dz
        out.append((ref_x, ref_y, ref_z))
    return out


async def _read_str(client: BleakClient, uuid: str) -> str | None:
    try:
        raw = await client.read_gatt_char(uuid)
        return raw.decode("utf-8", errors="ignore").strip()
    except Exception:
        return None


async def read_device_info(client: BleakClient) -> dict:
    return {
        "manufacturer": await _read_str(client, DIS_MANUFACTURER),
        "model":        await _read_str(client, DIS_MODEL),
        "serial":       await _read_str(client, DIS_SERIAL),
        "firmware":     await _read_str(client, DIS_FIRMWARE),
        "hardware":     await _read_str(client, DIS_HARDWARE),
    }


_target_serial = None  # set via CLI to pick a specific strap

async def run_sensor() -> None:
    print("scanning for Polar H10 (10s)…")
    devices = await BleakScanner.discover(timeout=10.0)
    polars = [d for d in devices if d.name and "Polar H10" in d.name]
    if _target_serial:
        device = next((d for d in polars if _target_serial in (d.name or '')), None)
        if not device:
            device = next((d for d in polars if _target_serial in d.address), None)
    else:
        device = polars[0] if polars else None
    if not device:
        names = ", ".join(sorted({d.name for d in devices if d.name})) or "(none)"
        raise RuntimeError(f"Polar H10 not found. Saw: {names}. Wet the electrodes, strap on chest.")
    print(f"connecting: {device.name} [{device.address}]")

    async with BleakClient(device) as client:
        print("connected")

        info = await read_device_info(client)
        print(f"device: {info.get('manufacturer')} {info.get('model')} · fw {info.get('firmware')}")
        await broadcast({"type": "device", **info, "name": device.name, "address": device.address})

        # SQLite logging
        db = init_db()
        device_id = get_or_create_device(db, {**info, "name": device.name}, device.address)
        session_id = create_session(db, device_id, device.name)
        rr_logger = RRLogger(db, session_id, device_id)
        print(f"logging to SQLite · device #{device_id} · session #{session_id}")

        try:
            bat = await client.read_gatt_char(BATTERY_UUID)
            await broadcast({"type": "battery", "pct": int(bat[0])})
            print(f"battery: {bat[0]}%")
        except Exception:
            pass

        def bat_cb(_, data):
            if data:
                asyncio.create_task(broadcast({"type": "battery", "pct": int(data[0])}))

        def hr_cb(_, data):
            hr, rr = parse_hr(bytes(data))
            asyncio.create_task(broadcast({"type": "hr", "bpm": hr, "rr": rr}))
            rr_logger.add(hr, rr)

        acc_frames_dumped = 0

        def pmd_cb(_, data):
            nonlocal acc_frames_dumped
            b = bytes(data)
            if not b:
                return
            if b[0] == 0x00:
                samples = parse_ecg(b)
                if samples:
                    asyncio.create_task(broadcast({"type": "ecg", "samples": samples}))
            elif b[0] == 0x02:
                if acc_frames_dumped < 3:
                    acc_frames_dumped += 1
                    ft = b[9] if len(b) > 9 else -1
                    print(f"ACC FRAME #{acc_frames_dumped} len={len(b)} frame_type=0x{ft:02x}")
                    print(f"  hex: {b.hex()}")
                    parsed = parse_acc(b)
                    print(f"  parsed count: {len(parsed)}  first: {parsed[:3] if parsed else '(none)'}")
                samples = parse_acc(b)
                if samples:
                    asyncio.create_task(broadcast({"type": "acc", "samples": samples}))

        try:
            await client.start_notify(BATTERY_UUID, bat_cb)
        except Exception:
            pass
        await client.start_notify(HR_UUID, hr_cb)
        await client.start_notify(PMD_DATA, pmd_cb)
        await client.write_gatt_char(PMD_CONTROL, START_ECG, response=True)
        await asyncio.sleep(0.3)
        await client.write_gatt_char(PMD_CONTROL, START_ACC, response=True)
        print("streaming HR + ECG(130Hz) + ACC(50Hz) + battery — open http://localhost:8080")

        try:
            while client.is_connected:
                await asyncio.sleep(1)
                rr_logger.flush()
        except Exception as e:
            print(f"\n  BLE error during streaming: {e}")

        # flush remaining on disconnect
        rr_logger.flush()
        from datetime import datetime, timezone
        db.execute("UPDATE sessions SET ended_at = ? WHERE id = ?",
                   (datetime.now(timezone.utc).isoformat(), session_id))
        db.commit()
        db.close()
        print(f"\nsession #{session_id} closed — will reconnect")


_ws_port = 8765

async def main() -> None:
    server = await websockets.serve(ws_handler, "0.0.0.0", _ws_port)
    print(f"ws://localhost:{_ws_port}")
    print("auto-reconnect enabled — will retry on BLE disconnect\n")

    while True:
        try:
            await run_sensor()
        except KeyboardInterrupt:
            break
        except Exception as e:
            err = str(e)
            if "not found" in err.lower():
                print(f"\n  strap not found — scanning again in 15s…")
                await asyncio.sleep(15)
            else:
                print(f"\n  BLE disconnected ({err}) — reconnecting in 5s…")
                await asyncio.sleep(5)

    server.close()
    await server.wait_closed()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Polar H10 BLE → WebSocket bridge")
    parser.add_argument("--port", type=int, default=8765, help="WebSocket port (default 8765)")
    parser.add_argument("--device", type=str, default=None,
                        help="Target device serial or name substring (e.g. '1534913A')")
    args = parser.parse_args()
    _ws_port = args.port
    _target_serial = args.device
    asyncio.run(main())
