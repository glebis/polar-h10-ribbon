"""Polar H10 internal memory control — start/stop recording, download data.

Usage:
    python polar_memory.py start [--id SESSION_NAME]   Start recording to internal memory
    python polar_memory.py stop                         Stop recording
    python polar_memory.py status                       Check if recording
    python polar_memory.py download [--db hrv_data.db]  Download + import to SQLite
    python polar_memory.py list                         List stored exercises
    python polar_memory.py delete <exercise_id>         Delete an exercise
"""
import argparse
import asyncio
import math
import sqlite3
import struct
import sys
import time
from datetime import datetime, timezone

from bleak import BleakClient, BleakScanner

# Polar PSFTP service
PSFTP_SERVICE = "0000feee-0000-1000-8000-00805f9b34fb"
MTU_CHAR = "fb005c51-02e7-f387-1cad-8acd2d8df0c8"
D2H_CHAR = "fb005c52-02e7-f387-1cad-8acd2d8df0c8"
H2D_CHAR = "fb005c53-02e7-f387-1cad-8acd2d8df0c8"

# Query IDs
Q_START_RECORDING = 14
Q_STOP_RECORDING = 15
Q_RECORDING_STATUS = 16

# Request commands
CMD_GET = 0
CMD_REMOVE = 3


def encode_varint(value):
    result = bytearray()
    while value > 0x7F:
        result.append((value & 0x7F) | 0x80)
        value >>= 7
    result.append(value & 0x7F)
    return bytes(result)


def decode_varint(data, pos=0):
    result = 0
    shift = 0
    while pos < len(data):
        b = data[pos]
        result |= (b & 0x7F) << shift
        pos += 1
        if not (b & 0x80):
            break
        shift += 7
    return result, pos


