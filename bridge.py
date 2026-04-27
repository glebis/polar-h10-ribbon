"""Polar H10 -> WebSocket bridge. Streams HR, ECG (130 Hz), and ACC (50 Hz)."""
import asyncio
import json
import struct
from bleak import BleakClient, BleakScanner
import websockets

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


async def run_sensor() -> None:
    print("scanning for Polar H10 (10s)…")
    devices = await BleakScanner.discover(timeout=10.0)
    device = next((d for d in devices if d.name and "Polar H10" in d.name), None)
    if not device:
        names = ", ".join(sorted({d.name for d in devices if d.name})) or "(none)"
        raise RuntimeError(f"Polar H10 not found. Saw: {names}. Wet the electrodes, strap on chest.")
    print(f"connecting: {device.name} [{device.address}]")

    async with BleakClient(device) as client:
        print("connected")

        info = await read_device_info(client)
        print(f"device: {info.get('manufacturer')} {info.get('model')} · fw {info.get('firmware')}")
        await broadcast({"type": "device", **info, "name": device.name, "address": device.address})

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

        while client.is_connected:
            await asyncio.sleep(1)


async def main() -> None:
    server = await websockets.serve(ws_handler, "localhost", 8765)
    print("ws://localhost:8765")
    try:
        await run_sensor()
    finally:
        server.close()
        await server.wait_closed()


if __name__ == "__main__":
    asyncio.run(main())
