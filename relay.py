"""Multiplayer heartbeat relay server.

Participants connect and send their HR data. Viewers connect and receive
all participants' data merged. Also serves the visualization page.

Usage:
    python relay.py [--port 9000]

Participant sends:  {"type":"join","name":"Alice"}
                    {"type":"hr","bpm":72,"rr":[831,845]}

Viewer receives:    {"type":"state","participants":{
                      "alice":{"bpm":72,"rr":[831],"rmssd":45.2,"color":"#ff4444","lastBeat":1234},
                      "bob":{"bpm":68,"rr":[882],"rmssd":52.1,"color":"#4488ff","lastBeat":1235}
                    },"sync":0.73}
"""
import asyncio
import json
import math
import time
import hashlib
from collections import defaultdict

try:
    import websockets
except ImportError:
    print("pip install websockets")
    exit(1)

COLORS = [
    "#ff4455", "#4488ff", "#44dd88", "#ffaa22",
    "#dd44ff", "#44dddd", "#ff6699", "#88cc44",
    "#ff8844", "#6644ff", "#44ffaa", "#dddd44",
]


class Participant:
    def __init__(self, name, color):
        self.name = name
        self.color = color
        self.bpm = 0
        self.rr_history = []
        self.rmssd = 0
        self.last_beat = 0
        self.last_update = time.time()

    def update(self, msg):
        self.last_update = time.time()
        if msg.get("bpm"):
            self.bpm = msg["bpm"]
        if msg.get("rr"):
            for rr in msg["rr"]:
                if 200 < rr < 2000:
                    self.rr_history.append(rr)
                    self.last_beat = time.time()
                    if len(self.rr_history) > 30:
                        self.rr_history.pop(0)
            self._compute_rmssd()

    def _compute_rmssd(self):
        if len(self.rr_history) < 4:
            return
        diffs = [(self.rr_history[i+1]-self.rr_history[i])**2 for i in range(len(self.rr_history)-1)]
        self.rmssd = math.sqrt(sum(diffs)/len(diffs))

    def to_dict(self):
        return {
            "name": self.name,
            "bpm": self.bpm,
            "rr": self.rr_history[-5:],
            "rmssd": round(self.rmssd, 1),
            "color": self.color,
            "lastBeat": self.last_beat,
        }


class Relay:
    def __init__(self):
        self.participants: dict[str, Participant] = {}
        self.senders: dict = {}  # ws -> participant_id
        self.viewers: set = set()
        self.color_idx = 0

    def compute_sync(self):
        """How synchronized are participants' heartbeats? 0=chaotic, 1=in sync."""
        active = [p for p in self.participants.values() if time.time() - p.last_update < 10]
        if len(active) < 2:
            return 0
        # Compare recent RR intervals across participants
        rr_sets = [p.rr_history[-5:] for p in active if len(p.rr_history) >= 5]
        if len(rr_sets) < 2:
            return 0
        # Sync metric: how similar are the mean RR intervals?
        means = [sum(rr)/len(rr) for rr in rr_sets]
        overall_mean = sum(means) / len(means)
        if overall_mean == 0:
            return 0
        variance = sum((m - overall_mean)**2 for m in means) / len(means)
        cv = math.sqrt(variance) / overall_mean
        return max(0, min(1, 1 - cv * 5))

    def get_state(self):
        active = {k: v.to_dict() for k, v in self.participants.items()
                  if time.time() - v.last_update < 30}
        return {
            "type": "state",
            "participants": active,
            "sync": round(self.compute_sync(), 2),
            "count": len(active),
        }

    async def handle(self, ws):
        # First message determines role
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=10)
            msg = json.loads(raw)
        except Exception:
            return

        if msg.get("type") == "join":
            await self._handle_sender(ws, msg)
        elif msg.get("type") == "view":
            await self._handle_viewer(ws)
        else:
            # Default: treat as sender with auto-name
            name = msg.get("name", f"anon-{len(self.participants)}")
            await self._handle_sender(ws, {"type": "join", "name": name})

    async def _handle_sender(self, ws, join_msg):
        name = join_msg.get("name", f"user-{len(self.participants)}")
        pid = name.lower().replace(" ", "-")
        color = COLORS[self.color_idx % len(COLORS)]
        self.color_idx += 1

        if pid not in self.participants:
            self.participants[pid] = Participant(name, color)
            print(f"  + {name} joined ({color})")

        self.senders[ws] = pid
        # Confirm join
        await ws.send(json.dumps({"type": "joined", "name": name, "color": color, "id": pid}))

        try:
            async for raw in ws:
                msg = json.loads(raw)
                if msg.get("type") == "hr":
                    self.participants[pid].update(msg)
                    # Broadcast state to viewers
                    state = json.dumps(self.get_state())
                    dead = []
                    for viewer in self.viewers:
                        try:
                            await viewer.send(state)
                        except Exception:
                            dead.append(viewer)
                    for d in dead:
                        self.viewers.discard(d)
        finally:
            self.senders.pop(ws, None)
            print(f"  - {name} disconnected")

    async def _handle_viewer(self, ws):
        self.viewers.add(ws)
        print(f"  👁 viewer connected ({len(self.viewers)} total)")
        # Send initial state
        await ws.send(json.dumps(self.get_state()))
        try:
            async for _ in ws:
                pass  # Viewers don't send, just receive
        finally:
            self.viewers.discard(ws)
            print(f"  👁 viewer disconnected ({len(self.viewers)} total)")


async def main(port):
    relay = Relay()
    print(f"\n  Multiplayer Heartbeat Relay")
    print(f"  ws://localhost:{port}")
    print(f"  Participants: send {{\"type\":\"join\",\"name\":\"Alice\"}}")
    print(f"  Viewers:      send {{\"type\":\"view\"}}")
    print()

    async with websockets.serve(relay.handle, "0.0.0.0", port):
        await asyncio.Future()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=9000)
    args = parser.parse_args()
    asyncio.run(main(args.port))