class PolarPSFTP:
    def __init__(self, client: BleakClient):
        self.client = client
        self.seq = 0
        self.response_data = bytearray()
        self.response_complete = asyncio.Event()
        self.response_error = None

    async def setup(self):
        await self.client.start_notify(MTU_CHAR, self._on_mtu_notify)
        await self.client.start_notify(D2H_CHAR, self._on_d2h_notify)

    def _on_mtu_notify(self, _, data):
        data = bytes(data)
        header = data[0]
        status = (header >> 1) & 0x03
        payload = data[1:]

        if status == 0x00:
            # Error response
            if len(payload) >= 2:
                self.response_error = struct.unpack_from('<H', payload, 0)[0]
            self.response_complete.set()
        else:
            self.response_data.extend(payload)
            if status == 0x01:  # last frame
                self.response_complete.set()

    def _on_d2h_notify(self, _, data):
        pass

    def _next_seq(self):
        s = self.seq
        self.seq = (self.seq + 1) & 0x0F
        return s

    def _build_query(self, query_id, params=None):
        header_byte = (self._next_seq() << 4) | 0x02
        query_header = struct.pack('<H', query_id | 0x8000)
        payload = query_header + (params or b'')
        return bytes([header_byte]) + payload

    def _build_request(self, protobuf_bytes):
        header_byte = (self._next_seq() << 4) | 0x02
        req_header = struct.pack('<H', len(protobuf_bytes) & 0x7FFF)
        return bytes([header_byte]) + req_header + protobuf_bytes

    def _encode_pftp_operation(self, command, path):
        msg = b'\x08' + encode_varint(command)
        path_bytes = path.encode('utf-8')
        msg += b'\x12' + encode_varint(len(path_bytes)) + path_bytes
        return msg

    async def _send_and_wait(self, frame, timeout=30):
        self.response_data = bytearray()
        self.response_error = None
        self.response_complete.clear()
        await self.client.write_gatt_char(MTU_CHAR, frame, response=True)
        try:
            await asyncio.wait_for(self.response_complete.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            raise RuntimeError("Timeout waiting for response")
        if self.response_error is not None:
            errors = {103: "no such file", 106: "operation not permitted", 205: "disk full", 209: "battery too low"}
            raise RuntimeError(f"Device error {self.response_error}: {errors.get(self.response_error, 'unknown')}")
        return bytes(self.response_data)

    async def start_recording(self, exercise_id="hrv_session", sample_type=1, interval_s=1):
        """Start internal recording. sample_type: 1=HR, 16=RR."""
        params = b'\x08' + encode_varint(sample_type)
        duration = b'\x18' + encode_varint(interval_s)
        params += b'\x12' + encode_varint(len(duration)) + duration
        id_bytes = exercise_id.encode('utf-8')
        params += b'\x1a' + encode_varint(len(id_bytes)) + id_bytes
        frame = self._build_query(Q_START_RECORDING, params)
        await self._send_and_wait(frame)
        print(f"  Recording started: {exercise_id}")

    async def stop_recording(self):
        frame = self._build_query(Q_STOP_RECORDING)
        await self._send_and_wait(frame)
        print("  Recording stopped")

    async def recording_status(self):
        frame = self._build_query(Q_RECORDING_STATUS)
        data = await self._send_and_wait(frame)
        if len(data) >= 2:
            # Parse PbRequestRecordingStatusResult
            pos = 0
            recording_on = False
            exercise_id = ""
            while pos < len(data):
                tag = data[pos]
                field_num = tag >> 3
                wire_type = tag & 0x07
                pos += 1
                if wire_type == 0:
                    val, pos = decode_varint(data, pos)
                    if field_num == 1:
                        recording_on = bool(val)
                elif wire_type == 2:
                    length, pos = decode_varint(data, pos)
                    if field_num == 2:
                        exercise_id = data[pos:pos+length].decode('utf-8', errors='ignore')
                    pos += length
                else:
                    break
            return recording_on, exercise_id
        return False, ""

    async def list_directory(self, path="/"):
        op = self._encode_pftp_operation(CMD_GET, path)
        frame = self._build_request(op)
        data = await self._send_and_wait(frame)
        entries = []
        pos = 0
        while pos < len(data):
            if pos >= len(data):
                break
            tag = data[pos]
            field_num = tag >> 3
            wire_type = tag & 0x07
            pos += 1
            if wire_type == 2:
                length, pos = decode_varint(data, pos)
                entry_data = data[pos:pos+length]
                # Parse PbPFtpEntry
                epos = 0
                name = ""
                size = 0
                while epos < len(entry_data):
                    etag = entry_data[epos]
                    ef = etag >> 3
                    ew = etag & 0x07
                    epos += 1
                    if ew == 2:
                        el, epos = decode_varint(entry_data, epos)
                        if ef == 1:
                            name = entry_data[epos:epos+el].decode('utf-8', errors='ignore')
                        epos += el
                    elif ew == 0:
                        val, epos = decode_varint(entry_data, epos)
                        if ef == 2:
                            size = val
                    else:
                        break
                if name:
                    entries.append({"name": name, "size": size})
                pos += length
            elif wire_type == 0:
                _, pos = decode_varint(data, pos)
            else:
                break
        return entries

    async def download_file(self, path):
        op = self._encode_pftp_operation(CMD_GET, path)
        frame = self._build_request(op)
        return await self._send_and_wait(frame, timeout=90)

    async def delete_exercise(self, exercise_id):
        path = f"/{exercise_id}/"
        op = self._encode_pftp_operation(CMD_REMOVE, path)
        frame = self._build_request(op)
        await self._send_and_wait(frame)
        print(f"  Deleted: {exercise_id}")


def parse_exercise_samples(data):
    """Parse PbExerciseSamples protobuf → list of HR values and RR intervals."""
    hr_samples = []
    rr_intervals = []
    pos = 0
    while pos < len(data):
        if pos >= len(data):
            break
        tag = data[pos]
        field_num = tag >> 3
        wire_type = tag & 0x07
        pos += 1
        if wire_type == 2:
            length, pos = decode_varint(data, pos)
            chunk = data[pos:pos+length]
            if field_num == 3:
                # heart_rate_samples (packed repeated uint32)
                cpos = 0
                while cpos < len(chunk):
                    val, cpos = decode_varint(chunk, cpos)
                    hr_samples.append(val)
            elif field_num == 4:
                # rr_samples (embedded PbExerciseRRIntervals)
                cpos = 0
                while cpos < len(chunk):
                    ctag = chunk[cpos]
                    cf = ctag >> 3
                    cw = ctag & 0x07
                    cpos += 1
                    if cw == 2 and cf == 1:
                        cl, cpos = decode_varint(chunk, cpos)
                        rr_chunk = chunk[cpos:cpos+cl]
                        rpos = 0
                        while rpos < len(rr_chunk):
                            val, rpos = decode_varint(rr_chunk, rpos)
                            rr_intervals.append(val)
                        cpos += cl
                    elif cw == 0:
                        _, cpos = decode_varint(chunk, cpos)
                    else:
                        break
            pos += length
        elif wire_type == 0:
            _, pos = decode_varint(data, pos)
        else:
            break
    return hr_samples, rr_intervals


async def find_polar():
    print("  Scanning for Polar H10...")
    devices = await BleakScanner.discover(timeout=8.0)
    device = next((d for d in devices if d.name and "Polar H10" in d.name), None)
    if not device:
        names = ", ".join(sorted({d.name for d in devices if d.name})) or "(none)"
        print(f"  Not found. Saw: {names}")
        sys.exit(1)
    print(f"  Found: {device.name} [{device.address}]")
    return device


async def cmd_start(args):
    device = await find_polar()
    async with BleakClient(device) as client:
        psftp = PolarPSFTP(client)
        await psftp.setup()
        exercise_id = args.id or f"hrv_{datetime.now().strftime('%Y%m%d_%H%M')}"
        try:
            await psftp.start_recording(exercise_id, sample_type=1, interval_s=1)
        except RuntimeError as e:
            if "not permitted" in str(e):
                print("  Existing recording found. Deleting first...")
                entries = await psftp.list_directory("/")
                for entry in entries:
                    if entry["name"].endswith("/"):
                        await psftp.delete_exercise(entry["name"].rstrip("/"))
                await psftp.start_recording(exercise_id, sample_type=1, interval_s=1)
            else:
                raise
    print(f"\n  Recording to internal memory as '{exercise_id}'.")
    print("  You can disconnect now. Run 'python polar_memory.py stop' when done.")


async def cmd_stop(args):
    device = await find_polar()
    async with BleakClient(device) as client:
        psftp = PolarPSFTP(client)
        await psftp.setup()
        await psftp.stop_recording()


async def cmd_status(args):
    device = await find_polar()
    async with BleakClient(device) as client:
        psftp = PolarPSFTP(client)
        await psftp.setup()
        recording, eid = await psftp.recording_status()
        if recording:
            print(f"  Recording active: {eid}")
        else:
            print("  Not recording")


async def cmd_list(args):
    device = await find_polar()
    async with BleakClient(device) as client:
        psftp = PolarPSFTP(client)
        await psftp.setup()
        entries = await psftp.list_directory("/")
        if not entries:
            print("  No stored exercises")
        for e in entries:
            print(f"  {e['name']:30s}  {e['size']} bytes")


async def cmd_download(args):
    device = await find_polar()
    async with BleakClient(device) as client:
        psftp = PolarPSFTP(client)
        await psftp.setup()
        entries = await psftp.list_directory("/")
        if not entries:
            print("  No stored exercises to download")
            return

        db = sqlite3.connect(args.db)
        db.execute("PRAGMA journal_mode=WAL")

        for entry in entries:
            name = entry["name"].rstrip("/")
            print(f"\n  Downloading: {name}")
            try:
                data = await psftp.download_file(f"/{name}/SAMPLES.BPB")
            except RuntimeError as e:
                print(f"    Error: {e}")
                continue

            hr_samples, rr_intervals = parse_exercise_samples(data)
            print(f"    HR samples: {len(hr_samples)}, RR intervals: {len(rr_intervals)}")

            # Create session
            session_id = db.execute(
                "INSERT INTO sessions (started_at, notes) VALUES (?, ?)",
                (datetime.now(timezone.utc).isoformat(), f"Downloaded from H10 memory: {name}")
            ).lastrowid

            # Import HR as approximate RR intervals if no RR data
            now = time.time()
            if rr_intervals:
                for i, rr in enumerate(rr_intervals):
                    ts = now - len(rr_intervals) + i
                    db.execute(
                        "INSERT INTO rr_intervals (session_id, ts, rr_ms, hr_bpm) VALUES (?,?,?,?)",
                        (session_id, ts, rr, int(60000/rr) if rr > 0 else 0)
                    )
            elif hr_samples:
                for i, hr in enumerate(hr_samples):
                    ts = now - len(hr_samples) + i
                    rr = int(60000 / hr) if hr > 0 else 0
                    db.execute(
                        "INSERT INTO rr_intervals (session_id, ts, rr_ms, hr_bpm) VALUES (?,?,?,?)",
                        (session_id, ts, rr, hr)
                    )

            db.commit()
            print(f"    Imported to session #{session_id}")

            # Optionally delete after download
            if not args.keep:
                await psftp.delete_exercise(name)
                print(f"    Deleted from device")

    print("\n  Done.")


async def cmd_delete(args):
    device = await find_polar()
    async with BleakClient(device) as client:
        psftp = PolarPSFTP(client)
        await psftp.setup()
        await psftp.delete_exercise(args.exercise_id)


def main():
    parser = argparse.ArgumentParser(description="Polar H10 Internal Memory")
    sub = parser.add_subparsers(dest="command")

    p_start = sub.add_parser("start", help="Start internal recording")
    p_start.add_argument("--id", default=None, help="Exercise ID (default: auto-generated)")

    sub.add_parser("stop", help="Stop recording")
    sub.add_parser("status", help="Check recording status")

    p_list = sub.add_parser("list", help="List stored exercises")

    p_dl = sub.add_parser("download", help="Download and import to SQLite")
    p_dl.add_argument("--db", default="hrv_data.db")
    p_dl.add_argument("--keep", action="store_true", help="Don't delete after download")

    p_del = sub.add_parser("delete", help="Delete an exercise")
    p_del.add_argument("exercise_id")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return

    cmds = {
        "start": cmd_start, "stop": cmd_stop, "status": cmd_status,
        "list": cmd_list, "download": cmd_download, "delete": cmd_delete,
    }
    asyncio.run(cmds[args.command](args))


if __name__ == "__main__":
    main()
