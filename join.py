"""Join a multiplayer heartbeat session. Connects your Polar H10 to a relay.

Usage:
    python join.py --name "Alice" --relay ws://host:9000

Can also bridge from the local bridge.py WebSocket:
    python join.py --name "Alice" --relay ws://host:9000 --local ws://localhost:8765
"""
import argparse
import asyncio
import json

import websockets


async def run(name, relay_url, local_url=None):
    if local_url:
        # Bridge mode: read from local bridge.py, forward to relay
        print(f"  Bridging {local_url} → {relay_url} as '{name}'")
        async with websockets.connect(relay_url) as relay:
            await relay.send(json.dumps({"type": "join", "name": name}))
            resp = json.loads(await relay.recv())
            print(f"  Joined as {resp.get('name')} ({resp.get('color')})")

            async with websockets.connect(local_url) as local:
                async for raw in local:
                    msg = json.loads(raw)
                    if msg.get("type") == "hr":
                        await relay.send(raw)
    else:
        # Direct BLE mode: connect to Polar H10 and send to relay
        from bleak import BleakClient, BleakScanner
        import struct

        HR_UUID = "00002a37-0000-1000-8000-00805f9b34fb"

        print(f"  Scanning for Polar H10...")
        devices = await BleakScanner.discover(timeout=8.0)
        device = next((d for d in devices if d.name and "Polar" in d.name), None)
        if not device:
            # Try any HR monitor
            device = next((d for d in devices if d.name and ("H10" in d.name or "HR" in d.name)), None)
        if not device:
            print(f"  No heart rate monitor found. Available: {[d.name for d in devices if d.name]}")
            return

        print(f"  Found: {device.name}")

        async with websockets.connect(relay_url) as relay:
            await relay.send(json.dumps({"type": "join", "name": name}))
            resp = json.loads(await relay.recv())
            print(f"  Joined relay as {resp.get('name')} ({resp.get('color')})")

            async with BleakClient(device) as client:
                print(f"  Connected to {device.name}")

                def hr_callback(_, data):
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
                    msg = json.dumps({"type": "hr", "bpm": hr, "rr": rr})
                    asyncio.get_event_loop().create_task(relay.send(msg))

                await client.start_notify(HR_UUID, hr_callback)
                print(f"  Streaming heartbeat to relay. Ctrl+C to stop.")
                while client.is_connected:
                    await asyncio.sleep(1)


def main():
    parser = argparse.ArgumentParser(description="Join multiplayer heartbeat")
    parser.add_argument("--name", required=True, help="Your display name")
    parser.add_argument("--relay", required=True, help="Relay WebSocket URL")
    parser.add_argument("--local", default=None, help="Local bridge.py WebSocket (bridge mode)")
    args = parser.parse_args()
    asyncio.run(run(args.name, args.relay, args.local))


if __name__ == "__main__":
    main()
